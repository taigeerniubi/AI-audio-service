"""定义数据库表共享的 SQLAlchemy 声明式基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """集中注册 ORM 表结构，供建表和迁移检查使用。"""
