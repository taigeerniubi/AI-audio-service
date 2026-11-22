"""协调资料解析、向量索引、检索与索引状态展示。"""

import asyncio
import hashlib
import time
from collections import OrderedDict
from uuid import uuid4

from backend.core.errors import ServiceError
from backend.llm.qwen.embedding import EmbeddingError
from backend.services.knowledge.ingestion import build_index, parse_isolated
from backend.services.knowledge.retrieval import Knowledge
from backend.services.products import ProductService


def document_view(doc: dict) -> dict:
    """生成浏览器可见的资料概要，排除原文和向量载荷。

    Args:
        doc: 包含完整索引的内部文档记录。

    Returns:
        浏览器可见的文件、切片和索引状态。
    """
    rag = doc.get("rag") or {}
    return {
        "id": doc["id"], "name": doc["name"], "characters": len(doc["text"]),
        "index_status": "ready" if rag else "legacy_bm25", "chunks": len(rag.get("chunks", [])),
        "embedding_model": rag.get("model"), "warnings": rag.get("warnings", []),
        "max_chunk_tokens": max((chunk["tokens"] for chunk in rag.get("chunks", [])), default=0),
    }


class KnowledgeService:
    """将耗时解析与检索放在线程中，成功后一次性保存完整索引。"""

    def __init__(self, settings, store, llm_factory, parser=None):
        """绑定配置、持久化和可注入的解析器。

        Args:
            settings: 应用级 RAG 与模型配置。
            store: 当前应用的存储实例。
            llm_factory: 创建向量模型并记录用量的工厂。
            parser: 测试解析器；默认使用隔离进程。
        """
        self.settings, self.store, self.llm = settings, store, llm_factory
        self.products = ProductService(store)
        self.parse = parser or parse_isolated
        self.ingestion_lock = asyncio.Lock()
        self.processing = OrderedDict()

    def processing_status(self, product_id: str) -> dict | None:
        """返回指定产品最近一次处理状态，重启后为空。

        Args:
            product_id: 待查看处理状态的产品 ID。

        Returns:
            不包含原文或向量的阶段快照；尚未处理时为 None。
        """
        self.products.require(product_id)
        state = self.processing.get(product_id)
        return dict(state) if state else None

    def start_processing(self, product_id: str, name: str, upload_id: str | None) -> None:
        """在取得解析锁后登记本次操作，最多保留一百个产品的最近状态。

        Args:
            product_id: 当前处理的产品 ID。
            name: 资料文件名。
            upload_id: 客户端关联 ID；省略时由服务端生成。
        """
        self.processing[product_id] = {
            "id": upload_id or uuid4().hex, "name": name[:200], "stage": "validating",
            "started_at": time.time(),
        }
        self.processing.move_to_end(product_id)
        while len(self.processing) > 100:
            self.processing.popitem(last=False)

    def update_processing(self, product_id: str, stage: str, **details) -> None:
        """只在真实步骤边界更新阶段，不推测完成百分比。

        Args:
            product_id: 当前持有解析锁的产品 ID。
            stage: 当前步骤或完成、失败状态。
            **details: 切片数、成功资料概要或公开错误信息。
        """
        state = {**self.processing[product_id], "stage": stage, **details}
        if stage in {"completed", "failed"}:
            state["finished_at"] = time.time()
        self.processing[product_id] = state

    def embeddings(self, context: str):
        """创建将用量归属到指定产品的向量客户端。

        Args:
            context: 用量归属的产品 ID 或会话 ID。

        Returns:
            成功批次自动登记用量的向量客户端。
        """
        return self.llm.create_embedding(on_usage=lambda usage: self.store.log_embedding(context, usage))

    def documents(self, product_id: str) -> list[dict]:
        """读取资料概要，保持现有 API 的空列表行为。

        Args:
            product_id: 目标产品的唯一 ID。

        Returns:
            产品资料的公开概要列表。
        """
        return [document_view(doc) for doc in self.store.documents(product_id)]

    async def upload(self, product_id: str, name: str, data: bytes, upload_id: str | None = None) -> dict:
        """去重并解析上传资料，构建索引后保存。

        Args:
            product_id: 资料归属的产品。
            name: 经过路由清理的文件名。
            data: 限额内的完整文件内容。
            upload_id: 可选客户端关联 ID，区分迟到状态与新上传。

        Returns:
            已保存资料的公开概要。

        Raises:
            ServiceError: 产品不存在、重复上传、解析失败或模型调用失败。
        """
        self.products.require(product_id)
        if self.ingestion_lock.locked():
            raise ServiceError(429, "正在处理另一份资料，请完成后重试")
        try:
            async with self.ingestion_lock:
                self.start_processing(product_id, name, upload_id)
                existing = self.store.documents(product_id)
                digest = hashlib.sha256(data).hexdigest()
                if any((doc.get("rag") or {}).get("file_sha256") == digest for doc in existing):
                    raise ServiceError(409, "此文件已入库，无需重复上传")
                if len(existing) >= 20:
                    raise ServiceError(409, "每个产品最多 20 份资料")
                self.settings.embedding_url()
                if self.settings.missing():
                    raise EmbeddingError("请先配置 DASHSCOPE_API_KEY")
                self.update_processing(product_id, "parsing")
                parsed = await asyncio.to_thread(self.parse, name[:200], data, self.settings)
                self.update_processing(product_id, "embedding", chunks=len(parsed["chunks"]))
                rag = await asyncio.to_thread(build_index, parsed, self.settings, self.embeddings(product_id))
                rag["file_sha256"] = digest
                self.update_processing(product_id, "saving")
                doc = self.store.add_document(product_id, name[:200], parsed["text"], rag)
                result = document_view(doc)
                self.update_processing(product_id, "completed", document=result)
                return result
        except ServiceError as exc:
            self.update_processing(product_id, "failed", error=exc.message)
            raise
        except EmbeddingError as exc:
            self.update_processing(product_id, "failed", error=str(exc))
            raise ServiceError(502, str(exc)) from None
        except ValueError as exc:
            self.update_processing(product_id, "failed", error=str(exc))
            raise ServiceError(422, str(exc)) from None
        except asyncio.CancelledError:
            self.update_processing(product_id, "failed", error="处理已中断，请刷新资料列表确认后重试。")
            raise
        except Exception:
            self.update_processing(product_id, "failed", error="资料处理失败，请检查服务日志后重试。")
            raise

    async def reindex(self, product_id: str, document_id: str, upload_id: str | None = None) -> dict:
        """保留已有切片和页码，重建指定文档的向量索引。

        Args:
            product_id: 目标产品的唯一 ID。
            document_id: 目标资料的唯一 ID。
            upload_id: 可选客户端关联 ID。

        Returns:
            重建索引后的资料概要。
        """
        doc = next((doc for doc in self.store.documents(product_id) if doc["id"] == document_id), None)
        if not doc:
            raise ServiceError(404, "资料不存在")
        if self.ingestion_lock.locked():
            raise ServiceError(429, "正在处理另一份资料，请稍后重试")
        try:
            async with self.ingestion_lock:
                self.start_processing(product_id, doc["name"], upload_id)
                self.update_processing(product_id, "parsing")
                parsed = doc.get("rag") or await asyncio.to_thread(self.parse, doc["name"] + ".md", doc["text"].encode(), self.settings)
                self.update_processing(product_id, "embedding", chunks=len(parsed["chunks"]))
                rag = await asyncio.to_thread(build_index, parsed, self.settings, self.embeddings(product_id))
                self.update_processing(product_id, "saving")
                self.store.update_index(product_id, document_id, rag)
                result = document_view({**doc, "rag": rag})
                self.update_processing(product_id, "completed", document=result)
                return result
        except EmbeddingError as exc:
            self.update_processing(product_id, "failed", error=str(exc))
            raise ServiceError(502, str(exc)) from None
        except ValueError as exc:
            self.update_processing(product_id, "failed", error=str(exc))
            raise ServiceError(422, str(exc)) from None
        except asyncio.CancelledError:
            self.update_processing(product_id, "failed", error="索引重建已中断，请刷新确认后重试。")
            raise
        except Exception:
            self.update_processing(product_id, "failed", error="索引重建失败，请检查服务日志后重试。")
            raise

    async def search(self, product_id: str, query: str) -> dict:
        """执行当前产品的混合检索，返回来源、排名和向量用量。

        Args:
            product_id: 目标产品的唯一 ID。
            query: 客户问题或手工输入的检索词。

        Returns:
            证据片段、查询用量及融合参数。
        """
        self.products.require(product_id)

        def search():
            """在线程中构建当前产品检索器并返回证据和查询用量。

            Returns:
                混合检索片段、用量和排名融合配置。
            """
            kb = Knowledge(self.store.documents(product_id), self.settings, self.embeddings(product_id))
            return {"matches": kb.search(query), "embedding_usage": kb.last_usage, "rrf_k": self.settings.rrf_k, "reranker": "reciprocal_rank_fusion"}

        try:
            return await asyncio.wait_for(asyncio.to_thread(search), 10)
        except (ValueError, asyncio.TimeoutError) as exc:
            raise ServiceError(502, str(exc) or "检索超时，请重试") from None

    def delete(self, product_id: str, document_id: str) -> dict:
        """删除指定资料，不修改已经创建的会话快照。

        Args:
            product_id: 目标产品的唯一 ID。
            document_id: 目标资料的唯一 ID。

        Returns:
            删除成功标记；不存在时抛出业务异常。
        """
        if not self.store.delete_document(product_id, document_id):
            raise ServiceError(404, "资料不存在")
        state = self.processing.get(product_id)
        if state and state.get("document", {}).get("id") == document_id:
            self.processing.pop(product_id)
        return {"ok": True}
