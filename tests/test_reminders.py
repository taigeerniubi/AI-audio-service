"""回归验证静音提醒输入、确认顺序、撤销竞态及业务隔离。"""

import pytest
from test_core import Browser, Clock, Transport, done, response, run

from backend.context.prompts import SILENCE_REMINDER_TEXT
from backend.core.config import Settings
from backend.db.store import Store
from backend.services.realtime.session import LiveSession


@pytest.fixture
def live(tmp_path):
    """创建完全离线的语音会话，测试不调用任何云模型。"""
    store = Store(str(tmp_path / "reminder.db"))
    sid = store.create_session(store.products()[0]["id"])
    session = LiveSession(
        sid, store.session(sid)["snapshot"], Settings(), store, Browser(), Transport(), Clock()
    )
    session.ready = True
    yield session
    store.close()


async def schedule(live):
    """模拟模型空闲满八秒，返回待确认的应用控制消息。"""
    await live.tick()
    live.clock.value += 8
    await live.tick()
    return live.reminders.pending.item_id


async def acknowledge(live, item_id):
    """模拟服务端确认控制消息已进入上下文。"""
    await live.on_model({"type": "conversation.item.created", "item": {"id": item_id}})


async def deleted(live, item_id):
    """模拟服务端确认删除过期或已消费的控制消息。"""
    await live.on_model({"type": "conversation.item.deleted", "item_id": item_id})


def generations(live):
    """读取测试传输记录中的主动生成请求。"""
    return [event for event in live.transport.events if event["type"] == "response.create"]


def test_reminder_waits_for_exact_ack_and_never_reuses_last_question(live):
    """确认之前不能生成，重复确认不能重复生成。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：确认之前不能生成，重复确认不能重复生成。"""
        item_id = await schedule(live)
        message = live.transport.events[-1]["item"]
        assert message["role"] == "user"
        assert "非客户语音" in message["content"][0]["text"]
        assert SILENCE_REMINDER_TEXT in message["content"][0]["text"]
        assert not generations(live)
        await live.tick()
        await acknowledge(live, "unrelated_item")
        assert not generations(live)
        await acknowledge(live, item_id)
        await acknowledge(live, item_id)
        assert len(generations(live)) == 1
        assert live.reminded

    run(scenario())


def test_speech_before_ack_removes_stale_reminder_without_generating(live):
    """用户已经说话时，迟到确认只能清理上下文。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：用户已经说话时，迟到确认只能清理上下文。"""
        item_id = await schedule(live)
        await live.on_model({"type": "input_audio_buffer.speech_started"})
        await acknowledge(live, item_id)
        assert item_id in live.pending_deletes
        await deleted(live, item_id)
        assert not generations(live)
        assert not live.reminders.pending
        assert not live.reminded

    run(scenario())


def test_muting_before_ack_pauses_reminder_and_restarts_idle_after_unmute(live):
    """麦克风静音期间不产生提醒，恢复后重新计算空闲。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：麦克风静音期间不产生提醒，恢复后重新计算空闲。"""
        item_id = await schedule(live)
        await live.on_client({"type": "mute", "muted": True})
        await acknowledge(live, item_id)
        await deleted(live, item_id)
        live.clock.value += 20
        await live.tick()
        assert not generations(live) and not live.closed
        await live.on_client({"type": "mute", "muted": False})
        await live.tick()
        assert not live.reminders.pending
        live.clock.value += 8
        await live.tick()
        assert live.reminders.pending.item_id != item_id
        assert not generations(live)

    run(scenario())


@pytest.mark.parametrize("source", ["speech", "button", "microphone_muted"])
def test_late_reminder_response_is_cancelled_without_repeating_old_answer(live, source):
    """生成请求与插话竞争时丢弃提醒，随后仍接收客户的新回复。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
        source: 参数化提供的打断来源，如语音或停止按钮。
    """

    async def scenario():
        """按事件顺序执行异步场景：生成请求与插话竞争时丢弃提醒，随后仍接收客户的新回复。"""
        item_id = await schedule(live)
        await acknowledge(live, item_id)
        if source == "speech":
            await live.on_model({"type": "input_audio_buffer.speech_started"})
        elif source == "button":
            await live.on_client({"type": "interrupt"})
        else:
            await live.on_client({"type": "mute", "muted": True})
        await deleted(live, item_id)
        await response(live, "late_reminder")
        assert live.responses["late_reminder"].cancelled
        assert not any(event["type"] == "audio" for event in live.browser.events)
        cancels = [event for event in live.transport.events if event["type"] == "response.cancel"]
        assert len(cancels) == (0 if source == "speech" else 1)
        await done(live, "late_reminder", "cancelled")
        await deleted(live, "ilate_reminder")
        assert len(generations(live)) == 1
        await live.on_model({"type": "input_audio_buffer.speech_stopped"})
        await response(live, "actual_user_reply")
        assert live.browser.events[-1]["type"] == "audio"
        assert not live.responses["actual_user_reply"].cancelled

    run(scenario())


@pytest.mark.parametrize("reply,matched", [(SILENCE_REMINDER_TEXT, True), ("能听清您说话。", False)])
def test_reminder_checks_actual_words_and_keeps_application_text_out_of_transcripts(live, reply, matched):
    """保存真实话术是否匹配的证据，不把控制指令伪装成客户发言。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
        reply: 假模型生成的提醒文本，用于检查是否符合固定话术。
        matched: 预期的提醒话术匹配结果。
    """

    async def scenario():
        """按事件顺序执行异步场景：保存真实话术是否匹配的证据，不把控制指令伪装成客户发言。"""
        item_id = await schedule(live)
        await acknowledge(live, item_id)
        await live.on_model(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "transcript": "好的，请预约",
            }
        )
        assert item_id not in live.transcripts
        await response(live, "reminder")
        await live.on_model(
            {
                "type": "response.audio_transcript.done",
                "response_id": "reminder",
                "item_id": "ireminder",
                "transcript": reply,
            }
        )
        await done(live, "reminder")
        assert item_id in live.pending_deletes
        result = next(event for event in live.events if event["type"] == "idle_reminder_result")
        assert result["matches_expected"] is matched
        assert result["actual_text"] == reply
        await deleted(live, item_id)
        await live.on_client({"type": "playback_complete", "response_id": "reminder", "epoch": 0})
        await live.tick()
        live.clock.value += 20
        await live.tick()
        assert live.reason == "idle_timeout"
        assert len(generations(live)) == 1

    run(scenario())


def test_missing_ack_fails_explicitly_without_paid_retry(live):
    """控制消息没有确认时不重复生成，不无限挂起会话。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：控制消息没有确认时不重复生成，不无限挂起会话。"""
        await schedule(live)
        live.clock.value += 5
        await live.tick()
        assert live.closed and live.reason == "error"
        assert not generations(live)
        assert "确认超时" in live.browser.events[-1]["message"]

    run(scenario())


def test_reminder_cannot_execute_tools_or_trigger_another_response(live):
    """即使模型错误调用工具，也不能把静音当作客户授权。

    Args:
        live: 注入假时钟和假传输的隔离会话夹具。
    """

    async def scenario():
        """按事件顺序执行异步场景：即使模型错误调用工具，也不能把静音当作客户授权。"""
        item_id = await schedule(live)
        await acknowledge(live, item_id)
        await response(live, "reminder")
        call = {
            "name": "search_product_knowledge",
            "arguments": '{"query":"价格"}',
            "call_id": "c1",
            "response_id": "reminder",
        }
        await live.prepare_tool(call)
        kind, prepared = await live.queue.get()
        assert kind == "tool" and "静音提醒" in prepared["error"]
        await live.commit_tool(prepared)
        assert live.store.actions(live.id) == []
        assert not live.tool_followup
        assert len(generations(live)) == 1

    run(scenario())
