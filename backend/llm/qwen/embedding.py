"""调用千问文本向量接口并校验批次、维度和返回用量。"""

import hashlib
import math
from functools import lru_cache
from pathlib import Path

import httpx
from tokenizers import Tokenizer


class EmbeddingError(ValueError):
    """表示可向应用层报告的向量配置、输入或服务异常。"""
    pass


@lru_cache(maxsize=4)
def load_tokenizer(path):
    """读取并缓存本地 tokenizer 文件，不联网下载权重。

    Args:
        path: 分词器 JSON 文件路径。

    Returns:
        可复用的 Tokenizer 实例。

    Raises:
        EmbeddingError: 指定文件不存在。
    """
    location = Path(path)
    if not location.is_file():
        raise EmbeddingError("缺少 Qwen tokenizer，请先运行 python -m scripts.setup_rag")
    return Tokenizer.from_file(str(location))


def token_count(text, settings):
    """计算文本实际使用的分词数量，不添加特殊标记。

    Args:
        text: 待计数的完整文本。
        settings: 包含 tokenizer 路径的应用配置。

    Returns:
        文本的 Token 数。
    """
    return len(load_tokenizer(settings.tokenizer_path).encode(text, add_special_tokens=False).ids)


def validate_text(text, settings, limit=8000):
    """在请求前同时检查空文本、Token 预算和保守字节上限。

    Args:
        text: 含标题等上下文的最终输入文本。
        settings: 分词器配置。
        limit: 允许的最大 Token 数，默认 8000。

    Raises:
        EmbeddingError: 文本为空、超过 Token 预算或超过 8000 UTF-8 字节。
    """
    if not text.strip() or token_count(text, settings) > limit:
        raise EmbeddingError(f"Embedding 输入为空或超过 {limit} Token，需重新切片")
    # Service tokenizer revision is not published. UTF-8 bytes give a conservative
    # extra bound for Qwen's byte-level BPE; leave space below the 8192 API limit.
    if len(text.encode("utf-8")) > 8000:
        raise EmbeddingError("Embedding 输入超过保守字节上限，需缩小切片；不会静默截断")


def tokenizer_digest(settings):
    """计算分词器摘要以记录索引使用的词表版本。

    Args:
        settings: 包含本地 tokenizer 路径的配置。

    Returns:
        文件内容的 SHA256 十六进制字符串。
    """
    return hashlib.sha256(Path(settings.tokenizer_path).read_bytes()).hexdigest()


class QwenEmbedding:
    """封装千问向量请求的输入预算、批次顺序和实际用量。"""
    def __init__(self, settings, client=None, on_usage=None):
        """绑定向量配置、可选 HTTP 客户端和用量回调。

        Args:
            settings: 模型、维度、地域及凭证配置。
            client: 注入的测试或共享客户端；留空时请求期间自行创建。
            on_usage: 每个成功批次完成后的用量记录回调。
        """
        self.settings = settings
        self.client = client
        self.on_usage = on_usage

    def embed(self, texts, kind="document"):
        """分批请求向量，恢复输入顺序并校验和归一化结果。

        Args:
            texts: 待嵌入文本，每批最多发送 10 条。
            kind: document 用于索引，query 用于检索词。

        Returns:
            按输入顺序排列的单位向量列表，以及各成功批次的用量。

        Raises:
            EmbeddingError: 配置、输入、网络请求或返回向量校验失败。
        """
        settings = self.settings
        url = settings.embedding_url()
        if settings.missing():
            raise EmbeddingError("未配置 DASHSCOPE_API_KEY，无法建立向量索引")
        if kind not in {"query", "document"}:
            raise EmbeddingError("Embedding text_type 无效")
        for text in texts:
            validate_text(text, settings)
        vectors, usages = [], []
        with (
            httpx.Client(timeout=httpx.Timeout(6, connect=3))
            if self.client is None
            else _borrow(self.client) as client
        ):
            for start in range(0, len(texts), 10):
                batch = texts[start : start + 10]
                try:
                    response = client.post(
                        url,
                        headers={"Authorization": f"Bearer {settings.api_key}"},
                        json={
                            "model": settings.embedding_model,
                            "input": {"texts": batch},
                            "parameters": {
                                "text_type": kind,
                                "dimension": settings.embedding_dimension,
                                "output_type": "dense",
                            },
                        },
                    )
                    if not response.is_success:
                        # Don't echo provider messages, request headers or untrusted document text.
                        raise EmbeddingError(
                            f"千问 Embedding 请求失败（HTTP {response.status_code}），请检查模型权限、地域或限流"
                        )
                    payload = response.json()
                    rows = payload.get("output", {}).get("embeddings", [])
                    rows = sorted(rows, key=lambda x: x["text_index"])
                    if [r["text_index"] for r in rows] != list(range(len(batch))):
                        raise EmbeddingError("Embedding 返回数量或索引不完整，未保存索引")
                    for row in rows:
                        vector = row["embedding"]
                        if len(vector) != settings.embedding_dimension or not all(
                            isinstance(v, (float, int)) and math.isfinite(v) for v in vector
                        ):
                            raise EmbeddingError("Embedding 向量维度或数值异常，未保存索引")
                        norm = math.sqrt(sum(v * v for v in vector))
                        if not norm:
                            raise EmbeddingError("Embedding 返回零向量")
                        vectors.append([v / norm for v in vector])
                    usages.append(
                        {
                            "request_id": payload.get("request_id"),
                            "usage": payload.get("usage"),
                            "model": settings.embedding_model,
                            "text_type": kind,
                            "count": len(batch),
                        }
                    )
                    if self.on_usage:
                        self.on_usage(usages[-1])
                except (httpx.HTTPError, KeyError, TypeError) as exc:
                    raise EmbeddingError("Embedding 服务超时或返回格式错误，请重试；未切换其他模型") from exc
        return vectors, usages


class _borrow:
    """借用已有 HTTP 客户端而不取得其关闭权限。"""
    def __init__(self, client):
        """记录由调用方管理生命周期的客户端。

        Args:
            client: 退出上下文时不应关闭的 HTTP 客户端。
        """
        self.client = client

    def __enter__(self):
        """借用客户端而不创建新连接池。

        Returns:
            构造时提供的客户端。
        """
        return self.client

    def __exit__(self, *args):
        """保留客户端生命周期并让异常继续传播。

        Args:
            *args: 上下文协议提供的异常类型、实例和回溯。

        Returns:
            始终为 False，表示不吞掉异常。
        """
        return False
