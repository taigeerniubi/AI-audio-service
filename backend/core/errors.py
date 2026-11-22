"""定义业务失败的状态码与可安全展示给用户的错误信息。"""


class ServiceError(ValueError):
    """携带 API 状态码且不包含凭证的业务错误。

    Attributes:
        status_code: API 层应返回的 HTTP 状态码。
        message: 已脱敏、可直接展示给用户的说明。
    """

    def __init__(self, status_code: int, message: str):
        """记录业务失败的返回状态和展示文本。

        Args:
            status_code: HTTP 状态码。
            message: 面向用户的错误说明。
        """
        super().__init__(message)
        self.status_code = status_code
        self.message = message
