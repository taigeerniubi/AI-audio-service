"""组装应用依赖、路由和本地页面，提供 FastAPI 启动入口。"""

import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend.api.dependencies import Runtime
from backend.api.routes import documents, health, products, sessions
from backend.core.config import Settings
from backend.core.errors import ServiceError
from backend.core.origin import local_origin
from backend.core.paths import FRONTEND_DIST
from backend.db.store import Store
from backend.llm.factory import LLMFactory
from backend.services.knowledge.retrieval import tokenize
from backend.services.knowledge.service import KnowledgeService
from backend.services.products import ProductService
from backend.services.sessions import SessionService


def create_app(settings=None, transport_factory=None, parser=None, embedder=None) -> FastAPI:
    """创建依赖隔离的应用，生产与离线测试共用此入口。

    Args:
        settings: 显式配置；省略时读取后端环境变量。
        transport_factory: 可选的实时模型测试构造器。
        parser: 可选的文档测试解析器。
        embedder: 可选的向量模型测试替身。

    Returns:
        已注册路由、中间件和资源清理逻辑的 FastAPI 应用。
    """
    settings = settings or Settings.from_env()
    store = Store(settings.database)
    llm = LLMFactory(settings, realtime_builder=transport_factory, embedding_model=embedder)
    runtime = Runtime(settings, store, llm, ProductService(store), KnowledgeService(settings, store, llm, parser), SessionService(settings, store, llm))

    @asynccontextmanager
    async def lifespan(app):
        """启动时预热本地检索资源，退出时关闭会话、HTTP 池及数据库。

        Args:
            app: FastAPI 传入的当前应用实例。
        """
        try:
            await asyncio.to_thread(llm.start)
            await asyncio.to_thread(tokenize, "本地预热")
            yield
        finally:
            runtime.sessions.shutdown()
            await asyncio.to_thread(llm.close)
            store.close()

    app = FastAPI(title="AI audio service", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.store = store
    app.state.active = runtime.sessions.active
    allowed_hosts = ["localhost", "127.0.0.1", "[::1]", "testserver"]
    allowed_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]
    if settings.public_origin:
        allowed_hosts.append(urlparse(settings.public_origin).hostname)
        allowed_origins.append(settings.public_origin)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type"])

    @app.middleware("http")
    async def guard_origin(request: Request, call_next):
        """校验请求 Origin 后交给下一层，阻止未授权网页来源调用。

        Args:
            request: 当前 HTTP 请求。
            call_next: 后续中间件或路由处理函数。

        Returns:
            来源错误响应或后续处理结果。
        """
        if not local_origin(request.headers.get("origin"), settings.public_origin):
            return JSONResponse({"detail": "不允许此页面来源访问"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        """把业务异常转换为稳定的 JSON 错误响应。

        Args:
            request: 触发异常的 HTTP 请求。
            exc: 包含状态码与公开文案的业务异常。

        Returns:
            含 detail 字段的 JSONResponse。
        """
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    for router in (health.router, products.router, documents.router, sessions.router):
        app.include_router(router)
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
    return app


app = create_app()
