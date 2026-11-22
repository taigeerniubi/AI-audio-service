"""定义实时语音和向量模型对业务层公开的最小接口。"""

from typing import Protocol


class RealtimeModel(Protocol):
    """约定实时模型连接的生命周期和 JSON 事件收发。"""

    async def open(self) -> None:
        """建立模型连接，失败时由调用方结束会话。"""
        ...

    async def send(self, event: dict) -> None:
        """向模型发送一条客户端事件。

        Args:
            event: 符合模型协议的事件字典。
        """
        ...

    async def receive(self) -> dict:
        """读取下一条模型事件。

        Returns:
            已解码的服务端事件字典。
        """
        ...

    async def close(self) -> None:
        """释放连接，允许在初始化未完成时调用。"""
        ...


class EmbeddingModel(Protocol):
    """约定按用途生成归一化向量并返回实际用量。"""

    def embed(self, texts: list[str], kind: str = "document") -> tuple[list[list[float]], list[dict]]:
        """生成文档或查询向量。

        Args:
            texts: 最终提交给模型的文本，包含需要计算预算的元数据。
            kind: document 表示入库，query 表示查询。

        Returns:
            与输入顺序一致的向量，以及各批次返回的用量。
        """
        ...
