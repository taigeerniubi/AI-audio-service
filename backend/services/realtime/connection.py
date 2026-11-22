"""提取两段 WebSocket 连接的关闭码并限制诊断内容，避免泄漏凭证。"""

CLIENT_INACTIVITY_SECONDS = 15


def connection_diagnostic(side: str, error: Exception | None = None, code: int | None = None) -> dict:
    """提取可安全保存的连接异常字段，不读取任意异常或关闭原因文本。

    Args:
        side: browser 或 model，用于区分故障链路。
        error: 传输层异常，可含 websockets 的 received/sent Close 信息。
        code: ASGI 提供的显式关闭码。

    Returns:
        仅包含链路、异常类名以及整数关闭码的字典。
    """
    received = getattr(getattr(error, "rcvd", None), "code", None)
    sent = getattr(getattr(error, "sent", None), "code", None)
    close_code = code if isinstance(code, int) else received if isinstance(received, int) else 1006
    result = {"side": side, "close_code": close_code}
    if error is not None:
        result["category"] = type(error).__name__
    if isinstance(sent, int):
        result["sent_close_code"] = sent
    return result
