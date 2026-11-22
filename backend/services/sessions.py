"""管理语音会话的创建、独占启动、报告读取和运行实例。"""

from dataclasses import replace

from backend.core.errors import ServiceError
from backend.llm.qwen.voices import voice_options
from backend.services.realtime.session import LiveSession


class SessionService:
    """在应用内维护活动会话，所有会话分别拥有模型连接。"""

    def __init__(self, settings, store, llm_factory):
        """绑定应用配置、存储和模型工厂。

        Args:
            settings: 当前应用的模型、检索和计时配置。
            store: 当前应用的数据库存储服务。
            llm_factory: 按会话构造独立模型客户端的工厂。
        """
        self.settings, self.store, self.llm = settings, store, llm_factory
        self.active: dict[str, LiveSession] = {}

    def create(self, product_id: str, voice: str | None = None, turn_detection: str | None = None) -> dict:
        """校验模型配置与容量，创建不可变的产品知识快照。

        Args:
            product_id: 目标产品的唯一 ID。
            voice: 首次连接使用的音色；省略时采用后端默认值。
            turn_detection: 首次连接使用的轮次模式；省略时采用后端默认值。

        Returns:
            新建会话 ID 与一次性启动路径。
        """
        if self.settings.missing():
            raise ServiceError(503, "尚未配置 " + "、".join(self.settings.missing()) + "。请编辑本地 .env 并重启后端。")
        try:
            self.settings.url()
        except ValueError as exc:
            raise ServiceError(503, str(exc)) from None
        if len(self.active) >= 4:
            raise ServiceError(429, "本地演示最多同时运行 4 个会话")
        voice = voice or self.settings.voice
        if voice not in {row["id"] for row in voice_options(self.settings.voice)}:
            raise ServiceError(422, "所选音色不在当前支持列表中")
        options = {"voice": voice, "turn_detection": turn_detection or self.settings.turn_detection,
                   "vad_silence_ms": self.settings.vad_silence_ms}
        try:
            replace(self.settings, **options).turn_detection_config()
        except ValueError as exc:
            raise ServiceError(422, str(exc)) from None
        try:
            session_id = self.store.create_session(product_id, options)
        except KeyError:
            raise ServiceError(404, "产品不存在") from None
        return {"id": session_id, "websocket_path": f"/ws/sessions/{session_id}"}

    def report(self, session_id: str) -> dict:
        """读取运行中的报告，或返回已保存的最终报告。

        Args:
            session_id: 目标语音会话的唯一 ID。

        Returns:
            运行中或已持久化的会话报告。
        """
        session = self.store.session(session_id)
        if not session:
            raise ServiceError(404, "会话不存在")
        if session_id in self.active:
            return self.active[session_id].report()
        return session["report"] or {"session_id": session_id, "status": session["status"], "transcripts": [], "metrics": [], "actions": [], "events": []}

    def attach(self, session_id: str, browser) -> LiveSession | None:
        """原子认领会话并预留容量，返回独立的会话协调器。

        Args:
            session_id: 尚未运行的会话 ID。
            browser: 提供收发消息能力的客户端连接。

        Returns:
            已登记的会话；无效、重复启动或容量不足时返回空值。
        """
        session = self.store.session(session_id)
        if not session or len(self.active) >= 4 or not self.store.claim_session(session_id):
            return None
        live = LiveSession(session_id, session["snapshot"], self.settings, self.store, browser, llm_factory=self.llm)
        self.active[session_id] = live
        return live

    def release(self, session_id: str) -> None:
        """从活动集合移除已清理的会话。

        Args:
            session_id: 目标语音会话的唯一 ID。
        """
        self.active.pop(session_id, None)

    def shutdown(self) -> None:
        """通知所有会话停止事件循环并执行各自的资源清理。"""
        for live in list(self.active.values()):
            live.reason, live.closed = "server_shutdown", True
