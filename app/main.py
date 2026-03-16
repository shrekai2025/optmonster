from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.routes.admin import router as admin_router
from app.api.routes.system import router as system_router
from app.api.routes.ui import router as ui_router
from app.runtime.container import ServiceContainer, build_container, shutdown_container
from app.runtime.settings import Settings, get_settings

ContainerFactory = Callable[[Settings], Awaitable[ServiceContainer]]


def create_app(
    settings: Settings | None = None,
    *,
    container_factory: ContainerFactory = build_container,
) -> FastAPI:
    runtime_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        container = await container_factory(runtime_settings)
        app.state.container = container
        yield
        await shutdown_container(container)

    app = FastAPI(
        title="OptMonster",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(ui_router)
    app.include_router(system_router)
    app.include_router(admin_router, prefix="/admin")
    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    run()
