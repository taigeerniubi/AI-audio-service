"""处理千问模型事件并更新单一会话状态。"""

from typing import TYPE_CHECKING

from backend.models.realtime import Response

if TYPE_CHECKING:
    from backend.services.realtime.session import LiveSession


async def handle_model_event(self: "LiveSession", event: dict) -> None:
    """按模型事件推进转录、响应、上下文删除、提醒和工具准备。

    Args:
        self: 由单一事件循环持有和修改的 LiveSession。
        event: 已解码的对应协议事件。
    """
    self.last_model_event = self.clock()
    kind = event.get("type", "")
    rid = event.get("response_id")
    if kind == "session.created":
        await self.transport.send(self.context.session_update(self.snapshot["product"]))
    elif kind == "session.updated" and not self.ready:
        self.ready = True
        await self.emit("ready", model=self.settings.model, voice=self.settings.voice)
        await self.transport.send(self.context.greeting_message())
        await self.trace("application_greeting_requested")
        await self.create_response()
    elif kind == "error":
        error = event.get("error", {})
        absent = str(error.get("message", "")).removeprefix("Cannot find item with id: ")
        if (error.get("code") == "invalid_value" and error.get("param") == "conversation.item.delete"
                and absent in self.pending_deletes
                and error.get("message") == f"Cannot find item with id: {absent}"):
            self.pending_deletes.discard(absent)
            await self.trace("context_item_already_absent", item_id=absent)
            await self.maybe_continue()
            return
        await self.fail("千问接口返回错误，请核对模型权限与参数；不会自动切换模型。")
        self.log("provider_error", code=event.get("error", {}).get("code", "unknown"))
    elif kind == "conversation.item.created":
        item = event.get("item", {})
        item_id = item.get("id")
        if item_id and item.get("role") == "assistant":
            self.context_items.add(item_id)
            for response in self.responses.values():
                if item_id in response.items and response.cancelled and response.done:
                    await self.delete_items(response)
        await self.reminders.acknowledge(item_id)
    elif kind == "input_audio_buffer.speech_started":
        self.user_speaking = True
        self.latest_turn = None
        self.reminded, self.idle_since = False, None
        await self.interrupt("speech", event.get("audio_start_ms"))
        await self.emit("state", state="listening")
    elif kind == "input_audio_buffer.speech_stopped":
        self.user_speaking = False
        if event.get("reason") != "turn_invalid":
            self.latest_turn = {"detected_at": self.clock(), "audio_end_ms": event.get("audio_end_ms")}
        await self.trace(kind, reason=event.get("reason"))
    elif kind == "conversation.item.ambient_audio_transcription.completed":
        # 附和与环境声只保留分类观测，不能进入客户转录、预约证据或触发回复。
        await self.trace(
            "ambient_transcription_completed", item_id=event.get("item_id"),
            characters=len(event.get("transcript", "")),
        )
    elif kind.startswith("conversation.item.input_audio_transcription."):
        if event.get("item_id") in self.reminders.application_items:
            return
        final = kind.endswith("completed") or kind.endswith("done")
        if kind.endswith("failed"):
            await self.trace("transcription_failed")
        elif final or kind.endswith("delta"):
            item_id = event.get("item_id", "user")
            preview = not final and ("text" in event or "stash" in event)
            # 千问 text + stash 是当前整句预览；stash 可被修订，不能按输出 delta 累加。
            text = (
                event.get("text", "") + event.get("stash", "")
                if preview else event.get("transcript", event.get("delta", ""))
            )
            previous = self.transcripts.get(item_id)
            if not final and previous and previous["final"]:
                return
            if final or (text and (not previous or not previous["text"])):
                await self.trace(
                    "user_transcription_completed" if final else "user_transcription_preview",
                    item_id=item_id, characters=len(text),
                )
            await self.transcript(
                item_id, "user", text, final, replace_text=preview,
            )
    elif kind == "response.created":
        rid = event["response"]["id"]
        self.active, self.requested = rid, False
        response = Response(rid, self.epoch, self.clock())
        stale_reminder = self.reminders.attach_response(response)
        if self.latest_turn:
            response.metric.update(self.latest_turn)
            self.latest_turn = None
        self.responses[rid] = response
        if self.repair_marker or self.pending_deletes or self.playback.pending or stale_reminder:
            # 过期提醒直接丢弃，不能在清理后又自动重答上一个客户问题。
            self.regenerate = not stale_reminder
            response.cancelled = True
            # 云端轮次检测已因真实发声取消生成，避免重复取消已结束的响应。
            if stale_reminder != "speech":
                await self.cancel_active()
        else:
            await self.emit("response_started", response_id=rid, epoch=response.epoch)
            await self.emit("state", state="thinking")
    elif kind in {"response.output_item.added", "response.output_item.done"}:
        response = self.responses.get(rid)
        item = event.get("item", {})
        if response and item.get("type") == "message" and item.get("id"):
            response.items.add(item["id"])
            if kind == "response.output_item.added":
                self.context_items.add(item["id"])
            if response.cancelled and response.done:
                await self.delete_items(response)
    elif kind == "response.audio.delta":
        response = self.responses.get(rid)
        if response and not response.cancelled:
            if response.first_audio is None:
                response.first_audio = self.clock()
                response.metric["model_first_audio_ms"] = round((self.clock() - response.started) * 1000, 1)
            response.audio_sent = True
            await self.emit("audio", response_id=rid, item_id=event.get("item_id", rid),
                            epoch=response.epoch, audio=event["delta"])
    elif kind in {"response.audio_transcript.delta", "response.audio_transcript.done"}:
        response = self.responses.get(rid)
        if response:
            item_id = event.get("item_id", rid)
            response.items.add(item_id)
            await self.transcript(
                item_id,
                "assistant",
                event.get("transcript", event.get("delta", "")),
                kind.endswith("done"),
                rid,
                response.cancelled,
            )
    elif kind == "response.function_call_arguments.done":
        response = self.responses.get(rid)
        if response:
            response.calls[event["call_id"]] = event
    elif kind == "response.done":
        raw = event["response"]
        rid = raw["id"]
        response = self.responses.get(rid)
        if not response or response.done:
            return
        response.done = True
        if self.active == rid:
            self.active = None
        for item in raw.get("output", []):
            if item.get("type") == "message":
                response.items.add(item["id"])
            elif item.get("type") == "function_call":
                response.calls.setdefault(item["call_id"], {**item, "response_id": rid})
        if raw.get("usage"):
            self.usages.append(raw["usage"])
        else:
            self.missing_usage += 1
        if raw.get("status") == "failed":
            await self.fail("模型生成失败，请重新开始。")
            return
        if raw.get("status") == "cancelled" and not response.cancelled:
            await self.interrupt("provider")
            response.cancelled = True
            self.repair_marker = True
        if response.cancelled:
            await self.delete_items(response)
        await self.reminders.finish(response)
        await self.emit(
            "response_done",
            response_id=rid,
            epoch=response.epoch,
            cancelled=response.cancelled,
            audio_sent=response.audio_sent,
        )
        for call in response.calls.values():
            cid = call["call_id"]
            if cid not in self.tool_seen:
                self.tool_seen.add(cid)
                self.tool_inflight.add(cid)
                self.spawn(self.prepare_tool(call))
        await self.maybe_continue()
    elif kind == "conversation.item.deleted":
        self.pending_deletes.discard(event.get("item_id"))
        await self.maybe_continue()
