"""以单一事件循环协调语音会话、打断、工具提交和资源释放。"""

import asyncio
import base64
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import WebSocketDisconnect

from backend.context.factory import ContextFactory
from backend.llm.factory import LLMFactory
from backend.llm.qwen.embedding import EmbeddingError
from backend.models.realtime import Response
from backend.services.knowledge.retrieval import Knowledge
from backend.services.realtime.client_events import handle_client_event
from backend.services.realtime.connection import CLIENT_INACTIVITY_SECONDS, connection_diagnostic
from backend.services.realtime.metrics import estimate_cost, percentiles
from backend.services.realtime.model_events import handle_model_event
from backend.services.realtime.playback import PlaybackController
from backend.services.realtime.reminders import ReminderController
from backend.services.sales_tools import prepare_result, validate


class LiveSession:
    """持有一次实时对话的状态，所有业务提交和上下文修正均由串行事件循环协调。"""
    def __init__(
        self,
        session_id,
        snapshot,
        settings,
        store,
        browser,
        transport=None,
        clock=time.monotonic,
        llm_factory=None,
    ):
        """初始化一个会话的模型连接、产品快照、事件队列与播放状态。

        Args:
            session_id: 会话持久化记录的唯一 ID。
            snapshot: 创建时固定的产品字段、文档和向量。
            settings: 模型、RAG 和会话计时配置。
            store: 保存最终报告和幂等工具结果的存储。
            browser: 当前会话的浏览器 WebSocket。
            transport: 可选实时传输替身；留空时由工厂创建。
            clock: 单调时钟，可由测试注入。
            llm_factory: 可选模型工厂，不在会话间复用实时连接。
        """
        settings = replace(settings, **snapshot.get("session_options", {}))
        self.id, self.snapshot, self.settings, self.store = session_id, snapshot, settings, store
        self.browser = browser
        self.llm = llm_factory or LLMFactory(settings)
        self.context = ContextFactory(settings)
        self.transport = transport or self.llm.create_realtime()
        self.clock = clock
        self.born = clock()
        self.queue = asyncio.Queue(maxsize=256)
        self.responses: dict[str, Response] = {}
        self.active = None
        self.ready = False
        self.requested = False
        self.user_speaking = False
        self.muted = False
        self.epoch = 0
        self.idle_since = None
        self.reminded = False
        self.ending = None
        self.closed = False
        self.reason = "client_closed"
        self.transcripts = {}
        self.events = []
        self.usages = []
        self.missing_usage = 0
        self.metrics = []
        self.interruptions = {}
        self.pending_deletes = set()
        self.context_items = set()
        self.deleted = set()
        self.repair_marker = False
        self.regenerate = False
        self.tasks = set()
        self.tool_inflight = set()
        self.tool_seen = set()
        self.tool_replied = set()
        self.tool_followup = False
        self.latest_turn = None
        self.audio_ms = 0
        self.audio_timeline = []
        self.knowledge = None
        self.last_model_event = self.clock()
        self.last_client_activity: float | None = None
        self.reminders = ReminderController(self)
        self.playback = PlaybackController(self)

    def log(self, kind, **data):
        # No audio payloads or credentials in trace.
        """记录事件时间并保留最近三千条诊断事件。

        Args:
            kind: 诊断事件名称。
            **data: 已经由调用方筛选的字段，不应包含音频或凭证。

        Returns:
            同时带相对毫秒数和 UTC 时间的事件。
        """
        entry = {
            "type": kind,
            "elapsed_ms": round((self.clock() - self.born) * 1000, 1),
            "utc": datetime.now(timezone.utc).isoformat(),
            **data,
        }
        self.events.append(entry)
        self.events = self.events[-3000:]
        return entry

    async def emit(self, kind, **data):
        """给浏览器发送带会话 ID 的事件，并限制发送等待时间。

        Args:
            kind: 浏览器协议事件名称。
            **data: 字幕、音频、状态或业务结果字段。
        """
        await asyncio.wait_for(self.browser.send_json({"type": kind, "session_id": self.id, **data}), 2)

    async def trace(self, kind, **data):
        """保存诊断事件并同步到浏览器技术面板。

        Args:
            kind: 诊断事件名称。
            **data: 允许进入日志的脱敏元信息。
        """
        await self.emit("trace", event=self.log(kind, **data))

    def spawn(self, coroutine):
        """登记会话后台任务，便于退出时统一取消。

        Args:
            coroutine: 待执行的异步读取或工具准备协程。

        Returns:
            已启动并登记完成回调的 asyncio Task。
        """
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def enqueue(self, kind, data):
        """将外部事件送入有界队列，交给会话循环串行处理。

        Args:
            kind: 模型、客户端、音频、工具或连接事件类别。
            data: 对应类别的事件载荷。
        """
        await asyncio.wait_for(self.queue.put((kind, data)), 2)

    async def read_model(self):
        """持续接收模型事件，断线时向会话循环提交脱敏失败事件。"""
        try:
            while not self.closed:
                await self.enqueue("model", await self.transport.receive())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log("connection_closed", **connection_diagnostic("model", exc))
            await self.enqueue(
                "failure", {"message": "模型连接中断，请重新开始。", "reason": "model_disconnected"}
            )

    async def read_browser(self):
        """读取音频和控制帧，校验帧大小并记录客户端活跃时间。"""
        try:
            while not self.closed:
                message = await self.browser.receive()
                if message["type"] == "websocket.disconnect":
                    diagnostic = connection_diagnostic("browser", code=message.get("code"))
                    self.log("connection_closed", **diagnostic)
                    reason = (
                        "client_closed" if diagnostic["close_code"] in {1000, 1001} else "client_disconnected"
                    )
                    await self.enqueue("close", reason)
                    return
                self.last_client_activity = self.clock()
                if message.get("bytes") is not None:
                    data = message["bytes"]
                    if len(data) != 640:
                        await self.enqueue("failure", "音频帧必须为 20ms、16kHz PCM16 单声道。")
                        return
                    await self.enqueue("audio", data)
                elif message.get("text"):
                    if len(message["text"]) > 8192:
                        raise ValueError("frame too large")
                    await self.enqueue("client", json.loads(message["text"]))
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, RuntimeError) as exc:
            diagnostic = connection_diagnostic("browser", exc, getattr(exc, "code", None))
            self.log("connection_closed", **diagnostic)
            reason = "client_closed" if diagnostic["close_code"] in {1000, 1001} else "client_disconnected"
            await self.enqueue("close", reason)
        except Exception:
            await self.enqueue("failure", "客户端数据无效或发送积压，请重新开始。")

    async def run(self):
        """构建快照检索器并驱动会话循环，退出时清理任务和保存报告。"""
        try:
            self.last_client_activity = self.clock()
            embedder = self.llm.create_embedding(
                on_usage=lambda usage: self.store.log_embedding(self.id, usage)
            )
            self.knowledge = await asyncio.to_thread(
                Knowledge, self.snapshot["documents"], self.settings, embedder
            )
            await self.transport.open()
            self.spawn(self.read_model())
            self.spawn(self.read_browser())
            while not self.closed:
                try:
                    kind, data = await asyncio.wait_for(self.queue.get(), 0.2)
                except asyncio.TimeoutError:
                    kind, data = "tick", None
                if kind == "model":
                    await self.on_model(data)
                elif kind == "client":
                    await self.on_client(data)
                elif kind == "audio" and self.ready:
                    audio = bytes(len(data)) if self.muted else data
                    self.audio_ms += 20
                    self.audio_timeline.append((self.audio_ms, self.clock()))
                    await self.transport.send(
                        {"type": "input_audio_buffer.append", "audio": base64.b64encode(audio).decode()}
                    )
                elif kind == "tool":
                    await self.commit_tool(data)
                elif kind == "close":
                    self.reason, self.closed = data, True
                elif kind == "failure":
                    if isinstance(data, dict):
                        await self.fail(data["message"], reason=data["reason"])
                    else:
                        await self.fail(data)
                await self.tick()
        except asyncio.CancelledError:
            self.reason = "server_shutdown"
            raise
        except Exception as exc:
            # Do not return SDK exception strings: they may contain authenticated URLs/headers.
            if not self.closed:
                self.reason = "connection_error"
            self.log("failure", category=type(exc).__name__)
            try:
                message = (
                    "语音连接异常，请重新开始。"
                    if self.ready
                    else "无法连接千问。请检查网络、地域、模型权限和后端凭证配置。"
                )
                await self.emit("error", message=message)
            except Exception:
                pass
        finally:
            self.closed = True
            for task in list(self.tasks):
                task.cancel()
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
            try:
                await self.transport.close()
            except Exception as exc:
                self.log("transport_close_error", category=type(exc).__name__)
            self.store.finish(self.id, self.report())
            try:
                await self.emit("ended", reason=self.reason)
                await self.browser.close()
            except Exception:
                pass

    def instructions(self):
        """读取由上下文工厂组装的当前产品固定指令。

        Returns:
            销售规则与产品快照的组合文本。
        """
        return self.context.instructions(self.snapshot["product"])

    async def create_response(self):
        """仅在无生成、无用户发言且无待确认删除时请求模型回答。

        Returns:
            成功发出生成请求时为 True；条件不满足时为 False。
        """
        if self.active or self.requested or self.user_speaking or self.pending_deletes or self.playback.pending:
            return False
        self.requested = True
        await self.transport.send({"type": "response.create"})
        return True

    async def system_note(self, text):
        """向模型历史追加由应用生成的系统说明。

        Args:
            text: 可信的上下文修正说明，不接受客户文本直接充当系统指令。
        """
        await self.transport.send(self.context.system_note(text))

    async def on_model(self, event):
        """把模型协议事件交给统一分发器更新会话状态。

        Args:
            event: 已解码的千问服务端事件。
        """
        await handle_model_event(self, event)

    async def transcript(self, item_id, role, text, final, rid=None, interrupted=False, *, replace_text=False):
        """按对话项累积增量字幕或覆盖最终文本，并通知浏览器。

        Args:
            item_id: 模型对话项 ID，用于合并同一条字幕。
            role: user 或 assistant。
            text: 转录增量或最终全文。
            final: 为 True 时用全文覆盖增量，并标记可用于业务确认。
            rid: 可选助手响应 ID。
            interrupted: 该回复是否已被打断；已打断标记不会被后续字幕清除。
            replace_text: 用当前整句预览替换旧文本，仍保持未完成状态。
        """
        response = self.responses.get(rid)
        if role == "assistant" and response and response.cancelled:
            return
        old = self.transcripts.get(
            item_id,
            {
                "id": item_id,
                "role": role,
                "text": "",
                "final": False,
                "response_id": rid,
                "interrupted": False,
            },
        )
        old["text"] = text if final or replace_text else old["text"] + text
        old["final"] = final
        old["interrupted"] = old["interrupted"] or interrupted
        if response:
            old["epoch"] = response.epoch
        self.transcripts[item_id] = old
        await self.emit("transcript", message=old)

    async def cancel_active(self):
        """只取消仍在生成且尚未发送取消的响应，避免重复取消报错。"""
        response = self.responses.get(self.active)
        if response and not response.done and not response.cancel_sent:
            response.cancel_sent = True
            await self.transport.send({"type": "response.cancel"})

    async def interrupt(self, source, audio_start_ms=None):
        """提升播放版本、清空旧音频并安排删除未完整播放的助手历史。

        Args:
            source: speech、button 或 provider；语音入口由云端轮次检测负责取消。
            audio_start_ms: 模型报告的可选用户起声时间。
        """
        await self.reminders.cancel(source)
        targets = [
            r
            for r in self.responses.values()
            if not r.cancelled and (not r.done or (r.audio_sent and not r.played))
        ]
        self.epoch += 1
        iid = uuid4().hex
        self.interruptions[iid] = {
            "sent": self.clock(),
            "audio_start_ms": audio_start_ms,
            "source": source,
            "had_output": bool(targets),
            "response_ids": [r.id for r in targets],
        }
        self.playback.begin(iid, [r.id for r in targets])
        await self.emit("clear_audio", epoch=self.epoch, interruption_id=iid, source=source)
        for response in targets:
            response.cancelled = True
            self.repair_marker = True
            for item in self.transcripts.values():
                if item.get("response_id") == response.id:
                    item["interrupted"] = True
                    await self.emit("transcript", message=item)
            if response.done:
                await self.delete_items(response)
        if targets:
            # Server turn detection already cancels on speech; a duplicate cancel can
            # race response.done and produce "no active response" from the provider.
            if source == "button":
                await self.cancel_active()
            await self.trace("interrupted", source=source, response_ids=[r.id for r in targets])
        await self.maybe_continue()

    async def delete_context_item(self, item_id: str) -> None:
        """幂等发送对话项删除并登记待确认项，由事件循环等待确认。

        Args:
            item_id: 已经确认存在且需要从云端历史移除的对话项 ID。
        """
        if item_id not in self.deleted:
            self.deleted.add(item_id)
            self.pending_deletes.add(item_id)
            await self.transport.send({"type": "conversation.item.delete", "item_id": item_id})

    async def delete_items(self, response):
        """只清理已确认创建的助手消息，取消时的空结束事件可能携带不存在的 ID。

        Args:
            response: 包含助手对话项 ID 集合的响应状态。
        """
        for item_id in (response.items & self.context_items) - self.deleted:
            await self.delete_context_item(item_id)

    async def maybe_continue(self):
        """等待生成、工具和历史删除结束，再补修正说明或继续回答。"""
        if self.active or self.requested or self.pending_deletes or self.tool_inflight or self.playback.pending:
            return
        if self.repair_marker:
            await self.transport.send(self.context.interruption_note(self.playback.take_prefixes()))
            self.repair_marker = False
        if not self.user_speaking and (self.tool_followup or self.regenerate):
            self.tool_followup, self.regenerate = False, False
            await self.create_response()

    async def prepare_tool(self, call):
        """异步校验工具参数并准备检索结果，不在此阶段提交业务写入。

        Args:
            call: 包含调用 ID、响应 ID、工具名及 JSON 参数的模型调用。
        """
        try:
            response = self.responses.get(call["response_id"])
            if response and response.purpose == "idle_reminder":
                await self.enqueue("tool", {"call": call, "error": "静音提醒不执行销售工具。"})
                return
            args = validate(call["name"], call["arguments"])
            search = None
            if call["name"] == "search_product_knowledge":
                search = await asyncio.wait_for(asyncio.to_thread(self.knowledge.search, args["query"]), 8)
            await self.enqueue("tool", {"call": call, "args": args, "search": search})
        except asyncio.CancelledError:
            raise
        except EmbeddingError as exc:
            await self.enqueue("tool", {"call": call, "error": str(exc)})
        except Exception:
            await self.enqueue("tool", {"call": call, "error": "工具参数无效或执行超时，请澄清后重试。"})

    async def commit_tool(self, prepared):
        """串行检查轮次取消状态、幂等提交业务结果并回填模型历史。

        Args:
            prepared: 准备任务返回的调用、参数、检索结果或明确错误。
        """
        call = prepared["call"]
        cid, rid, name = call["call_id"], call["response_id"], call["name"]
        self.tool_inflight.discard(cid)
        if cid in self.tool_replied:
            return
        response = self.responses.get(rid)
        cancelled = not response or response.cancelled
        args = prepared.get("args", {})
        # No await between cancellation check and transaction: one actor owns all writes.
        if cancelled:
            result = {"error": "本轮已取消，未执行此操作。"}
        elif response.purpose == "idle_reminder":
            result = {"error": "静音提醒不执行销售工具。"}
        elif "error" in prepared:
            result = {"error": prepared["error"]}
        else:
            result = prepare_result(name, args, list(self.transcripts.values()), prepared.get("search"))
            result = self.store.action(self.id, cid, name, args, result)
            if name == "end_session" and result.get("ok"):
                self.ending = args["reason"]
        self.tool_replied.add(cid)
        await self.transport.send(self.context.tool_result(cid, result))
        await self.emit("tool_result", name=name, result=result, cancelled=cancelled)
        await self.trace("tool_result", name=name, cancelled=cancelled, ok="error" not in result)
        if not cancelled and response.purpose != "idle_reminder":
            self.tool_followup = True
        await self.maybe_continue()

    async def on_client(self, event):
        """处理播放确认、打断、静音和心跳等客户端事件。

        Args:
            event: 已解码的浏览器控制事件。
        """
        await handle_client_event(self, event)

    def is_idle(self):
        """检查模型、用户、工具及播放均空闲且麦克风开启。

        Returns:
            可以开始等待用户并累计静音时间时为 True。
        """
        return (
            self.ready
            and not self.active
            and not self.requested
            and not self.user_speaking
            and not self.muted
            and not self.tool_inflight
            and not self.pending_deletes
            and not self.playback.pending
            and not self.reminders.pending
            and not any(r.audio_sent and not r.played and not r.cancelled for r in self.responses.values())
        )

    async def tick(self):
        """检查会话上限、连接超时与静音策略，并推进提醒或结束状态。"""
        if self.closed:
            return
        await self.playback.expire()
        await self.reminders.check_timeout()
        if self.closed:
            return
        if self.clock() - self.born >= self.settings.max_seconds:
            self.reason, self.closed = "time_limit", True
            return
        if (
            self.ready
            and self.last_client_activity is not None
            and self.clock() - self.last_client_activity >= CLIENT_INACTIVITY_SECONDS
        ):
            self.log(
                "connection_timeout",
                side="browser",
                inactive_seconds=round(self.clock() - self.last_client_activity, 1),
            )
            await self.fail("客户端连接长时间无响应，会话已结束，请重新开始。", reason="client_timeout")
            return
        if not self.ready and self.clock() - self.born > 15:
            await self.fail("模型初始化超时，请检查配置和网络。")
            return
        if (
            self.active or self.requested or self.pending_deletes
        ) and self.clock() - self.last_model_event > 30:
            await self.fail("模型响应或上下文确认超时，请重新开始。")
            return
        if self.is_idle():
            if self.ending:
                self.reason, self.closed = self.ending, True
                return
            if self.idle_since is None:
                self.idle_since = self.clock()
                await self.emit("state", state="listening")
            elapsed = self.clock() - self.idle_since
            if not self.reminded and elapsed >= self.settings.reminder_seconds:
                await self.reminders.begin()
            elif self.reminded and elapsed >= self.settings.idle_close_seconds:
                self.reason, self.closed = "idle_timeout", True
        else:
            self.idle_since = None

    async def fail(self, message, reason="error"):
        """标记会话结束原因并向浏览器显示可公开的错误。

        Args:
            message: 不含凭证或内部堆栈的错误文案。
            reason: 写入最终报告的结束原因。
        """
        self.reason, self.closed = reason, True
        await self.emit("error", message=message)

    def report(self):
        """汇总本地转录、事件、业务结果、可观测延迟和已报告用量。

        Returns:
            可存入数据库或返回浏览器的报告；不作为新会话的历史输入。
        """
        return {
            "session_id": self.id,
            "product": self.snapshot["product"],
            "model": self.settings.model,
            "voice": self.settings.voice,
            "turn_detection": self.settings.turn_detection_config(),
            "reason": self.reason,
            "duration_seconds": round(self.clock() - self.born, 1),
            "transcripts": [item for item in self.transcripts.values()
                            if item["text"] or not item.get("interrupted")],
            "events": self.events,
            "metrics": self.metrics,
            "usage": self.usages,
            "embedding_usage": self.store.embedding_usage(self.id),
            "missing_usage_responses": self.missing_usage,
            "estimated_cost_cny": estimate_cost(self.usages, self.settings.model, self.settings.region),
            "cost_note": "仅语音模型已返回 usage 的估算，不含另记的 Embedding 用量；取消响应可能缺失用量，最终以云账单为准。",
            "latency": percentiles([m.get("speech_stop_event_to_play_ack_ms") for m in self.metrics]),
            "measurement_note": "服务端检测结束事件至收到播放确认，含网络往返；非物理声学端到端时延。",
            "actions": self.store.actions(self.id),
        }
