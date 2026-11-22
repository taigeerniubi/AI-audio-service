"""校验销售工具的数据结构。"""

from typing import Literal

from pydantic import Field

from backend.schemas.base import StrictModel


class SearchArgs(StrictModel):
    query: str = Field(min_length=1, max_length=500)


class InterestArgs(StrictModel):
    needs: str = Field(max_length=2000)
    level: Literal["high", "medium", "low", "declined"]
    follow_up: str = Field(default="", max_length=2000)


class ScheduleArgs(StrictModel):
    when: str = Field(min_length=1, max_length=200)
    contact: str = Field(default="", max_length=200)
    confirmation_quote: str = Field(
        min_length=1, max_length=500, description="用户明确同意预约的原话，必须来自本会话最终转录"
    )


class EndArgs(StrictModel):
    reason: str = Field(min_length=1, max_length=300)
