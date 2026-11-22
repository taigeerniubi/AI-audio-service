"""回归验证会话隔离、打断、工具提交、静音和 HTTP 接口行为。"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from backend.core.config import Settings
from backend.db.store import Store
from backend.schemas import ProductInput
from backend.services.knowledge.retrieval import Knowledge
from backend.services.realtime.metrics import estimate_cost, percentiles
from backend.services.realtime.session import LiveSession
from backend.services.sales_tools import prepare_result


class Clock:
    def __init__(self):
        """初始化可手动推进的单调时钟。"""
        self.value = 100.0

    def __call__(self):
        """读取测试控制的当前时间。

        Returns:
            当前模拟秒数。
        """
        return self.value


class Browser:
    def __init__(self):
        """创建浏览器事件收集器。"""
        self.events = []

    async def send_json(self, event):
        """收集服务端发给浏览器的事件，不建立真实连接。

        Args:
            event: 待保存的浏览器协议事件。
        """
        self.events.append(event)


class Transport:
    def __init__(self):
        """创建模型客户端事件收集器。"""
        self.events = []

    async def send(self, event):
        """保存发给假模型的事件供断言检查。

        Args:
            event: 待记录的模型客户端协议事件。
        """
        self.events.append(event)


@pytest.fixture
def store(tmp_path):
    """创建带演示产品的临时存储。

    Args:
        tmp_path: pytest 为本测试分配的临时目录。

    Returns:
        独立 Store 实例。
    """
    return Store(str(tmp_path / "test.db"))


@pytest.fixture
def live(store):
    """创建使用假传输和假时钟的会话。

    Args:
        store: 使用临时 SQLite 文件的存储夹具。

    Returns:
        尚未连接真实模型的 LiveSession。
    """
    sid = store.create_session(store.products()[0]["id"])
    return LiveSession(
        sid, store.session(sid)["snapshot"], Settings(), store, Browser(), Transport(), Clock()
    )


def run(coroutine):
    """在同步测试内运行一个异步场景。

    Args:
        coroutine: 待验证的场景协程。

    Returns:
        协程的返回结果。
    """
    return asyncio.run(coroutine)


def test_cancelled_response_does_not_delete_phantom_item(live):
    """取消响应的空转录和结束事件不能证明助手消息已进入云端历史。

    Args:
        live: 使用假模型的会话夹具。
    """
    async def scenario():
        """重放真实 500 ms 句中停顿触发的空消息取消事件。"""
        await live.on_model({"type": "response.created", "response": {"id": "cancelled"}})
        await live.on_model({"type": "response.audio_transcript.done", "response_id": "cancelled",
                             "item_id": "ghost", "transcript": ""})
        await live.on_model({"type": "response.output_item.done", "response_id": "cancelled",
                             "item": {"id": "ghost", "type": "message", "role": "assistant"}})
        await live.on_model({"type": "response.done", "response": {"id": "cancelled", "status": "cancelled",
                             "output": [{"id": "ghost", "type": "message"}]}})
        assert not any(row.get("type") == "conversation.item.delete" for row in live.transport.events)
        assert not live.pending_deletes and not live.closed
        # 迟到的真实创建确认到达后，仍须删除被打断消息，不能漏留上下文。
        await live.on_model({"type": "conversation.item.created", "item": {
            "id": "ghost", "type": "message", "role": "assistant",
        }})
        assert "ghost" in live.pending_deletes

    run(scenario())


def test_missing_requested_delete_is_idempotent_but_other_errors_fail(live):
    """仅将已请求删除且明确不存在的目标视为成功，其他模型错误仍结束会话。

    Args:
        live: 使用假模型的会话夹具。
    """
    async def scenario():
        """检查删除确认竞争的精确匹配与错误隔离。"""
        await live.delete_context_item("gone")
        await live.on_model({"type": "error", "error": {
            "code": "invalid_value", "param": "conversation.item.delete",
            "message": "Cannot find item with id: gone",
        }})
        assert not live.closed and not live.pending_deletes
        await live.on_model({"type": "error", "error": {"code": "invalid_api_key"}})
        assert live.closed

    run(scenario())


async def response(live, rid="r1", audio=True):
    """向会话注入响应创建、消息项及可选首音频事件。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
        rid: 本次假响应 ID。
        audio: 是否追加音频增量事件。
    """
    await live.on_model({"type": "response.created", "response": {"id": rid}})
    await live.on_model(
        {
            "type": "response.output_item.added",
            "response_id": rid,
            "item": {"id": f"i{rid}", "type": "message"},
        }
    )
    if audio:
        await live.on_model({"type": "response.audio.delta", "response_id": rid, "delta": "AAA="})


async def done(live, rid="r1", status="completed"):
    """向会话注入指定响应的完成状态。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
        rid: 假响应 ID。
        status: 模型完成状态，如 completed 或 cancelled。
    """
    await live.on_model({"type": "response.done", "response": {"id": rid, "status": status, "output": []}})


def test_snapshot_isolates_product_and_documents(store):
    """验证编辑产品或删除资料不会改变已创建会话的快照。

    Args:
        store: 使用临时 SQLite 文件的存储夹具。
    """
    p = store.products()[0]
    doc = store.add_document(p["id"], "old.md", "原版报价")
    sid = store.create_session(p["id"])
    store.save_product({**p, "pricing": "新版报价"}, p["id"])
    store.delete_document(p["id"], doc["id"])
    snapshot = store.session(sid)["snapshot"]
    assert snapshot["product"]["pricing"] == p["pricing"]
    assert snapshot["documents"][0]["text"] == "原版报价"


def test_document_limit(store):
    """验证单产品最多保存二十份资料。

    Args:
        store: 使用临时 SQLite 文件的存储夹具。
    """
    pid = store.products()[0]["id"]
    for n in range(20):
        store.add_document(pid, str(n), "内容")
    with pytest.raises(ValueError):
        store.add_document(pid, "21", "内容")


def test_search_chinese_and_empty():
    """验证中文关键词可命中，空库或无关词返回空列表。"""
    kb = Knowledge(
        [
            {"id": "1", "name": "套餐.md", "text": "团队套餐价格为每月899元，含10名成员。"},
            {"id": "2", "name": "安全.md", "text": "隐私数据存储和权限管理。"},
        ]
    )
    assert kb.search("套餐价格")[0]["source"] == "套餐.md"
    assert not kb.search("火星宇宙飞船")
    assert not Knowledge([]).search("价格")


def test_plus_cost_and_unknown_modality():
    """验证已配置的语音计费分项及缺少模态数据时的空估算。"""
    usage = {
        "input_tokens_details": {"text_tokens": 1000, "audio_tokens": 1000},
        "output_tokens_details": {"text_tokens": 1000, "audio_tokens": 1000},
    }
    assert estimate_cost([usage], "qwen-audio-3.1-realtime-plus", "cn-beijing") == 0.195
    assert estimate_cost([usage], "qwen-audio-3.1-realtime-flash", "cn-beijing") == 0.0195
    assert estimate_cost([{"input_tokens": 10}], "qwen-audio-3.1-realtime-plus", "cn-beijing") is None
    assert estimate_cost([usage], "unknown", "cn-beijing") is None
    assert percentiles([]) == {"count": 0, "p50": None, "p95": None}
    assert percentiles([None, 1, 5, -3, float("nan")])["p95"] == 5


def test_confirmation_requires_actual_final_user_quote():
    """验证预约只接受最终用户转录中的明确同意且排除拒绝。"""
    args = {"when": "明天下午两点", "contact": "", "confirmation_quote": "好的，就这个时间"}
    assert "error" in prepare_result("schedule_demo", args, [])
    turn = {"role": "user", "final": True, "text": args["confirmation_quote"]}
    assert prepare_result("schedule_demo", args, [turn])["local_only"]
    for quote in ["不要预约", "好的，不过不要安排", "这个价格好贵"]:
        assert "error" in prepare_result(
            "schedule_demo", {**args, "confirmation_quote": quote}, [{**turn, "text": quote}]
        )


def test_interrupt_drops_late_audio_and_repairs_context(live):
    """验证打断丢弃迟到音频并清理未听完的助手上下文。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证打断丢弃迟到音频并清理未听完的助手上下文。"""
        await response(live)
        await live.on_client({"type": "interrupt"})
        count = sum(e["type"] == "audio" for e in live.browser.events)
        await live.on_model({"type": "response.audio.delta", "response_id": "r1", "delta": "AAA="})
        assert sum(e["type"] == "audio" for e in live.browser.events) == count
        await done(live, status="cancelled")
        assert "ir1" in live.pending_deletes
        assert not any(e["type"] == "conversation.item.create" for e in live.transport.events)
        await live.on_model({"type": "conversation.item.deleted", "item_id": "ir1"})
        assert not live.repair_marker
        assert sum(e["type"] == "response.cancel" for e in live.transport.events) == 1

    run(scenario())


def test_speech_does_not_send_duplicate_provider_cancel(live):
    """验证语音打断不重复取消已由智能轮次取消的生成。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证语音打断不重复取消已由智能轮次取消的生成。"""
        await response(live)
        await live.on_model({"type": "input_audio_buffer.speech_started", "audio_start_ms": 20})
        await done(live, status="cancelled")
        assert not any(e["type"] == "response.cancel" for e in live.transport.events)
        assert live.responses["r1"].cancelled

    run(scenario())


def test_cancel_before_any_audio_still_cancels_tools(live):
    """验证首音频尚未到达时取消也能阻止工具写入。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证首音频尚未到达时取消也能阻止工具写入。"""
        await response(live, audio=False)
        await done(live, status="cancelled")
        assert live.responses["r1"].cancelled
        assert "ir1" in live.pending_deletes

    run(scenario())


def test_late_new_response_waits_for_context_repair(live):
    """验证迟到的新响应须等历史删除和修正完成后再继续。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证迟到的新响应须等历史删除和修正完成后再继续。"""
        await response(live)
        await live.interrupt("button")
        await done(live, status="cancelled")
        await response(live, "r2")
        assert live.responses["r2"].cancelled
        assert not any(e["type"] == "audio" and e["response_id"] == "r2" for e in live.browser.events)
        await done(live, "r2", "cancelled")
        await live.on_model({"type": "conversation.item.deleted", "item_id": "ir1"})
        await live.on_model({"type": "conversation.item.deleted", "item_id": "ir2"})
        assert any(e["type"] == "response.create" for e in live.transport.events)

    run(scenario())


def test_three_interruptions_and_next_response(live):
    """验证连续三次打断后仍可在同一会话正常回答。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证连续三次打断后仍可在同一会话正常回答。"""
        for n in range(3):
            rid = f"r{n}"
            await response(live, rid)
            await live.interrupt("button")
            await done(live, rid, "cancelled")
            await live.on_model({"type": "conversation.item.deleted", "item_id": f"i{rid}"})
        await response(live, "last")
        assert live.browser.events[-1]["type"] == "audio"
        assert live.browser.events[-1]["epoch"] == 3

    run(scenario())


def test_cancelled_tool_has_no_side_effect_and_duplicates_are_idempotent(live):
    """验证取消轮次不写业务记录，重复调用不会重复预约。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证取消轮次不写业务记录，重复调用不会重复预约。"""
        await response(live, audio=False)
        args = {"needs": "客服", "level": "high", "follow_up": "演示"}
        call = {
            "name": "record_interest",
            "arguments": json.dumps(args),
            "response_id": "r1",
            "call_id": "c1",
        }
        await live.commit_tool({"call": call, "args": args})
        await live.commit_tool({"call": call, "args": args})
        assert len(live.store.actions(live.id)) == 1
        await live.interrupt("button")
        await live.commit_tool({"call": {**call, "call_id": "c2"}, "args": args})
        assert len(live.store.actions(live.id)) == 1
        assert live.store.actions(live.id)[0]["result"]["ok"]

    run(scenario())


def test_idle_only_after_playback_and_mute_pauses(live):
    """验证播放结束后才累计静音，麦克风静音暂停提醒。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证播放结束后才累计静音，麦克风静音暂停提醒。"""
        live.ready = True
        await response(live)
        await done(live)
        live.clock.value += 30
        await live.tick()
        assert not live.reminded
        await live.on_client({"type": "playback_complete", "response_id": "r1", "epoch": 0})
        await live.tick()
        live.clock.value += 9
        await live.tick()
        assert live.reminders.pending and not live.requested
        item_id = live.reminders.pending.item_id
        await live.on_model({"type": "conversation.item.created", "item": {"id": item_id}})
        assert live.reminded and live.requested
        await response(live, "reminder")
        await done(live, "reminder")
        await live.on_model({"type": "conversation.item.deleted", "item_id": item_id})
        await live.on_client({"type": "playback_complete", "response_id": "reminder", "epoch": 0})
        await live.on_client({"type": "mute", "muted": True})
        live.clock.value += 30
        await live.tick()
        assert not live.closed
        await live.on_client({"type": "mute", "muted": False})
        await live.tick()
        live.clock.value += 21
        await live.tick()
        assert live.reason == "idle_timeout"

    run(scenario())


def test_stale_playback_ack_does_not_complete_response(live):
    """验证旧播放版本的确认不能完成当前响应。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证旧播放版本的确认不能完成当前响应。"""
        await response(live)
        await live.interrupt("button")
        await live.on_client({"type": "playback_complete", "response_id": "r1", "epoch": 0})
        assert not live.responses["r1"].played

    run(scenario())


def test_independent_sessions(store):
    """验证会话事件、产品及响应状态相互隔离。

    Args:
        store: 使用临时 SQLite 文件的存储夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证会话事件、产品及响应状态相互隔离。"""
        lives = []
        for _ in range(30):
            sid = store.create_session(store.products()[0]["id"])
            lives.append(
                LiveSession(sid, store.session(sid)["snapshot"], Settings(), store, Browser(), Transport())
            )
        await asyncio.gather(*(response(live, f"r{n}") for n, live in enumerate(lives)))
        await lives[0].interrupt("button")
        assert all(not next(iter(session.responses.values())).cancelled for session in lives[1:])
        assert all(len(session.responses) == 1 for session in lives)

    run(scenario())


def test_http_product_upload_and_key_redaction(tmp_path, monkeypatch):
    """验证产品与上传接口及公开状态中的凭证脱敏。

    Args:
        tmp_path: pytest 为本测试分配的临时目录。
        monkeypatch: pytest 提供的临时对象替换夹具，测试结束时自动还原。
    """
    from test_rag import FakeEmbedding, fake_parse

    from backend.app import create_app

    # This test exercises the HTTP contract. Token boundaries are covered with
    # offline tokenizers in test_rag; no downloaded asset required here.
    monkeypatch.setattr("backend.services.knowledge.ingestion.validate_text", lambda *args: None)

    app = create_app(
        Settings(
            api_key="secret-value-never-return",
            workspace="workspace_test",
            database=str(tmp_path / "http.db"),
            embedding_dimension=64,
        ),
        parser=fake_parse,
        embedder=FakeEmbedding(),
    )
    with TestClient(app) as client:
        assert "secret-value" not in client.get("/api/health").text
        p = client.post("/api/products", json=ProductInput(name="测试产品").model_dump()).json()
        assert (
            client.post(f"/api/products/{p['id']}/documents", files={"file": ("x.exe", b"test")}).status_code
            == 415
        )
        assert (
            client.post(
                f"/api/products/{p['id']}/documents", files={"file": ("x.txt", b"x" * (10 * 1024**2 + 1))}
            ).status_code
            == 413
        )
        assert (
            client.post(
                f"/api/products/{p['id']}/documents", files={"file": ("x.md", "价格说明".encode())}
            ).status_code
            == 201
        )
        assert client.post("/api/sessions", json={"product_id": p["id"]}).status_code == 201
        assert (
            client.post(
                "/api/products", headers={"Origin": "https://attacker.example"}, json={"name": "bad"}
            ).status_code
            == 403
        )


def test_missing_credentials_blocks_session(tmp_path):
    """验证未配置 API Key 时禁止创建收费模型会话。

    Args:
        tmp_path: pytest 为本测试分配的临时目录。
    """
    from backend.app import create_app

    with TestClient(create_app(Settings(database=str(tmp_path / "empty.db")))) as client:
        p = client.get("/api/products").json()[0]
        assert client.post("/api/sessions", json={"product_id": p["id"]}).status_code == 503


def test_url_uses_optional_workspace_without_changing_model():
    """验证可选工作空间只改变域名，不更换指定模型。"""
    settings = Settings(api_key="test")
    assert not settings.missing()
    assert (
        settings.url() == "wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=qwen-audio-3.1-realtime-plus"
    )
    settings.workspace = "space"
    assert "space.cn-beijing.maas.aliyuncs.com" in settings.url()


def test_stalled_model_exits_instead_of_hanging(live):
    """验证生成超时会结束会话并显示错误。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """
    async def scenario():
        """按事件顺序执行异步场景：验证生成超时会结束会话并显示错误。"""
        live.ready = True
        await response(live, audio=False)
        live.clock.value += 31
        await live.tick()
        assert live.closed and live.reason == "error"

    run(scenario())


def test_websocket_full_lifecycle_with_fake_provider(tmp_path):
    """验证假模型下的配置、收音、回复、主动结束及资源释放。

    Args:
        tmp_path: pytest 为本测试分配的临时目录。
    """
    from backend.app import create_app

    class Provider(Transport):
        def __init__(self):
            """初始化假模型的事件队列和收到的音频帧计数。"""
            super().__init__()
            self.incoming = asyncio.Queue()
            self.audio_frames = 0

        async def open(self):
            """模拟模型创建会话的服务端事件。"""
            await self.incoming.put({"type": "session.created"})

        async def send(self, event):
            """根据客户端配置和生成请求推进假模型协议。

            Args:
                event: 应用提交给模型的协议事件。
            """
            await super().send(event)
            if event["type"] == "session.update":
                await self.incoming.put({"type": "session.updated"})
            elif event["type"] == "input_audio_buffer.append":
                self.audio_frames += 1
            elif event["type"] == "response.create":
                for item in [
                    {"type": "response.created", "response": {"id": "r1"}},
                    {"type": "response.audio.delta", "response_id": "r1", "delta": "AAA="},
                    {"type": "response.done", "response": {"id": "r1", "status": "completed"}},
                ]:
                    await self.incoming.put(item)

        async def receive(self):
            """读取下一条假模型事件。

            Returns:
                队列中的服务端事件。
            """
            return await self.incoming.get()

        async def close(self):
            """模拟关闭模型连接，不访问外部资源。"""
            pass

    provider = Provider()
    app = create_app(
        Settings(api_key="fake", workspace="fake", database=str(tmp_path / "ws.db")), lambda: provider
    )
    with TestClient(app) as client:
        pid = client.get("/api/products").json()[0]["id"]
        sid = client.post("/api/sessions", json={"product_id": pid}).json()["id"]
        with client.websocket_connect(f"/ws/sessions/{sid}") as ws:
            assert ws.receive_json()["type"] == "ready"
            ws.send_bytes(bytes(640))
            while ws.receive_json()["type"] != "response_done":
                pass
            ws.send_json({"type": "stop"})
            while ws.receive_json()["type"] != "ended":
                pass
        report = client.get(f"/api/sessions/{sid}/report").json()
        assert report["reason"] == "user_ended"
        assert provider.audio_frames == 1
        assert not app.state.active
