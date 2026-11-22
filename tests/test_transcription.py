"""验证千问流式转录预览、最终校正及附和事件与业务状态的隔离。"""

import pytest
from test_core import Browser, Clock, Transport, run

from backend.core.config import Settings
from backend.db.store import Store
from backend.services.realtime.session import LiveSession
from backend.services.sales_tools import prepare_result


@pytest.fixture
def live(tmp_path):
    """创建临时数据库与离线模型会话，退出时释放数据库。"""
    store = Store(str(tmp_path / "transcription.db"))
    sid = store.create_session(store.products()[0]["id"])
    yield LiveSession(
        sid, store.session(sid)["snapshot"], Settings(), store, Browser(), Transport(), Clock()
    )
    store.close()


def test_text_stash_preview_replaces_drafts_before_speech_finishes(live):
    """重放已确认前缀和可修订尾部，结束前即可见整句且没有重复拼接。"""
    async def scenario():
        """逐个推送官方格式并检查每次发送给浏览器的预览。"""
        for text, stash, expected in [
            ("", "我想买电脑", "我想买电脑"),
            ("我想", "买内存", "我想买内存"),
            ("我想买内存", "，三十二G", "我想买内存，三十二G"),
            ("我想买内存", "，三十二GB的", "我想买内存，三十二GB的"),
        ]:
            await live.on_model({
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "u1", "text": text, "stash": stash,
            })
            sent = live.browser.events[-1]
            assert sent["type"] == "transcript"
            assert sent["message"]["text"] == expected
            assert sent["message"]["final"] is False
        assert [e["type"] for e in live.events] == ["user_transcription_preview"]
        await live.on_model({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u1", "transcript": "我想买内存，32GB的。",
        })
        assert live.transcripts["u1"]["text"] == "我想买内存，32GB的。"
        assert live.transcripts["u1"]["final"] is True
        assert live.events[-1]["type"] == "user_transcription_completed"
        await live.on_model({
            "type": "conversation.item.input_audio_transcription.delta",
            "item_id": "u1", "text": "过期草稿", "stash": "",
        })
        assert live.transcripts["u1"]["text"] == "我想买内存，32GB的。"
        assert live.transcripts["u1"]["final"] is True

    run(scenario())


def test_user_previews_are_isolated_and_cannot_confirm_booking(live):
    """不同话轮互不串字，临时识别出的肯定句不能变成预约证据。"""
    async def scenario():
        """交错两个转录项并在最终纠正前后校验业务确认。"""
        for item_id, text in [("u1", "好的，帮我预约"), ("u2", "先问一下价格")]:
            await live.on_model({
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": item_id, "text": "", "stash": text,
            })
        args = {"confirmation_quote": "好的，帮我预约", "time": "明天"}
        assert "error" in prepare_result("schedule_demo", args, list(live.transcripts.values()))
        await live.on_model({
            "type": "conversation.item.input_audio_transcription.completed",
            "item_id": "u1", "transcript": "先不要帮我预约。",
        })
        assert "error" in prepare_result("schedule_demo", args, list(live.transcripts.values()))
        assert live.transcripts["u2"]["text"] == "先问一下价格"

    run(scenario())


def test_ambient_backchannel_does_not_interrupt_or_become_user_evidence(live):
    """模型判定的附和只留下观测记录，不清空播放、不生成新响应。"""
    async def scenario():
        """在助手响应进行时注入附和事件。"""
        await live.on_model({"type": "response.created", "response": {"id": "r1"}})
        live.browser.events.clear()
        for kind, fields in [
            ("delta", {"text": "", "stash": "嗯嗯"}),
            ("completed", {"transcript": "嗯嗯"}),
        ]:
            await live.on_model({
                "type": f"conversation.item.ambient_audio_transcription.{kind}",
                "item_id": "ambient1", **fields,
            })
        assert not live.transcripts and not live.transport.events
        assert live.active == "r1" and not live.responses["r1"].cancelled
        assert live.epoch == 0 and not live.user_speaking
        assert [e["type"] for e in live.browser.events] == ["trace"]
        assert live.events[-1]["characters"] == 2

    run(scenario())


def test_legacy_deltas_and_assistant_deltas_still_accumulate(live):
    """兼容旧 delta 字段并保持助手输出的增量语义。"""
    async def scenario():
        """对用户与助手分别发送两个增量并检查合并结果。"""
        await live.on_model({"type": "response.created", "response": {"id": "r1"}})
        for value in ["你好", "世界"]:
            await live.on_model({
                "type": "conversation.item.input_audio_transcription.delta",
                "item_id": "u1", "delta": value,
            })
            await live.on_model({
                "type": "response.audio_transcript.delta", "response_id": "r1",
                "item_id": "a1", "delta": value,
            })
        assert live.transcripts["u1"]["text"] == "你好世界"
        assert live.transcripts["a1"]["text"] == "你好世界"

    run(scenario())


def test_model_context_does_not_grow_with_uploaded_documents(live):
    """真实初始化入口只发送固定检索规则，资料数量与体积不会扩大模型指令。"""
    async def scenario():
        """比较空库与二十份大文件的完整初始化配置，不包含目录、正文或向量。"""
        live.snapshot["documents"] = []
        await live.on_model({"type": "session.created"})
        empty_config = live.transport.events[-1]
        live.snapshot["documents"] = [{
            "name": f"AMD_9700X_{i}.pdf", "text": "PRIVATE_DOCUMENT_BODY" * 5000,
            "rag": {"chunks": [], "vectors": [[0.123456789]]},
        } for i in range(20)]
        await live.on_model({"type": "session.created"})
        assert live.transport.events[-1] == empty_config
        instructions = live.transport.events[-1]["session"]["instructions"]
        assert "AMD_9700X" not in instructions and "knowledge_documents" not in instructions
        assert "PRIVATE_DOCUMENT_BODY" not in instructions and "0.123456789" not in instructions
        assert "先调用 search_product_knowledge" in instructions

    run(scenario())
