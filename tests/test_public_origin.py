"""验证公开域名的 HTTP、WebSocket 来源限制及默认本地隔离。"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.core.config import Settings
from backend.core.origin import normalize_public_origin


@pytest.mark.parametrize("value", ["http://audio.example.com", "https://*.example.com", "https://user@audio.example.com", "https://audio.example.com/path", "https://audio.example.com?x=1", "https://audio.example.com:invalid"])
def test_invalid_public_origin_rejected(value):
    """拒绝会扩大来源范围或无法代表网站来源的配置。

    Args:
        value: 非法来源配置。
    """
    with pytest.raises(ValueError, match="PUBLIC_ORIGIN"):
        Settings(public_origin=value)


def test_public_http_origin_and_host(tmp_path):
    """允许明确配置的域名，拒绝伪造后缀、子域名和其他主机。

    Args:
        tmp_path: 隔离测试数据库的临时目录。
    """
    origin = "https://audio.example.com"
    assert normalize_public_origin(origin + "/") == origin
    app = create_app(Settings(database=str(tmp_path / "public.db"), public_origin=origin))
    with TestClient(app, base_url=origin) as client:
        assert client.get("/api/health", headers={"Origin": origin}).status_code == 200
        for other in ["https://audio.example.com.evil.test", "https://evil.audio.example.com", "http://audio.example.com", "https://audio.example.com:444"]:
            assert client.get("/api/health", headers={"Origin": other}).status_code == 403
        assert client.get("/api/health", headers={"Host": "evil.example.com"}).status_code == 400
        assert client.get("/api/health", headers={"Host": "localhost", "Origin": "http://localhost:5173"}).status_code == 200


def test_default_app_rejects_public_domain(tmp_path):
    """未开启公开配置时保持本机域名和来源限制。

    Args:
        tmp_path: 隔离测试数据库的临时目录。
    """
    with TestClient(create_app(Settings(database=str(tmp_path / "local.db")))) as client:
        assert client.get("/api/health", headers={"Host": "audio.example.com"}).status_code == 400
        assert client.get("/api/health", headers={"Origin": "https://audio.example.com"}).status_code == 403


def test_public_websocket_origin(tmp_path, monkeypatch):
    """通过真实 WebSocket 握手验证公开来源，拒绝陌生网页发起连接。

    Args:
        tmp_path: 隔离测试数据库的临时目录。
        monkeypatch: 替换模型会话为不产生费用的测试会话。
    """
    origin = "https://audio.example.com"
    app = create_app(Settings(database=str(tmp_path / "ws.db"), public_origin=origin))

    class OfflineSession:
        """以无模型的会话确认握手后能够正常交换消息。"""

        closed = True

        async def run(self):
            """发送确认消息并等待客户端结束。"""
            await self.socket.send_json({"type": "ready"})
            await self.socket.receive_json()

    def attach(session_id, socket):
        """创建无需数据库或模型的测试会话。

        Args:
            session_id: 测试会话标识。
            socket: 待接受的 WebSocket 连接。

        Returns:
            绑定当前连接的离线会话。
        """
        session = OfflineSession()
        session.socket = socket
        return session

    monkeypatch.setattr(app.state.runtime.sessions, "attach", attach)
    with TestClient(app, base_url=origin) as client:
        with client.websocket_connect("/ws/sessions/test", headers={"Origin": origin}) as socket:
            assert socket.receive_json() == {"type": "ready"}
            socket.send_json({"type": "stop"})
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws/sessions/test", headers={"Origin": "https://evil.example.com"}):
                pytest.fail("Untrusted origin accepted")
        assert error.value.code == 1008
