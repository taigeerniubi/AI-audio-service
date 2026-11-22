"""定义销售工具、校验参数并检查预约确认依据。"""

import re
from uuid import uuid4

from pydantic import ValidationError

from backend.schemas import EndArgs, InterestArgs, ScheduleArgs, SearchArgs

TOOL_MODELS = {
    "search_product_knowledge": (
        SearchArgs,
        "检索当前会话知识库。用户问商品、型号、规格、兼容性或价格等细节时，先查询再回答。"
        "知识库可能包含产品档案以外的商品，不能仅凭档案名称拒答；无匹配时说明资料不足。",
    ),
    "record_interest": (InterestArgs, "记录客户明确表达的需求、意向和跟进事项。"),
    "schedule_demo": (ScheduleArgs, "仅在用户明确同意具体预约安排后保存本地预约；引用同意原话。"),
    "end_session": (EndArgs, "用户拒绝或结束时结束会话。工具成功后简短道别。"),
}


def definitions():
    """把销售工具的 Pydantic 参数模型转换为模型函数定义。

    Returns:
        用于 session.update 的工具描述和 JSON Schema 列表。
    """
    return [
        {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": model.model_json_schema()},
        }
        for name, (model, description) in TOOL_MODELS.items()
    ]


def validate(name, arguments):
    """按工具名校验 JSON 参数，拒绝未知工具和不合法字段。

    Args:
        name: 模型请求调用的工具名称。
        arguments: 模型输出的 JSON 参数字符串。

    Returns:
        通过对应 Pydantic 模型验证的参数字典。

    Raises:
        ValueError: 工具名未知或参数不符合约束。
    """
    if name not in TOOL_MODELS:
        raise ValueError("未知工具")
    try:
        return TOOL_MODELS[name][0].model_validate_json(arguments).model_dump()
    except ValidationError as exc:
        raise ValueError("工具参数不正确，请修正后重试") from exc


def prepare_result(name, arguments, transcripts, search_result=None):
    """根据已验证参数生成工具结果，并以最终用户转录核对预约同意。

    Args:
        name: 销售工具名称。
        arguments: 已通过参数模型验证的字典。
        transcripts: 本会话本地转录；预约确认只采信最终 user 文本。
        search_result: 检索工具返回的证据片段；其他工具可省略。

    Returns:
        待持久化的成功或错误结果；本函数本身不写数据库。
    """
    if name == "search_product_knowledge":
        return {"matches": search_result or [], "note": "只依据资料回答；无匹配时说明未知。"}
    if name == "schedule_demo":
        quote = arguments["confirmation_quote"]
        # Evidence must be a final user transcript, not an assistant-generated assertion.
        evidence = any(t["role"] == "user" and t.get("final") and quote in t["text"] for t in transcripts)
        affirmative = re.search(
            r"^(好的?|可以|行|没问题|确认|同意|就定|就按|帮我预约|请.*预约|安排吧)", quote
        )
        negative = re.search(r"不[要用想]|别|取消|不行|不可以|不同意|再考虑", quote)
        if not evidence or not affirmative or negative:
            return {"error": "缺少明确预约确认，请复述时间并请用户明确确认。"}
        return {"ok": True, "appointment_id": uuid4().hex, "local_only": True, **arguments}
    return {"ok": True, **arguments}
