"""在产品快照内执行关键词和向量召回并融合排名。"""

import hashlib
import re

import jieba
import numpy as np
from rank_bm25 import BM25Plus

from backend.llm.factory import LLMFactory

jieba.setLogLevel(30)


def tokenize(text):
    """用 jieba 分词并保留可检索的字母、数字和汉字。

    Args:
        text: 文档片段或检索问题。

    Returns:
        已转为小写的词项列表。
    """
    return [t.lower() for t in jieba.lcut(text) if re.search(r"[\w\u4e00-\u9fff]", t)]


def rrf(rankings, k=60):
    """融合多路有序候选，使用从 1 开始的排名计算倒数分数。

    Args:
        rankings: 各召回通道按相关性降序排列的候选 ID 列表。
        k: 平滑常数；每个通道贡献 1 / (k + rank)，缺席贡献零。

    Returns:
        融合后的候选顺序和各候选总分；同通道重复 ID 只计一次。
    """
    scores = {}
    for ranking in rankings:
        for rank, item in enumerate(dict.fromkeys(ranking), 1):
            scores[item] = scores.get(item, 0.0) + 1 / (k + rank)
    return sorted(scores, key=lambda i: (-scores[i], str(i))), scores


class Knowledge:
    """为固定产品资料维护关键词索引、归一化向量和查询用量。"""
    def __init__(self, documents, settings=None, embedder=None):
        """从固定文档集合建立会话专属 BM25 和向量检索结构。

        Args:
            documents: 含正文及可选向量索引的产品资料快照。
            settings: 用于核对模型、地域、维度和 RRF 参数的配置。
            embedder: 查询向量客户端；留空时由配置创建。

        Raises:
            ValueError: 资料向量配置过期或片段与向量数量不一致。
        """
        self.settings = settings
        self.embedder = embedder or (LLMFactory(settings).create_embedding() if settings else None)
        self.chunks, vectors, self.vector_indices = [], [], []
        self.has_legacy = False
        self.last_usage = []
        for doc in documents:
            rag = doc.get("rag")
            if rag:
                if settings and (
                    rag["model"] != settings.embedding_model
                    or rag["dimension"] != settings.embedding_dimension
                    or rag.get("region", settings.region) != settings.region
                ):
                    raise ValueError(f"{doc['name']} 的向量配置已过期，请重建索引")
                if len(rag["chunks"]) != len(rag["vectors"]):
                    raise ValueError("知识库索引不完整，请重建")
                for chunk, vector in zip(rag["chunks"], rag["vectors"], strict=True):
                    self.vector_indices.append(len(self.chunks))
                    self.chunks.append({**chunk, "document_id": doc["id"], "source": doc["name"]})
                    vectors.append(vector)
            else:
                # Existing docs remain readable; UI marks these as legacy BM25.
                self.has_legacy = True
                for paragraph in re.split(r"\n\s*\n|(?=^#{1,6} )", doc["text"], flags=re.M):
                    for start in range(0, len(paragraph), 600):
                        text = paragraph[start : start + 700].strip()
                        if text:
                            self.chunks.append(
                                {
                                    "source": doc["name"],
                                    "document_id": doc["id"],
                                    "text": text,
                                    "chunk_id": hashlib.sha256(text.encode()).hexdigest()[:24],
                                }
                            )
        self.tokens = [tokenize(c["text"]) or [""] for c in self.chunks]
        self.index = BM25Plus(self.tokens) if self.tokens else None
        self.matrix = np.array(vectors, dtype=np.float32) if vectors else None

    def search(self, query):
        """分别召回关键词和语义候选，经 RRF 融合去重后返回最多四段。

        Args:
            query: 客户问题或模型生成的检索词。

        Returns:
            含原文、来源、页码及两路排名的证据列表；空库或无匹配返回空列表。

        Raises:
            ValueError: 向量索引存在但查询客户端不可用。
        """
        self.last_usage = []
        if not self.index:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = self.index.get_scores(tokens)
        matches = [i for i in range(len(scores)) if set(tokens) & set(self.tokens[i])]
        lexical = sorted(matches, key=lambda i: (-scores[i], i))[:20]
        dense, cosines = [], {}
        if self.matrix is not None:
            if not self.embedder:
                raise ValueError("向量检索尚未配置")
            vectors, self.last_usage = self.embedder.embed([query], kind="query")
            values = self.matrix @ np.array(vectors[0], dtype=np.float32)
            cosines = dict(zip(self.vector_indices, (float(v) for v in values), strict=True))
            # A permissive relevance floor, NOT a confidence score.
            dense = sorted((i for i in cosines if cosines[i] >= 0.2), key=lambda i: (-cosines[i], i))[:20]
        ranking, fused = rrf([lexical, dense], k=self.settings.rrf_k if self.settings else 60)
        mode = "hybrid_rrf" if self.matrix is not None else "bm25_legacy"
        if self.has_legacy and self.matrix is not None:
            mode += "_with_legacy"
        seen, result = set(), []
        for i in ranking:
            chunk = self.chunks[i]
            key = re.sub(r"\s+", "", chunk["text"])
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    **chunk,
                    "score": round(fused[i], 8),
                    "retrieval_mode": mode,
                    "bm25_rank": lexical.index(i) + 1 if i in lexical else None,
                    "vector_rank": dense.index(i) + 1 if i in dense else None,
                    "bm25_score": round(float(scores[i]), 4) if i in lexical else None,
                    "cosine_similarity": round(cosines[i], 4) if i in cosines else None,
                }
            )
            if len(result) == 4:
                break
        return result
