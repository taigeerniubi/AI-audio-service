"""通过 SQLAlchemy 原子读写产品、知识快照、会话和业务结果。"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, literal_column, select, update
from sqlalchemy.dialects.sqlite import insert

from backend.db.session import Database
from backend.models.products import DocumentRecord, ProductRecord
from backend.models.sessions import EmbeddingCallRecord, SessionRecord, ToolActionRecord


def now() -> str:
    """返回带时区的 UTC 时间，供持久化记录使用。"""
    return datetime.now(timezone.utc).isoformat()


def document_dict(row: DocumentRecord) -> dict:
    """将资料表对象转换为脱离事务后仍可使用的数据。

    Args:
        row: 当前事务读取的文档 ORM 对象。

    Returns:
        包含原文和索引的普通字典。
    """
    return {key: getattr(row, key) for key in ("id", "product_id", "name", "text", "rag")}


class Store:
    """封装 SQL 查询，不持有跨请求或跨会话共享的事务。"""

    def __init__(self, path: str):
        """打开存储，并仅在数据库没有产品时加入默认产品配置。

        Args:
            path: 既有或待创建的 SQLite 文件路径。
        """
        self.path = path
        self.database = Database(path)
        if not self.products():
            self.save_product({
                "name": "知答 AI 客服",
                "audience": "有针对自己公司的产品感兴趣的人",
                "features": "基于产品资料回答常见问题；人工转接；对话记录；知识来源追溯。",
                "pricing": "电脑硬件按具体型号、数量和配置询价。当前资料未提供售价、套餐价、折扣或库存，暂无可直接引用的报价。涉及 CPU、主板、内存和 SSD 时，先检索对应资料；无价格依据则说明需人工确认。不得沿用客服订阅套餐、虚构金额或承诺优惠与现货。确认报价时应注明币种、含税及运费情况、报价有效期。",
                "limitations": "仅为作品集虚构产品；不承诺准确率、营业收益或未经确认的集成能力。",
                "opening": "您好，我是知答的 AI 产品顾问。您目前最想了解哪类产品？",
                "goal": "了解需求并预约产品演示，发送产品信息链接挂断电话后",
                "style": "自然、简洁，每次只问一个问题，会推销自家的产品。",
            })

    def close(self) -> None:
        """关闭数据库引擎。"""
        self.database.close()

    def products(self) -> list[dict]:
        """按入库顺序返回所有产品档案。"""
        with self.database.transaction() as db:
            return list(db.scalars(select(ProductRecord.body).order_by(literal_column("rowid"))))

    def product(self, product_id: str) -> dict | None:
        """读取指定产品，缺失时返回空值。

        Args:
            product_id: 目标产品的唯一 ID。

        Returns:
            产品字典；不存在时为空。
        """
        with self.database.transaction() as db:
            row = db.get(ProductRecord, product_id)
            return row.body if row else None

    def save_product(self, body: dict, product_id: str | None = None) -> dict:
        """保存产品并在同一写事务中递增版本号。

        Args:
            body: 已经过业务校验的产品字段。
            product_id: 更新目标；留空时创建新产品。

        Returns:
            含 ID 和最新版本号的产品档案。
        """
        with self.database.transaction(write=True) as db:
            row = db.get(ProductRecord, product_id) if product_id else None
            body = {**body, "id": product_id or uuid4().hex, "version": (row.body if row else {}).get("version", 0) + 1}
            if row:
                row.body = body
            else:
                db.add(ProductRecord(id=body["id"], body=body))
        return body

    def documents(self, product_id: str) -> list[dict]:
        """读取产品资料及完整索引，供后端检索或建立快照使用。

        Args:
            product_id: 目标产品的唯一 ID。

        Returns:
            包含完整正文和索引的文档列表。
        """
        with self.database.transaction() as db:
            rows = db.scalars(select(DocumentRecord).where(DocumentRecord.product_id == product_id))
            return [document_dict(row) for row in rows]

    def add_document(self, product_id: str, name: str, text: str, rag: dict | None = None) -> dict:
        """在同一写事务中检查数量上限并保存资料。

        Args:
            product_id: 所属产品 ID。
            name: 清理过路径的文件名。
            text: 解析后的文档内容。
            rag: 完整索引；旧文档可以不含向量。

        Returns:
            保存后的资料字典。

        Raises:
            ValueError: 产品已有 20 份资料。
        """
        with self.database.transaction(write=True) as db:
            count = db.scalar(select(func.count()).select_from(DocumentRecord).where(DocumentRecord.product_id == product_id))
            if count >= 20:
                raise ValueError("每个产品最多 20 份资料")
            row = DocumentRecord(id=uuid4().hex, product_id=product_id, name=name, text=text, rag=rag)
            db.add(row)
            return document_dict(row)

    def update_index(self, product_id: str, doc_id: str, rag: dict) -> None:
        """替换资料索引；资料在解析期间被删除时拒绝重新创建。

        Args:
            product_id: 目标产品的唯一 ID。
            doc_id: 目标资料的唯一 ID。
            rag: 已完整构建的正文、片段、向量及索引元信息。
        """
        with self.database.transaction(write=True) as db:
            result = db.execute(update(DocumentRecord).where(DocumentRecord.id == doc_id, DocumentRecord.product_id == product_id).values(rag=rag, text=rag["text"]))
            if not result.rowcount:
                raise ValueError("文档已被删除，未保存索引")

    def delete_document(self, product_id: str, doc_id: str) -> int:
        """删除产品下的指定资料，返回删除行数。

        Args:
            product_id: 目标产品的唯一 ID。
            doc_id: 目标资料的唯一 ID。

        Returns:
            实际删除的记录行数。
        """
        with self.database.transaction(write=True) as db:
            return db.execute(delete(DocumentRecord).where(DocumentRecord.id == doc_id, DocumentRecord.product_id == product_id)).rowcount

    def create_session(self, product_id: str, options: dict | None = None) -> str:
        """在一个事务内固定产品和全部资料，避免版本混杂。

        Args:
            product_id: 创建会话时选定的产品 ID。
            options: 同时固定的音色、轮次模式和声学静音时长。

        Returns:
            新会话 ID。

        Raises:
            KeyError: 指定产品不存在。
        """
        with self.database.transaction(write=True) as db:
            product = db.get(ProductRecord, product_id)
            if not product:
                raise KeyError(product_id)
            documents = db.scalars(select(DocumentRecord).where(DocumentRecord.product_id == product_id))
            snapshot = {"product": product.body, "documents": [document_dict(row) for row in documents]}
            snapshot["session_options"] = dict(options or {})
            session_id = uuid4().hex
            db.add(SessionRecord(id=session_id, product_id=product_id, snapshot=snapshot, status="created", created_at=now(), report=None))
            return session_id

    def session(self, session_id: str) -> dict | None:
        """返回会话、创建时快照与可选报告。

        Args:
            session_id: 目标语音会话的唯一 ID。

        Returns:
            会话状态、固定快照和报告；不存在时为空。
        """
        with self.database.transaction() as db:
            row = db.get(SessionRecord, session_id)
            return {key: getattr(row, key) for key in ("id", "product_id", "snapshot", "status", "created_at", "report")} if row else None

    def claim_session(self, session_id: str) -> bool:
        """原子认领待启动会话，重复连接不能取得同一会话。

        Args:
            session_id: 目标语音会话的唯一 ID。

        Returns:
            仅本次从 created 更新为 active 时为 True。
        """
        with self.database.transaction(write=True) as db:
            result = db.execute(update(SessionRecord).where(SessionRecord.id == session_id, SessionRecord.status == "created").values(status="active"))
            return result.rowcount == 1

    def finish(self, session_id: str, report: dict) -> None:
        """结束会话并保存最终报告。

        Args:
            session_id: 目标语音会话的唯一 ID。
            report: 会话结束后需要保存的完整报告。
        """
        with self.database.transaction(write=True) as db:
            db.execute(update(SessionRecord).where(SessionRecord.id == session_id).values(status="ended", report=report))

    def sessions(self) -> list[dict]:
        """返回最近 100 个会话的概要。"""
        with self.database.transaction() as db:
            query = select(SessionRecord.id, SessionRecord.product_id, SessionRecord.status, SessionRecord.created_at).order_by(SessionRecord.created_at.desc()).limit(100)
            return [dict(row) for row in db.execute(query).mappings()]

    def action(self, session_id: str, call_id: str, name: str, arguments: dict, result: dict) -> dict:
        """幂等提交工具结果，重复调用返回第一次实际保存的结果。

        Args:
            session_id: 所属语音会话。
            call_id: 模型给出的工具调用唯一标识。
            name: 工具名称。
            arguments: 已校验的参数。
            result: 业务执行结果。

        Returns:
            数据库中真实保存的结果。
        """
        with self.database.transaction(write=True) as db:
            db.execute(insert(ToolActionRecord).values(session_id=session_id, call_id=call_id, name=name, arguments=arguments, result=result).on_conflict_do_nothing(index_elements=["session_id", "call_id"]))
            return db.get(ToolActionRecord, (session_id, call_id)).result

    def actions(self, session_id: str) -> list[dict]:
        """读取已提交工具记录，不包含尚未提交的异步准备任务。

        Args:
            session_id: 目标语音会话的唯一 ID。

        Returns:
            已落库的工具名称、参数及真实结果。
        """
        with self.database.transaction() as db:
            rows = db.scalars(select(ToolActionRecord).where(ToolActionRecord.session_id == session_id))
            return [{key: getattr(row, key) for key in ("session_id", "call_id", "name", "arguments", "result")} for row in rows]

    def log_embedding(self, context: str, usage: dict) -> None:
        """保存一个成功 Embedding 请求返回的用量。

        Args:
            context: 用量归属的产品 ID 或会话 ID。
            usage: 成功请求返回的实际用量与模型元信息。
        """
        with self.database.transaction(write=True) as db:
            db.add(EmbeddingCallRecord(context=context, created_at=now(), body=usage))

    def embedding_usage(self, context: str) -> list[dict]:
        """按请求顺序读取指定产品或会话的向量用量。

        Args:
            context: 用量归属的产品 ID 或会话 ID。

        Returns:
            按数据库记录顺序排列的向量调用用量。
        """
        with self.database.transaction() as db:
            return list(db.scalars(select(EmbeddingCallRecord.body).where(EmbeddingCallRecord.context == context).order_by(EmbeddingCallRecord.id)))
