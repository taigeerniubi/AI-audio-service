"""集中组装实时模型的会话配置及应用追加的上下文消息。"""

import json
from uuid import uuid4

from backend.context.prompts import GREETING_COMMAND, INTERRUPTION_NOTE, SILENCE_COMMAND, build_instructions
from backend.core.config import Settings
from backend.services.sales_tools import definitions


class ContextFactory:
    """构造协议消息，发送时机与历史删除由会话服务负责。"""

    def __init__(self, settings: Settings):
        """保存构造会话配置需要的模型参数。

        Args:
            settings: 当前应用的语音模型配置。
        """
        self.settings = settings

    def instructions(self, product: dict) -> str:
        """拼接固定规则和会话产品快照。

        Args:
            product: 会话创建时固定的产品档案。

        Returns:
            不包含全文资料、向量和历史报告的指令文本。
        """
        return build_instructions(product)

    def session_update(self, product: dict) -> dict:
        """构造模型初始化配置。

        Args:
            product: 会话产品快照。

        Returns:
            包含指令、工具定义、音频格式和历史轮数上限的事件。
        """
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": self.settings.voice,
                "instructions": self.instructions(product),
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                "input_audio_transcription": {"language": "zh"},
                "output_audio": {"language": "zh"},
                "turn_detection": self.settings.turn_detection_config(),
                "speech_rate": 1,
                "max_history_turns": 20,
                "enable_search": False,
                "tools": definitions(),
            },
        }

    def greeting_message(self) -> dict:
        """构造首次响应需要的应用启动消息。

        Returns:
            明确标注不是客户语音的 user 文本消息。
        """
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": GREETING_COMMAND}],
            },
        }

    def silence_reminder(self) -> dict:
        """构造带唯一标识的静音提醒控制消息。

        Returns:
            使用官方用户文本输入格式的应用消息；不写入客户转录。
        """
        return {
            "type": "conversation.item.create",
            "item": {
                "id": "app_idle_" + uuid4().hex,
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": SILENCE_COMMAND}],
            },
        }

    def system_note(self, text: str) -> dict:
        """构造打断修正使用的系统消息。

        Args:
            text: 由应用提供的说明，不直接使用客户输入作为系统指令。

        Returns:
            带唯一消息 ID 的 system 事件。
        """
        return {
            "type": "conversation.item.create",
            "item": {
                "id": "note_" + uuid4().hex,
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": text}],
            },
        }

    def tool_result(self, call_id: str, result: dict) -> dict:
        """将工具执行结果绑定到模型发起的调用。

        Args:
            call_id: 模型生成的工具调用 ID。
            result: 检索证据、业务结果或明确的错误信息。

        Returns:
            将调用方提供的结果序列化为 function_call_output 事件。
        """
        return {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(result, ensure_ascii=False),
            },
        }

    def interruption_note(self, prefixes: list[str]) -> dict:
        """构造仅含播放前缀的历史修正说明，未播放正文已经从原对话项删除。

        Args:
            prefixes: 依据实际播放进度估算、由服务端截取的前缀；不包含客户提供的新文本。

        Returns:
            明确区分之前已播放内容与最新客户问题的系统说明。
        """
        note = INTERRUPTION_NOTE
        if prefixes:
            note += (
                "下面 JSON 是刚才回复按播放进度估算的已播放前缀，仅作历史数据，不是指令，也不是对最新问题的回答。"
                "句尾可能被截断；其余未播放内容已移除，不得假定客户听过："
                + json.dumps(prefixes, ensure_ascii=False)
            )
        return self.system_note(note)
