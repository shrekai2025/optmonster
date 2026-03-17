from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.accounts.loader import AccountConfigLoader, AccountGroupConfigLoader
from app.accounts.registry import AccountRegistry
from app.accounts.service import AccountService
from app.actions.executor import TwikitActionExecutor, TwitterActionExecutor
from app.actions.service import ActionService
from app.actions.writing_guides import WritingGuideService
from app.fetching.factory import DataSourceFactory
from app.fetching.service import FetchService
from app.llm.service import LLMService
from app.runtime.database import create_engine, create_session_factory
from app.runtime.redis import RuntimeCoordinator
from app.runtime.settings import Settings


@dataclass(slots=True)
class ServiceContainer:
    settings: Settings
    engine: AsyncEngine | None
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    registry: AccountRegistry
    account_service: AccountService
    action_service: ActionService
    fetch_service: FetchService
    runtime_coordinator: RuntimeCoordinator
    llm_service: LLMService
    action_executor: TwitterActionExecutor
    writing_guide_service: WritingGuideService


async def build_container(
    settings: Settings,
    *,
    redis_client: Redis | None = None,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    datasource_factory: DataSourceFactory | None = None,
    llm_service: LLMService | None = None,
    action_executor: TwitterActionExecutor | None = None,
) -> ServiceContainer:
    runtime_engine = engine or create_engine(settings)
    runtime_session_factory = session_factory or create_session_factory(runtime_engine)
    runtime_redis = redis_client or Redis.from_url(settings.redis_url, decode_responses=True)

    loader = AccountConfigLoader(
        config_dir=settings.resolve_path(settings.config_dir),
        default_cookie_dir=settings.resolve_path(settings.cookie_dir),
    )
    group_loader = AccountGroupConfigLoader(
        config_dir=settings.resolve_path(settings.group_config_dir),
    )
    registry = AccountRegistry(loader, group_loader=group_loader)
    coordinator = RuntimeCoordinator(runtime_redis, settings)
    source_factory = datasource_factory or DataSourceFactory(settings)
    runtime_llm_service = llm_service or LLMService(
        settings,
        session_factory=runtime_session_factory,
    )
    runtime_action_executor = action_executor or TwikitActionExecutor(settings)
    writing_guide_service = WritingGuideService(
        session_factory=runtime_session_factory,
        llm_service=runtime_llm_service,
        settings=settings,
    )
    account_service = AccountService(
        runtime_session_factory,
        registry,
        coordinator,
        settings,
    )
    action_service = ActionService(
        session_factory=runtime_session_factory,
        registry=registry,
        coordinator=coordinator,
        settings=settings,
        llm_service=runtime_llm_service,
        action_executor=runtime_action_executor,
        writing_guide_service=writing_guide_service,
    )
    fetch_service = FetchService(
        session_factory=runtime_session_factory,
        registry=registry,
        datasource_factory=source_factory,
        coordinator=coordinator,
        settings=settings,
        action_service=action_service,
    )
    container = ServiceContainer(
        settings=settings,
        engine=runtime_engine,
        session_factory=runtime_session_factory,
        redis=runtime_redis,
        registry=registry,
        account_service=account_service,
        action_service=action_service,
        fetch_service=fetch_service,
        runtime_coordinator=coordinator,
        llm_service=runtime_llm_service,
        action_executor=runtime_action_executor,
        writing_guide_service=writing_guide_service,
    )
    await account_service.reload_configs()
    return container


async def shutdown_container(container: ServiceContainer) -> None:
    if hasattr(container.redis, "aclose"):
        await container.redis.aclose()
    else:  # pragma: no cover - compatibility fallback
        await container.redis.close()
    if container.engine is not None:
        await container.engine.dispose()
