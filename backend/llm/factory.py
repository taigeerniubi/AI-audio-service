"""根据应用配置集中创建实时语音和 Embedding 模型客户端。"""

import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from backend.core.config import Settings
from backend.llm.protocols import EmbeddingModel, RealtimeModel
from backend.llm.qwen.embedding import QwenEmbedding, load_tokenizer
from backend.llm.qwen.realtime import QwenTransport


class LLMFactory:
    """管理模型构造方式，不在失败时切换模型或复用实时连接。"""

    def __init__(self, settings: Settings, realtime_builder=None, embedding_model=None):
        """记录模型配置和可选的测试适配器。

        Args:
            settings: 后端配置，包含模型名、地域和凭证。
            realtime_builder: 测试用无参数构造函数，每次返回独立连接。
            embedding_model: 测试用向量模型；生产环境留空。
        """
        self.settings = settings
        self.realtime_builder = realtime_builder
        self.embedding_model = embedding_model
        self._embedding_client: httpx.Client | None = None

    def start(self) -> None:
        """预加载本地词表并初始化应用级连接池，不发送任何云模型请求。"""
        if self.embedding_model is not None:
            return
        if Path(self.settings.tokenizer_path).is_file():
            try:
                load_tokenizer(self.settings.tokenizer_path)
            except Exception as exc:
                logging.getLogger(__name__).warning("RAG 分词器预热失败（%s），请检查本地文件", type(exc).__name__)
        if self._embedding_client is None:
            self._embedding_client = httpx.Client(
                timeout=httpx.Timeout(6, connect=3),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4, keepalive_expiry=60),
            )

    def close(self) -> None:
        """应用停止并排空请求后释放连接池，独立脚本仍使用每次调用的生命周期。"""
        if self._embedding_client is not None:
            self._embedding_client.close()
            self._embedding_client = None

    def create_realtime(self) -> RealtimeModel:
        """创建当前配置的实时语音客户端。

        Returns:
            尚未连接的客户端，每个会话独享一个实例。
        """
        if self.realtime_builder is not None:
            return self.realtime_builder()
        return QwenTransport(self.settings)

    def create_embedding(self, on_usage: Callable[[dict], None] | None = None) -> EmbeddingModel:
        """创建当前配置的文本向量客户端。

        Args:
            on_usage: 每个成功批次完成后记录用量的回调。

        Returns:
            支持 document/query 用途的向量客户端。
        """
        if self.embedding_model is not None:
            return self.embedding_model
        return QwenEmbedding(self.settings, client=self._embedding_client, on_usage=on_usage)
