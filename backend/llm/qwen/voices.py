"""提供已核对官方文档的千问 3.1 实时音色及后端配置的额外音色。"""

# https://help.aliyun.com/zh/model-studio/qwen-audio-tts-voice-list
# https://help.aliyun.com/zh/model-studio/fun-audiochat-client-events
DEFAULT_VOICE = "longanhuan_v3.1"
RETIRED_VOICES = frozenset({"longanqian_v3.1"})

VOICES = (
    ("longanhuan_v3.1", "龙安欢 · 女声"),
    ("longanlingxin_v3.1", "龙安灵心 · 女声"),
    ("longanfengyue_v3.1", "龙安风悦 · 女声"),
    ("xunanchuan_v3.1", "许南川 · 男声"),
)


def voice_options(default: str) -> list[dict[str, str]]:
    """返回可选音色；允许管理员通过环境变量指定复刻音色。

    Args:
        default: 仅从后端配置读取的默认音色 ID。

    Returns:
        新列表，调用者修改不会影响其他会话。
    """
    options = [{"id": key, "name": name} for key, name in VOICES]
    if default and default not in RETIRED_VOICES and default not in {row["id"] for row in options}:
        options.append({"id": default, "name": "自定义音色（后端配置）"})
    return options
