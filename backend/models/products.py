"""映射产品档案和已解析资料的持久化表结构。"""

from sqlalchemy import JSON, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import Base


class ProductRecord(Base):
    """存储带版本号的产品档案。"""

    __tablename__ = "products"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    body: Mapped[dict] = mapped_column(JSON)


class DocumentRecord(Base):
    """存储资料原文和包含切片、向量、来源的索引。"""

    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    product_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    rag: Mapped[dict | None] = mapped_column(JSON(none_as_null=True))
