"""处理浏览器事件并更新单一会话状态。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.realtime.session import LiveSession


async def handle_client_event(self: "LiveSession", event: dict) -> None:
    """处理客户端控制与播放确认，过期响应确认不得改变当前播放状态。

    Args:
        self: 由单一事件循环持有和修改的 LiveSession。
        event: 已解码的对应协议事件。
    """
    kind = event.get("type")
    if kind == "ping":
        # 心跳只探测应用连接，不提交给模型，也不改变语音静音时钟。
        sequence = event.get("sequence")
        if type(sequence) is int and 0 <= sequence <= 2**53 - 1:
            await self.emit("pong", sequence=sequence)
    elif kind == "stop":
        if self.playback.enabled:
            pending = {rid for iid in self.playback.pending for rid in self.interruptions[iid]["response_ids"]}
            targets = [r.id for r in self.responses.values() if not r.played and (not r.cancelled or r.id in pending)]
            await self.playback.apply(event.get("playback"), targets, restore=False)
        self.reason, self.closed = "user_ended", True
    elif kind == "playback_sync":
        self.playback.enabled = event.get("version") == 1
    elif kind == "interrupt":
        await self.interrupt("button")
    elif kind == "mute":
        self.muted = bool(event.get("muted"))
        self.idle_since = None
        if self.muted:
            await self.reminders.cancel("microphone_muted")
    elif kind == "playback_cleared":
        await self.playback.acknowledge(event)
        entry = self.interruptions.get(event.get("interruption_id"))
        if entry and entry["had_output"] and "ack_ms" not in entry:
            entry["ack_ms"] = round((self.clock() - entry["sent"]) * 1000, 1)
            metric = {"kind": "interruption", "clear_ack_roundtrip_ms": entry["ack_ms"]}
            duration = event.get("clear_ms")
            if isinstance(duration, (int, float)) and 0 <= duration <= 10000:
                metric["client_clear_ms"] = duration
            self.metrics.append(metric)
            await self.emit("metric", metric=metric)
        await self.maybe_continue()
    elif kind in {"playback_started", "playback_complete"}:
        response = self.responses.get(event.get("response_id"))
        if not response or response.cancelled or event.get("epoch") != response.epoch:
            return
        if kind == "playback_complete":
            response.played = True
            await self.trace("playback_complete", response_id=response.id)
        elif "playback_ack_at" not in response.metric:
            response.metric["playback_ack_at"] = self.clock()
            metric = {
                "kind": "response",
                "response_id": response.id,
                "model_first_audio_ms": response.metric.get("model_first_audio_ms"),
            }
            if "detected_at" in response.metric:
                metric["speech_stop_event_to_play_ack_ms"] = round(
                    (self.clock() - response.metric["detected_at"]) * 1000, 1
                )
            wait = event.get("queue_wait_ms")
            if isinstance(wait, (float, int)) and 0 <= wait <= 60000:
                metric["client_queue_wait_ms"] = wait
            self.metrics.append(metric)
            await self.emit("metric", metric=metric)
    elif kind == "client_error":
        await self.fail("音频设备或播放缓冲异常，请重新开始。")
