"""定义会话管理、报告查询及浏览器音频 WebSocket 接口。"""

from fastapi import APIRouter, Depends, WebSocket

from backend.api.dependencies import Runtime, get_runtime
from backend.core.origin import local_origin
from backend.schemas.sessions import SessionInput

router = APIRouter(tags=["sessions"])


@router.post("/api/sessions", status_code=201)
def create_session(body: SessionInput, runtime: Runtime = Depends(get_runtime)):
    """固定产品版本并返回一次性连接地址。

    Args:
        body: 包含所选产品 ID 的会话创建请求。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        新会话 ID 及其 WebSocket 路径。
    """
    return runtime.sessions.create(body.product_id, body.voice, body.turn_detection)


@router.get("/api/sessions")
def sessions(runtime: Runtime = Depends(get_runtime)):
    """列出最近创建的会话。

    Args:
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        最近创建的会话概要列表。
    """
    return runtime.store.sessions()


@router.get("/api/sessions/{session_id}/report")
async def report(session_id: str, runtime: Runtime = Depends(get_runtime)):
    """读取会话的当前或最终报告。

    Args:
        session_id: 目标语音会话的唯一 ID。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        运行时报告或已落库的最终报告。
    """
    return runtime.sessions.report(session_id)


@router.websocket("/ws/sessions/{session_id}")
async def session_socket(socket: WebSocket, session_id: str, runtime: Runtime = Depends(get_runtime)):
    """校验来源和会话归属，再将连接交给实时会话协调器。

    Args:
        socket: 尚待校验和接受的浏览器 WebSocket。
        session_id: 目标语音会话的唯一 ID。
        runtime: 当前应用独享的配置、存储与业务服务容器。
    """
    if not local_origin(socket.headers.get("origin"), runtime.settings.public_origin):
        await socket.close(code=1008)
        return
    live = runtime.sessions.attach(session_id, socket)
    if live is None:
        await socket.close(code=1008)
        return
    try:
        await socket.accept()
        await live.run()
    finally:
        if not live.closed:
            live.reason, live.closed = "connection_error", True
            runtime.store.finish(session_id, live.report())
        runtime.sessions.release(session_id)
