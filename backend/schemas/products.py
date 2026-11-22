"""校验产品档案的数据结构。"""

from pydantic import Field

from backend.schemas.base import StrictModel


class ProductInput(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    audience: str = Field(default="", max_length=2000)
    features: str = Field(default="", max_length=6000)
    pricing: str = Field(default="", max_length=3000)
    limitations: str = Field(default="", max_length=3000)
    opening: str = Field(default="您好，我是知答的 AI 产品顾问。您目前最想了解哪类产品？", max_length=500)
    goal: str = Field(default="了解需求并预约产品演示，发送产品信息链接挂断电话后", max_length=1000)
    style: str = Field(default="自然、简洁，每次只问一个问题，会推销自家的产品。", max_length=1000)
