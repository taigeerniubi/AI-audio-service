"""显式调用本地应用和真实模型验证开场，不采集麦克风或保存音频。

Explicit invocation generates one greeting and incurs model usage. No audio saved.
"""

import asyncio
import json

import httpx
from websockets.asyncio.client import connect


async def main():
    """连接已启动的本地应用并请求一次真实开场，验证完整会话结束。

    Returns:
        成功返回 0，未收到有效音频或正常结束时返回 1。
    """
    base = "http://127.0.0.1:8000"
    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        products = (await http.get("/api/products")).json()
        created = await http.post("/api/sessions", json={"product_id": products[0]["id"]})
        created.raise_for_status()
        session = created.json()
        events = []
        audio_bytes = 0
        async with connect("ws://127.0.0.1:8000" + session["websocket_path"]) as ws:
            async with asyncio.timeout(25):
                while True:
                    event = json.loads(await ws.recv())
                    events.append(event["type"])
                    if event["type"] == "ready":
                        await ws.send(bytes(640))
                    elif event["type"] == "audio":
                        audio_bytes += len(event["audio"]) * 3 // 4
                    elif event["type"] == "response_done":
                        await ws.send(json.dumps({"type": "stop"}))
                    elif event["type"] == "error":
                        print(json.dumps({"ok": False, "message": event["message"]}, ensure_ascii=True))
                        return 1
                    elif event["type"] == "ended":
                        break
        report = (await http.get(f"/api/sessions/{session['id']}/report")).json()
        ok = "ready" in events and audio_bytes > 0 and report["reason"] == "user_ended"
        print(
            json.dumps(
                {
                    "ok": ok,
                    "model": report["model"],
                    "audio_bytes_received": audio_bytes,
                    "transcripts": len(report["transcripts"]),
                    "end_reason": report["reason"],
                    "estimated_cost_cny": report["estimated_cost_cny"],
                    "note": "Real model + local API; silence input, no microphone or acoustic latency test.",
                }
            )
        )
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
