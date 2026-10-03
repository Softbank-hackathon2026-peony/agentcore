from contextlib import asynccontextmanager

from fastapi import FastAPI

from auth import health, passwords, routes
from auth.config import Settings
from auth.errors import install_error_handlers
from auth.infra import make_engine, make_redis
from auth.observability import RequestContextMiddleware, configure_logging, install_metrics_route


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    passwords.configure(settings.hash_concurrency, settings.hash_queue_timeout_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(settings.database_url)
        app.state.redis = make_redis(settings.redis_url)
        try:
            yield
        finally:
            await app.state.redis.aclose()
            await app.state.engine.dispose()

    app = FastAPI(title="auth-svc", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(RequestContextMiddleware)
    install_error_handlers(app)
    install_metrics_route(app)
    app.include_router(health.router)
    app.include_router(routes.router)
    return app
