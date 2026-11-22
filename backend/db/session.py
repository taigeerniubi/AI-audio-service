"""创建 SQLite 数据库引擎并明确控制读写事务的生命周期。"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from backend.models import products, sessions  # noqa: F401
from backend.models.base import Base


class Database:
    """为每次操作创建独立连接，不跨线程共享 ORM Session。"""

    def __init__(self, path: str):
        """打开现有数据库，并补齐早期版本缺少的索引字段。

        Args:
            path: SQLite 文件路径；原有表和记录会被保留。
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            "sqlite:///" + str(Path(path).resolve()),
            connect_args={"timeout": 10, "check_same_thread": False},
            poolclass=NullPool,
            json_serializer=lambda value: json.dumps(value, ensure_ascii=False),
        )

        @event.listens_for(self.engine, "connect")
        def configure_connection(connection, _record):
            # 禁用 sqlite3 隐式事务，由 begin 事件统一管理 SELECT 与写操作。
            """关闭 sqlite3 隐式事务，以统一处理读事务和写事务。

            Args:
                connection: 新建的 DBAPI 连接。
                _record: SQLAlchemy 连接池事件记录，此处无需使用。
            """
            connection.isolation_level = None

        @event.listens_for(self.engine, "begin")
        def begin_transaction(connection):
            """按连接执行选项显式开启延迟事务或立即写事务。

            Args:
                connection: 正在开始事务的 SQLAlchemy 连接。
            """
            mode = connection.get_execution_options().get("sqlite_begin_mode", "DEFERRED")
            connection.exec_driver_sql("BEGIN IMMEDIATE" if mode == "IMMEDIATE" else "BEGIN")

        raw = self.engine.raw_connection()
        try:
            raw.execute("PRAGMA journal_mode=WAL")
        finally:
            raw.close()
        Base.metadata.create_all(self.engine)
        with self.engine.connect().execution_options(sqlite_begin_mode="IMMEDIATE") as connection:
            with connection.begin():
                columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(documents)")}
                if "rag" not in columns:
                    connection.exec_driver_sql("ALTER TABLE documents ADD COLUMN rag TEXT")

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[Session]:
        """在成功时提交、异常时回滚，并始终关闭连接。

        Args:
            write: 写事务预先取得锁，避免读取快照后升级写锁失败。

        Yields:
            仅在本次操作中有效的 ORM Session。
        """
        with self.engine.connect() as connection:
            if write:
                connection = connection.execution_options(sqlite_begin_mode="IMMEDIATE")
            with Session(bind=connection, expire_on_commit=False) as session:
                with session.begin():
                    yield session

    def close(self) -> None:
        """释放数据库引擎持有的资源。"""
        self.engine.dispose()
