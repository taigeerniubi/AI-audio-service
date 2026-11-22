"""校验会话创建的数据结构。"""

from typing import Literal

from pydantic import Field

from backend.schemas.base import StrictModel


class SessionInput(StrictModel):
    product_id: str
    voice: str | None = Field(default=None, min_length=1, max_length=128)
    turn_detection: Literal["server_vad", "smart_turn_v2"] | None = None
