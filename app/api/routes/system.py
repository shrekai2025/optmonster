from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.deps import get_container
from app.runtime.container import ServiceContainer
from app.runtime.database import ping_database

router = APIRouter(tags=["system"])
ContainerDep = Annotated[ServiceContainer, Depends(get_container)]


@router.get("/healthz")
async def healthcheck(container: ContainerDep) -> dict[str, str]:
    try:
        await ping_database(container.session_factory)
        await container.runtime_coordinator.ping()
    except SQLAlchemyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except Exception as exc:  # pragma: no cover - safety net
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return {"status": "ok"}
