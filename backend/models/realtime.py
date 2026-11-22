"""保存单次助手响应的生成、播放、取消和工具状态。"""

from dataclasses import dataclass, field


@dataclass
class Response:
    """跟踪一个模型响应及其对应的播放版本。"""

    id: str
    epoch: int
    started: float
    items: set = field(default_factory=set)
    done: bool = False
    cancelled: bool = False
    cancel_sent: bool = False
    audio_sent: bool = False
    played: bool = False
    first_audio: float | None = None
    calls: dict = field(default_factory=dict)
    metric: dict = field(default_factory=dict)
    purpose: str = "conversation"
    application_item_id: str | None = None


@dataclass
class ReminderRequest:
    """跟踪静音控制消息的确认、生成请求和取消状态。"""

    item_id: str
    sent_at: float
    acknowledged: bool = False
    generation_requested: bool = False
    cancelled: bool = False
    cancel_reason: str | None = None
