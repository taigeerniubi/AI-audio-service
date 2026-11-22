"""封装千问实时语音 WebSocket 的连接和事件收发。"""
import asyncio
import json

from websockets.asyncio.client import connect


class QwenTransport:
    """为每个语音会话维护独立的千问实时连接。"""
    def __init__(self, settings):
        """保存模型配置，连接在 open 中建立。

        Args:
            settings: 经过应用管理的地域、模型及后端凭证配置。
        """
        self.settings = settings
        self.socket = None

    async def open(self):
        """建立独立千问 WebSocket，并限制连接时间和接收缓冲。"""
        self.socket = await connect(
            self.settings.url(),
            additional_headers={"Authorization": f"Bearer {self.settings.api_key}"},
            open_timeout=10,
            max_size=2**22,
            max_queue=64,
            close_timeout=2,
        )

    async def send(self, event):
        """将事件编码为 JSON，在两秒发送期限内提交。

        Args:
            event: 千问客户端协议事件，不在此处输出到日志。
        """
        await asyncio.wait_for(self.socket.send(json.dumps(event, ensure_ascii=False)), timeout=2)

    async def receive(self):
        """等待并解析下一条千问服务端 JSON 事件。

        Returns:
            服务端事件字典；连接或解码错误交给会话层处理。
        """
        return json.loads(await self.socket.recv())

    async def close(self):
        """关闭已建立的模型连接，允许在连接尚未成功时调用。"""
        if self.socket:
            await self.socket.close()
