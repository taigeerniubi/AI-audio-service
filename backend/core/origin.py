"""校验浏览器请求是否来自本地页面或明确配置的 HTTPS 网站。"""

import re
from urllib.parse import urlparse


def normalize_public_origin(value: str) -> str:
    """校验并规范化单个公开网站来源，禁止通配符和路径。

    Args:
        value: 可选的 HTTPS 来源，如 https://audio.example.com。

    Returns:
        不含末尾斜线的来源；空配置返回空字符串。

    Raises:
        ValueError: 来源不是有效的、无路径的 HTTPS 地址。
    """
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and re.fullmatch(r"[a-zA-Z0-9.-]+", parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.params and not parsed.query and not parsed.fragment
            and not re.search(r"\s", value)
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("PUBLIC_ORIGIN 必须是无路径、无通配符的 HTTPS 来源")
    return f"https://{parsed.netloc.lower()}"


def local_origin(origin: str | None, public_origin: str = "") -> bool:
    """判断请求来源是否符合本地访问约束。

    Args:
        origin: 浏览器 Origin；命令行或同源请求可能不携带此头。
        public_origin: 已校验的公开来源；默认为仅允许本机。

    Returns:
        未提供 Origin、来源为本机页面或精确匹配公开来源时返回 True。
    """
    if not origin:
        return True
    if public_origin and origin == public_origin:
        return True
    try:
        parsed = urlparse(origin)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.params and not parsed.query and not parsed.fragment
        )
    except ValueError:
        return False
