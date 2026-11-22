"""复现异常断线、连接心跳失效和模型连接清理并检查脱敏诊断。"""

import asyncio
import json

import pytest
from test_core import Browser, Clock, Transport, run
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from backend.core.config import Settings
from backend.db.store import Store
from backend.services.realtime.session import LiveSession


@pytest.fixture
def live(tmp_path):
    """创建使用假传输和独立数据库的会话，不调用云模型。"""
    store = Store(str(tmp_path / "connections.db"))
    sid = store.create_session(store.products()[0]["id"])
    session = LiveSession(
        sid, store.session(sid)["snapshot"], Settings(), store, Browser(), Transport(), Clock()
    )
    session.ready = True
    yield session
    store.close()


def test_ping_replies_without_resetting_silence_or_calling_model(live):
    """连接心跳不是用户发言，不能重置八秒静音或触发模型响应。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：连接心跳不是用户发言，不能重置八秒静音或触发模型响应。"""
        live.idle_since = live.clock() - 7
        await live.on_client({"type": "ping", "sequence": 3})
        assert live.browser.events[-1] == {"type": "pong", "session_id": live.id, "sequence": 3}
        assert live.idle_since == live.clock() - 7
        assert not live.transport.events

    run(scenario())


@pytest.mark.parametrize("code,reason", [(1006, "client_disconnected"), (1000, "client_closed")])
def test_browser_disconnect_keeps_code_without_raw_reason(live, code, reason):
    """正常关闭与异常断线分别记录，关闭原因中的任意文本不写日志。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
        code: 模拟浏览器断开的 WebSocket 关闭码。
        reason: 此关闭码对应的预期会话结束原因。
    """

    async def receive():
        """返回包含敏感关闭原因的假断线事件，以检查日志脱敏。

        Returns:
            模拟的 websocket.disconnect 事件。
        """
        return {"type": "websocket.disconnect", "code": code, "reason": "secret-in-close-reason"}

    async def scenario():
        """按事件顺序执行异步场景：正常关闭与异常断线分别记录，关闭原因中的任意文本不写日志。"""
        live.browser.receive = receive
        await live.read_browser()
        assert await live.queue.get() == ("close", reason)
        diagnostic = next(e for e in live.events if e["type"] == "connection_closed")
        assert diagnostic["side"] == "browser" and diagnostic["close_code"] == code
        assert "secret-in-close-reason" not in json.dumps(live.report())

    run(scenario())


def test_missing_client_activity_exits_even_if_playback_ack_never_arrives(live):
    """没有播放结束确认时，客户端失联不能一直等待到十分钟上限。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：没有播放结束确认时，客户端失联不能一直等待到十分钟上限。"""
        live.last_client_activity = live.clock()
        live.clock.value += 15.1
        await live.tick()
        assert live.closed and live.reason == "client_timeout"
        assert any(e["type"] == "connection_timeout" and e["side"] == "browser" for e in live.events)

    run(scenario())


def test_recent_client_activity_does_not_timeout(live):
    """普通音频或心跳活跃时不将用户静音误判成连接断开。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：普通音频或心跳活跃时不将用户静音误判成连接断开。"""
        live.clock.value += 20
        live.last_client_activity = live.clock() - 2
        await live.tick()
        assert not live.closed

    run(scenario())


def test_model_disconnect_closes_tasks_and_saves_diagnostic(live):
    """真实会话生命周期在模型收包异常后退出、释放任务且不记录凭证文本。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    class BrokenTransport(Transport):
        """只模拟连接异常，不使用真实网络。"""

        closed = False

        async def open(self):
            """模拟已建立连接，不访问网络。"""
            pass

        async def receive(self):
            """抛出带敏感原因的连接异常，驱动会话清理路径。"""
            raise ConnectionClosedError(Close(1011, "Bearer secret-in-close-reason"), None, None)

        async def close(self):
            """记录假模型连接已经关闭。"""
            self.closed = True

    async def scenario():
        """按事件顺序执行异步场景：真实会话生命周期在模型收包异常后退出、释放任务且不记录凭证文本。"""
        transport = live.transport = BrokenTransport()
        browser_closed = []

        async def receive():
            """持续等待直到任务被取消，模拟仍在读消息的浏览器。"""
            await asyncio.Future()

        async def close():
            """记录浏览器关闭动作，供资源释放断言检查。"""
            browser_closed.append(True)

        live.browser.receive, live.browser.close = receive, close
        await asyncio.wait_for(live.run(), 3)
        assert live.closed and live.reason == "model_disconnected"
        assert transport.closed and browser_closed and not live.tasks
        report = live.store.session(live.id)["report"]
        diagnostic = next(e for e in report["events"] if e["type"] == "connection_closed")
        assert diagnostic["side"] == "model" and diagnostic["close_code"] == 1011
        assert diagnostic["category"] == "ConnectionClosedError"
        assert "secret-in-close-reason" not in json.dumps(report)
        assert any(e["type"] == "error" for e in live.browser.events)

    run(scenario())
