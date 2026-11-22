"""验证打断按播放回执裁剪助手历史、丢弃未播放内容并等待上下文修复。"""

import pytest
from test_core import done, response, run
from test_core import live as core_live
from test_core import store as core_store

live, store = core_live, core_store


async def speaking(live):
    """建立支持播放同步的响应，并提交先于声音生成完毕的全文。

    Args:
        live: 隔离的假模型会话。
    """
    await live.on_client({"type": "playback_sync", "version": 1})
    await response(live)
    await live.on_model({"type": "response.audio_transcript.done", "response_id": "r1", "item_id": "ir1",
                         "transcript": "已播放的部分。尚未播放的秘密。"})


def receipt(count=7, **changes):
    """构造当前响应的播放回执，允许测试注入非法字段。

    Args:
        count: 已显示的 Unicode 字符数。
        **changes: 替换回执字段。

    Returns:
        不含客户端文本的播放回执字典。
    """
    return {"item_id": "ir1", "response_id": "r1", "epoch": 0,
            "heard_characters": count, "played_ms": 1200, **changes}


def test_interrupted_history_retains_only_playback_prefix_after_both_acknowledgements(live):
    """删除原消息和收到播放回执后才恢复前缀，迟到全文不能重新污染上下文。

    Args:
        live: 隔离的假模型会话。
    """
    async def scenario():
        """交错删除确认、播放确认和迟到模型事件。"""
        await speaking(live)
        await done(live)
        await live.interrupt("button")
        iid = next(iter(live.playback.pending))
        await live.on_model({"type": "conversation.item.deleted", "item_id": "ir1"})
        assert not any(event["type"] == "conversation.item.create" for event in live.transport.events)
        await live.on_model({"type": "response.audio_transcript.done", "response_id": "r1", "item_id": "ir1",
                             "transcript": "迟到的未播放全文不能出现"})
        assert live.transcripts["ir1"]["text"] == "已播放的部分。尚未播放的秘密。"
        await live.on_client({"type": "playback_cleared", "interruption_id": iid, "playback": [receipt()]})
        assert not live.playback.pending and not live.repair_marker
        assert live.transcripts["ir1"]["text"] == "已播放的部分。"
        notes = [event["item"]["content"][0]["text"] for event in live.transport.events if event["type"] == "conversation.item.create"]
        assert len(notes) == 1 and "已播放的部分。" in notes[0]
        assert "尚未播放的秘密" not in notes[0]
        await live.on_client({"type": "playback_cleared", "interruption_id": iid, "playback": [receipt(100)]})
        assert live.transcripts["ir1"]["text"] == "已播放的部分。"
        assert live.report()["transcripts"][0]["text"] == "已播放的部分。"
    run(scenario())


@pytest.mark.parametrize("invalid", [receipt(100), receipt(-1), receipt(True), receipt(epoch=9),
                                     receipt(response_id="other"), receipt(played_ms=0), {"text": "客户端注入"}])
def test_invalid_receipt_cannot_insert_or_preserve_unheard_text(live, invalid):
    """非法回执采取保守清空，不能凭客户端文字伪造已播放上下文。

    Args:
        live: 隔离的假模型会话。
        invalid: 参数化的非法回执。
    """
    async def scenario():
        """提交非法回执并检查报告及下一轮上下文。"""
        await speaking(live)
        await live.interrupt("button")
        iid = next(iter(live.playback.pending))
        await live.on_client({"type": "playback_cleared", "interruption_id": iid, "playback": [invalid]})
        await done(live, status="cancelled")
        await live.on_model({"type": "conversation.item.deleted", "item_id": "ir1"})
        assert live.report()["transcripts"] == []
        assert "尚未播放" not in str(live.transport.events)
        assert "客户端注入" not in str(live.transport.events)
    run(scenario())


def test_missing_playback_ack_expires_without_stalling_next_turn(live):
    """回执丢失只删除未确认内容，不关闭整场会话或无限等待。

    Args:
        live: 隔离的假模型会话。
    """
    async def scenario():
        """推进假时钟并验证删除后可以继续请求响应。"""
        await speaking(live)
        await done(live)
        await live.interrupt("button")
        await live.on_model({"type": "conversation.item.deleted", "item_id": "ir1"})
        live.clock.value += 2.1
        await live.playback.expire()
        assert not live.playback.pending and not live.closed
        assert live.report()["transcripts"] == []
        assert await live.create_response()
    run(scenario())


def test_user_end_trims_unplayed_transcript_in_final_report(live):
    """主动结束时报告同样只保留播放前缀。

    Args:
        live: 隔离的假模型会话。
    """
    async def scenario():
        """带最后播放位置结束会话并读取报告。"""
        await speaking(live)
        await live.on_client({"type": "stop", "playback": [receipt()]})
        assert live.closed and live.reason == "user_ended"
        assert live.report()["transcripts"][0]["text"] == "已播放的部分。"
    run(scenario())


@pytest.mark.parametrize("acknowledged", [False, True])
def test_end_after_interrupt_preserves_only_the_confirmed_prefix(live, acknowledged):
    """连续点击打断和结束时不遗留全文，也不擦掉已经确认的前缀。

    Args:
        live: 隔离的假模型会话。
        acknowledged: 结束之前是否已经处理清空回执。
    """
    async def scenario():
        """在清空回执前后分别结束，检查最终报告的一致性。"""
        await speaking(live)
        await live.interrupt("button")
        iid = next(iter(live.playback.pending))
        if acknowledged:
            await live.on_client({"type": "playback_cleared", "interruption_id": iid, "playback": [receipt()]})
        await live.on_client({"type": "stop", "playback": [] if acknowledged else [receipt()]})
        assert live.closed and live.reason == "user_ended"
        assert live.report()["transcripts"][0]["text"] == "已播放的部分。"
    run(scenario())
