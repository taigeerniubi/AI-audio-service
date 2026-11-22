"""兼容旧的 uvicorn 启动命令，实际应用组装位于 main 模块。"""

from backend.main import app, create_app

__all__ = ["app", "create_app"]
