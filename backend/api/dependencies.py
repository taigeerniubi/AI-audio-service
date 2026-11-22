"""把应用级配置、存储和业务服务注入 HTTP 与 WebSocket 路由。"""

from dataclasses import dataclass

from starlette.requests import HTTPConnection

from backend.core.config import Settings
from backend.db.store import Store
from backend.llm.factory import LLMFactory
from backend.services.knowledge.service import KnowledgeService
from backend.services.products import ProductService
from backend.services.sessions import SessionService


@dataclass
class Runtime:
    """每个 FastAPI 应用独享一组依赖，便于隔离测试和会话。"""

    settings: Settings
    store: Store
    llm: LLMFactory
    products: ProductService
    knowledge: KnowledgeService
    sessions: SessionService


def get_runtime(connection: HTTPConnection) -> Runtime:
    """从当前 HTTP 或 WebSocket 应用中取得依赖容器。

    Args:
        connection: 当前 HTTP 请求或 WebSocket 连接。

    Returns:
        该连接所属应用的依赖容器。
    """
    return connection.app.state.runtime
