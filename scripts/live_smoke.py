"""显式检查真实千问连接，默认生成一次收费开场且不保存音频。

Usage: python -m scripts.live_smoke [--configure-only]
Default generates one short greeting (billable); no microphone or customer data.
"""

import asyncio
import json
import sys
import time

from backend.core.config import Settings
from backend.llm.qwen.realtime import QwenTransport
from backend.services.sales_tools import definitions


async def main():
    """按命令行模式检查真实模型配置或生成一次收费开场。

    Returns:
        烟雾测试成功时返回 0，否则返回 1。
    """
    settings = Settings.from_env()
    if settings.missing():
        print(json.dumps({"ok": False, "missing": settings.missing()}))
        return 1
    transport = QwenTransport(settings)
    result = {"model": settings.model, "region": settings.region, "configured": False, "ok": False}
    try:
        await transport.open()
        begin = time.monotonic()
        async with asyncio.timeout(30):
            while True:
                event = await transport.receive()
                kind = event.get("type")
                if kind == "session.created":
                    await transport.send(
                        {
                            "type": "session.update",
                            "session": {
                                "modalities": ["text", "audio"],
                                "voice": settings.voice,
                                "instructions": "你是 AI 产品顾问。本次为接入测试，只说：您好，我是AI产品顾问。",
                                "input_audio_format": "pcm",
                                "output_audio_format": "pcm",
                                "input_audio_transcription": {"language": "zh"},
                                "output_audio": {"language": "zh"},
                                "turn_detection": {"type": "smart_turn_v2"},
                                "max_history_turns": 20,
                                "enable_search": False,
                                "tools": definitions(),
                            },
                        }
                    )
                elif kind == "session.updated":
                    result["configured"] = True
                    if "--configure-only" in sys.argv:
                        result["ok"] = True
                        break
                    begin = time.monotonic()
                    await transport.send(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "你好"}],
                            },
                        }
                    )
                    await transport.send({"type": "response.create"})
                elif kind == "response.audio.delta" and "first_audio_ms" not in result:
                    result["first_audio_ms"] = round((time.monotonic() - begin) * 1000, 1)
                elif kind == "response.audio_transcript.done":
                    result["reply"] = event.get("transcript")
                elif kind == "response.done":
                    result["status"] = event["response"].get("status")
                    result["usage"] = event["response"].get("usage")
                    result["ok"] = result["status"] == "completed" and "first_audio_ms" in result
                    break
                elif kind == "error":
                    result["error_code"] = event.get("error", {}).get("code")
                    # SDK/provider messages are intentionally not echoed with credentials.
                    result["error_type"] = event.get("error", {}).get("type")
                    result["error_message"] = (
                        event.get("error", {}).get("message", "").replace(settings.api_key, "[REDACTED]")
                    )
                    break
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status:
            result["http_status"] = status
    finally:
        await transport.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
