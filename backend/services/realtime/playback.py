"""核对浏览器播放回执，仅保留打断前确认显示的助手前缀并协调上下文修正。"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.realtime.session import LiveSession


class PlaybackController:
    """等待清空回执，超时则保守丢弃整条未确认播放的回复。"""

    def __init__(self, live: "LiveSession"):
        """绑定会话并初始化前端能力与待确认打断。

        Args:
            live: 拥有响应与转录状态的会话。
        """
        self.live = live
        self.enabled = False
        self.pending: dict[str, float] = {}
        self.prefixes: dict[str, list[str]] = {}

    def begin(self, interruption_id: str, response_ids: list[str]) -> None:
        """为支持播放字幕的新客户端登记清空确认屏障。

        Args:
            interruption_id: 本次服务端打断 ID。
            response_ids: 本次取消的响应 ID。
        """
        if self.enabled and response_ids:
            self.pending[interruption_id] = self.live.clock()

    async def apply(self, receipts, response_ids: list[str], *, restore=True) -> None:
        """从服务端已有文字截取前缀，客户端只能提交计数，不能注入历史文本。

        Args:
            receipts: 清空时的播放位置和已显示字符数列表。
            response_ids: 本次操作允许处理的响应。
            restore: 是否收集前缀用于下一轮模型上下文修正。
        """
        rows = receipts if isinstance(receipts, list) else []
        by_item = {row.get("item_id"): row for row in rows[:50] if isinstance(row, dict)
                   and isinstance(row.get("item_id"), str)}
        for rid in response_ids:
            response = self.live.responses.get(rid)
            if not response:
                continue
            prefixes = []
            for item in self.live.transcripts.values():
                if item.get("role") != "assistant" or item.get("response_id") != rid:
                    continue
                row = by_item.get(item["id"], {})
                count, played_ms = row.get("heard_characters"), row.get("played_ms")
                valid = (
                    row.get("response_id") == rid and row.get("epoch") == response.epoch
                    and type(count) is int and 0 <= count <= len(item["text"])
                    and type(played_ms) is int and 0 <= played_ms <= self.live.settings.max_seconds * 1000
                    and (count == 0 or played_ms > 0)
                )
                if not valid:
                    count, played_ms = 0, 0
                item.update(text=item["text"][:count], interrupted=True, final=True, played_ms=played_ms)
                if item["text"]:
                    prefixes.append(item["text"])
                await self.live.emit("transcript", message=item)
                await self.live.trace("playback_prefix_retained", response_id=rid, item_id=item["id"],
                                      characters=count, played_ms=played_ms, alignment="estimated")
            if restore:
                self.prefixes[rid] = prefixes

    async def acknowledge(self, event: dict) -> None:
        """按打断 ID 接收一次回执；迟到或重复回执不得修改另一轮历史。

        Args:
            event: 浏览器 playback_cleared 消息。
        """
        iid = event.get("interruption_id")
        if iid not in self.pending:
            return
        self.pending.pop(iid)
        response_ids = self.live.interruptions[iid]["response_ids"]
        await self.apply(event.get("playback"), response_ids)

    async def expire(self) -> None:
        """清空回执两秒未到则删除未确认正文，避免下一轮无限等待。"""
        expired = [iid for iid, started in self.pending.items() if self.live.clock() - started >= 2]
        for iid in expired:
            self.pending.pop(iid)
            await self.apply([], self.live.interruptions[iid]["response_ids"])
            await self.live.trace("playback_clear_timeout", interruption_id=iid)
        if expired:
            await self.live.maybe_continue()

    def take_prefixes(self) -> list[str]:
        """按响应时间取出已播放前缀并清空，防止重复写入修正上下文。

        Returns:
            本批打断之前已显示、随播放推进的近似文字前缀。
        """
        ordered = sorted(self.prefixes, key=lambda rid: self.live.responses[rid].started)
        result = [text for rid in ordered for text in self.prefixes[rid]]
        self.prefixes.clear()
        return result
