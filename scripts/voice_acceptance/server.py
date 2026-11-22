"""启动使用真实千问和隔离数据库的验收服务并限制主动生成次数。"""

import json
import os
import time
from dataclasses import replace
from datetime import datetime, timezone

from fastapi.responses import FileResponse

from backend.core.config import Settings
from backend.core.paths import PROJECT_ROOT
from backend.llm.qwen.realtime import QwenTransport
from backend.main import create_app

ROOT = PROJECT_ROOT / ".cache" / "voice-acceptance"
ROOT.mkdir(parents=True, exist_ok=True)
RUN_NAME = os.getenv("ACCEPTANCE_RUN", "run")
RESPONSE_LIMIT = int(os.getenv("ACCEPTANCE_RESPONSE_LIMIT", "10"))
if not 1 <= RESPONSE_LIMIT <= 10:
    raise ValueError("单批真实响应上限须为 1～10，包含失败和取消响应")
OUTPUT = PROJECT_ROOT / "output" / "voice-acceptance" / RUN_NAME
OUTPUT.mkdir(parents=True, exist_ok=True)
settings = replace(Settings.from_env(), database=str(ROOT / f"{RUN_NAME}.db"))
if settings.model != "qwen-audio-3.1-realtime-plus":
    raise ValueError("验收只允许当前确认的 Plus 模型")
trace_path = OUTPUT / "provider-events.jsonl"
events = (
    [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    if trace_path.exists()
    else []
)
response_ids = {row["response_id"] for row in events if row["type"] == "response.created"}
connections = max((row["connection"] for row in events), default=0)


def record(direction: str, connection: int, event: dict) -> None:
    """只记录时序、转录与返回用量，剔除原始音频和连接凭证。"""
    kind = event.get("type", "unknown")
    if kind == "input_audio_buffer.append":
        return
    entry = {
        "at": time.time(),
        "monotonic": time.perf_counter(),
        "utc": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "connection": connection,
        "type": kind,
    }
    for key in ("response_id", "item_id", "call_id", "name", "audio_start_ms", "audio_end_ms", "reason", "transcript"):
        if key in event:
            entry[key] = event[key]
    if isinstance(event.get("item"), dict):
        item = event["item"]
        entry["item_id"] = item.get("id")
        entry["item_role"] = item.get("role")
        entry["item_type"] = item.get("type")
        if item.get("call_id"):
            entry["call_id"] = item["call_id"]
        if item.get("name"):
            entry["name"] = item["name"]
        # 控制消息只保留类别；不记录音频、凭证或整个产品上下文。
        if str(item.get("id", "")).startswith("app_idle_"):
            entry["application_control"] = "idle_reminder"
    if "response" in event:
        response = event["response"]
        entry.update(
            response_id=response.get("id"), status=response.get("status"), usage=response.get("usage")
        )
    if kind == "error":
        error = event.get("error", {})
        entry["error"] = {
            key: str(error.get(key, "")).replace(settings.api_key, "[REDACTED]")
            for key in ("type", "code", "message", "param")
        }
    if kind in {"session.created", "session.updated"}:
        session = event.get("session", {})
        entry["session"] = {key: session.get(key) for key in ("model", "voice", "turn_detection")}
    events.append(entry)
    with (OUTPUT / "provider-events.jsonl").open("a", encoding="utf-8") as target:
        target.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ObservedTransport(QwenTransport):
    """观察真实模型事件，在十次响应额度耗尽后阻止新的主动生成。"""

    def __init__(self):
        """创建一个独享的真实连接并分配观测标识。"""
        super().__init__(settings)
        global connections
        connections += 1
        self.connection_id = connections

    async def open(self) -> None:
        """额度已耗尽时拒绝重连，重启验收服务也不重置调用计数。"""
        if len(response_ids) >= RESPONSE_LIMIT:
            raise RuntimeError("本次十响应验收已完成")
        await super().open()

    async def send(self, event: dict) -> None:
        """发送实际协议事件，达到额度后拒绝额外 response.create。"""
        if event.get("type") == "input_audio_buffer.append" and len(response_ids) >= RESPONSE_LIMIT:
            return
        if event.get("type") == "response.create" and len(response_ids) >= RESPONSE_LIMIT:
            raise RuntimeError("十响应验收额度已用尽")
        record("send", self.connection_id, event)
        await super().send(event)

    async def receive(self) -> dict:
        """记录服务端真实事件，包含被取消的响应，不隐藏失败样本。"""
        event = await super().receive()
        if event.get("type") == "response.created":
            response_ids.add(event["response"]["id"])
        record("receive", self.connection_id, event)
        return event


app = create_app(settings, transport_factory=ObservedTransport)
product = app.state.store.products()[0]
app.state.store.save_product(
    {
        **product,
        "name": "语音验收 · 虚构客服产品",
        "opening": "您好，我是知答的 AI 产品顾问。我们可以聊聊您的需求，您也可以随时打断我。您想先了解什么？",
    },
    product["id"],
)


@app.get("/acceptance/state")
async def state():
    """返回本机验收响应计数与脱敏事件，供测试驱动器使用。"""
    return {"responses": len(response_ids), "limit": RESPONSE_LIMIT, "events": events, "product_id": product["id"]}


@app.get("/acceptance/fixtures/{name}")
async def fixture(name: str):
    """只向验收浏览器提供白名单内的合成语音。"""
    from fastapi import HTTPException

    if name not in {
        "manifest.json",
        "uh.wav",
        "uhuh.wav",
        "identity.wav",
        "pause_600.wav",
        "pause_1200.wav",
        "long_prompt.wav",
        "interrupt.wav",
        "interrupt_two.wav",
        "listening.wav",
        "clicks.wav",
    }:
        raise HTTPException(404)
    return FileResponse(ROOT / "fixtures" / name)


# main 的静态挂载必须在验收辅助路由之后，否则根挂载会截获请求。
mounts = [route for route in app.router.routes if getattr(route, "name", None) == "frontend"]
for mount in mounts:
    app.router.routes.remove(mount)
    app.router.routes.append(mount)
