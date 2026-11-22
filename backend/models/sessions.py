"""映射会话快照、工具提交结果和向量模型用量记录。"""

from sqlalchemy import JSON, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class SessionRecord(Base):
    """保存创建时的产品与知识快照及结束后的会话报告。"""

    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    product_id: Mapped[str] = mapped_column(Text)
    snapshot: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    report: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))


class ToolActionRecord(Base):
    """使用会话与调用 ID 的联合主键确保工具提交幂等。"""

    __tablename__ = "actions"
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    arguments: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)


class EmbeddingCallRecord(Base):
    """记录每个成功请求的实际向量模型用量。"""

    __tablename__ = "embedding_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    context: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text)
    body: Mapped[dict] = mapped_column(JSON)
