"""协调静音提醒的消息确认、用户恢复说话时的撤销及上下文清理。"""

import re
from typing import TYPE_CHECKING

from backend.context.prompts import SILENCE_REMINDER_TEXT
from backend.models.realtime import ReminderRequest, Response

if TYPE_CHECKING:
    from backend.services.realtime.session import LiveSession


class ReminderController:
    """由会话事件循环调用，不另开计时器或并发修改会话状态。"""

    def __init__(self, live: "LiveSession"):
        """绑定所属会话并初始化应用控制消息的去重集合。

        Args:
            live: 本提醒控制器所属的实时会话。
        """
        self.live = live
        self.pending: ReminderRequest | None = None
        self.application_items: set[str] = set()

    async def begin(self) -> None:
        """发送提醒输入并等待服务端确认，期间不重复请求生成。"""
        if self.pending or not self.live.is_idle():
            return
        event = self.live.context.silence_reminder()
        item_id = event["item"]["id"]
        self.pending = ReminderRequest(item_id, self.live.clock())
        self.application_items.add(item_id)
        self.live.idle_since = None
        await self.live.transport.send(event)
        await self.live.trace("idle_reminder_queued", item_id=item_id)

    async def acknowledge(self, item_id: str | None) -> None:
        """仅对本次控制消息的确认触发生成；过期消息只做删除。

        Args:
            item_id: conversation.item.created 中的对话项 ID。
        """
        pending = self.pending
        if not pending or pending.item_id != item_id or pending.acknowledged:
            return
        pending.acknowledged = True
        live = self.live
        if (
            pending.cancelled
            or live.user_speaking
            or live.muted
            or live.closed
            or live.ending
            or live.active
            or live.requested
            or live.pending_deletes
            or live.tool_inflight
        ):
            self.pending = None
            await live.delete_context_item(pending.item_id)
            await live.trace("idle_reminder_discarded", item_id=pending.item_id)
            return
        pending.generation_requested = await live.create_response()
        if pending.generation_requested:
            live.reminded = True
            await live.trace("idle_reminder", item_id=pending.item_id, expected_text=SILENCE_REMINDER_TEXT)

    async def cancel(self, reason: str) -> None:
        """撤销未开始播放的提醒，避免迟到确认触发过期话术。

        Args:
            reason: 用户恢复说话、手动打断或麦克风静音等取消原因。
        """
        pending = self.pending
        if not pending or pending.cancelled:
            return
        pending.cancelled = True
        pending.cancel_reason = reason
        if pending.acknowledged:
            await self.live.delete_context_item(pending.item_id)
            if not pending.generation_requested:
                self.pending = None
        await self.live.trace("idle_reminder_cancelled", item_id=pending.item_id, reason=reason)

    def attach_response(self, response: Response) -> str | None:
        """关联已请求的提醒响应，返回已过期提醒的取消原因。

        Args:
            response: 刚由 response.created 创建的响应状态。

        Returns:
            过期时返回取消原因；有效提醒或普通响应返回空值。
        """
        pending = self.pending
        if not pending:
            return None
        if not pending.generation_requested:
            # 用户的自动响应先到时，迟到的控制消息确认不再触发另一轮。
            pending.cancelled = True
            pending.cancel_reason = "response_in_progress"
            return None
        response.purpose = "idle_reminder"
        response.application_item_id = pending.item_id
        self.pending = None
        return pending.cancel_reason if pending.cancelled else None

    async def finish(self, response: Response) -> None:
        """删除已经使用的提醒输入，并记录实际输出是否符合固定话术。

        Args:
            response: 当前模型响应及其播放、取消和用途状态。
        """
        if response.purpose != "idle_reminder":
            return
        if response.application_item_id:
            await self.live.delete_context_item(response.application_item_id)
        text = "".join(
            item["text"] for item in self.live.transcripts.values() if item.get("response_id") == response.id
        )
        await self.live.trace(
            "idle_reminder_result",
            response_id=response.id,
            interrupted=response.cancelled,
            matches_expected=re.sub(r"[\W_]", "", text) == re.sub(r"[\W_]", "", SILENCE_REMINDER_TEXT),
            actual_text=text,
        )

    async def check_timeout(self) -> None:
        """控制消息五秒内未获确认时明确结束，不盲目重试生成。"""
        pending = self.pending
        if pending and not pending.acknowledged and self.live.clock() - pending.sent_at >= 5:
            await self.live.fail("静音提醒的上下文确认超时，请重新开始。")
