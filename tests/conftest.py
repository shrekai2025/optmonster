from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fakeredis.aioredis
import pytest_asyncio
import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.accounts.schemas import AccountConfig
from app.actions.executor import TwitterActionExecutor
from app.fetching.datasource import TwitterDataSource
from app.fetching.factory import DataSourceFactory
from app.fetching.schemas import AccountProfileSnapshot, FetchBatchResult, SessionValidationResult
from app.llm.schemas import (
    AILogDetailView,
    AILogListItem,
    AILogSummaryView,
    DecisionResult,
    GuideRecommendation,
    PromptTemplateConfig,
    PromptTemplateUpdateResult,
    PromptTemplateView,
)
from app.runtime.container import ServiceContainer, build_container, shutdown_container
from app.runtime.database import create_engine, create_schema, create_session_factory
from app.runtime.enums import ActionStatus, SourceType
from app.runtime.models import (
    AccountRuntimeState,
    ActionRequest,
    AILogRecord,
    FetchCursor,
    FetchedTweet,
)
from app.runtime.settings import Settings


class FakeTwitterDataSource(TwitterDataSource):
    def __init__(self) -> None:
        self.validation_results: dict[str, SessionValidationResult] = {}
        self.profile_results: dict[str, AccountProfileSnapshot] = {}
        self.batch_queues: dict[
            tuple[str, SourceType, str],
            list[FetchBatchResult],
        ] = defaultdict(list)
        self.failures: dict[tuple[str, SourceType, str], Exception] = {}
        self.fetch_requests: list[tuple[str, SourceType, str, str | None, int]] = []

    def set_validation(self, account_id: str, result: SessionValidationResult) -> None:
        self.validation_results[account_id] = result

    def add_batch(
        self,
        account_id: str,
        source_type: SourceType,
        source_key: str,
        batch: FetchBatchResult,
    ) -> None:
        self.batch_queues[(account_id, source_type, source_key)].append(batch)

    def set_profile(
        self,
        account_id: str,
        *,
        follower_count: int | None,
        twitter_handle: str | None = None,
    ) -> None:
        self.profile_results[account_id] = AccountProfileSnapshot(
            follower_count=follower_count,
            twitter_handle=twitter_handle,
        )

    def set_failure(
        self,
        account_id: str,
        source_type: SourceType,
        source_key: str,
        exc: Exception,
    ) -> None:
        self.failures[(account_id, source_type, source_key)] = exc

    async def validate_session(self, account) -> SessionValidationResult:
        return self.validation_results.get(
            account.id,
            SessionValidationResult(ok=True, detail="session valid"),
        )

    async def get_account_profile(self, account) -> AccountProfileSnapshot:
        return self.profile_results.get(
            account.id,
            AccountProfileSnapshot(
                follower_count=None,
                twitter_handle=account.twitter_handle,
            ),
        )

    async def fetch_home_timeline(
        self,
        account,
        *,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        self.fetch_requests.append(
            (account.id, SourceType.TIMELINE, "home_following", cursor, limit)
        )
        return self._resolve(account.id, SourceType.TIMELINE, "home_following")

    async def fetch_user_tweets(
        self,
        account,
        *,
        user_handle: str,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        self.fetch_requests.append((account.id, SourceType.WATCH_USER, user_handle, cursor, limit))
        return self._resolve(account.id, SourceType.WATCH_USER, user_handle)

    async def search_recent(
        self,
        account,
        *,
        query: str,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        self.fetch_requests.append((account.id, SourceType.KEYWORD_SEARCH, query, cursor, limit))
        return self._resolve(account.id, SourceType.KEYWORD_SEARCH, query)

    def _resolve(
        self,
        account_id: str,
        source_type: SourceType,
        source_key: str,
    ) -> FetchBatchResult:
        key = (account_id, source_type, source_key)
        if key in self.failures:
            raise self.failures[key]
        queue = self.batch_queues[key]
        if not queue:
            return FetchBatchResult(items=[], next_cursor=None)
        return queue.pop(0)


class FakeLLMService:
    def __init__(self) -> None:
        self.decisions: dict[tuple[str, str], DecisionResult] = {}
        self.generated_guides: list[tuple[str, str, str]] = []
        self.logs: list[dict[str, Any]] = []
        self.decision_calls = 0
        self.session_factory: async_sessionmaker[AsyncSession] | None = None
        self.prompt_templates = PromptTemplateConfig(
            decision_system_template="default decision system",
            decision_user_template="default decision user",
            learning_system_template="default learning system",
            learning_user_template="default learning user",
        )
        self.prompt_config_file = "memory://prompts.yaml"

    def set_decision(self, account_id: str, tweet_text: str, result: DecisionResult) -> None:
        self.decisions[(account_id, tweet_text)] = result

    async def generate_decision(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        author_handle: str | None,
        writing_guide: str | None,
        fetched_tweet_id: int | None = None,
    ) -> DecisionResult:
        self.decision_calls += 1
        result = self.decisions.get(
            (account.id, tweet_text),
            DecisionResult(
                relevance_score=9,
                like=True,
                reply_draft="Thoughtful follow-up from fake LLM.",
                reply_confidence=8,
                rationale="default fake decision",
            ),
        )
        await self._append_log(
            account_id=account.id,
            fetched_tweet_id=fetched_tweet_id,
            log_type="decision",
            request_payload={"tweet_text": tweet_text, "author_handle": author_handle},
            response_payload={"result": result.model_dump(mode="json")},
        )
        return result

    async def summarize_learning(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        ai_draft: str,
        final_draft: str,
    ) -> GuideRecommendation:
        self.generated_guides.append((account.id, ai_draft, final_draft))
        result = GuideRecommendation(
            voice=f"{account.persona.name}: {account.persona.tone}",
            dos=[f"Prefer: {final_draft}"],
            donts=[f"Avoid: {ai_draft}"],
        )
        await self._append_log(
            account_id=account.id,
            fetched_tweet_id=None,
            log_type="learning",
            request_payload={
                "tweet_text": tweet_text,
                "ai_draft": ai_draft,
                "final_draft": final_draft,
            },
            response_payload={"result": result.model_dump(mode="json")},
        )
        return result

    async def test_prompt(self, prompt: str) -> dict[str, Any]:
        result = {
            "provider": "mock",
            "model_id": None,
            "content": f"fake:{prompt}",
        }
        await self._append_log(
            account_id=None,
            fetched_tweet_id=None,
            log_type="prompt_test",
            request_payload={"prompt": prompt},
            response_payload={"result": result},
        )
        return result

    def get_prompt_templates(self) -> PromptTemplateView:
        return PromptTemplateView(
            config_file=self.prompt_config_file,
            **self.prompt_templates.model_dump(),
        )

    def update_prompt_templates(
        self,
        payload: PromptTemplateConfig,
    ) -> PromptTemplateUpdateResult:
        self.prompt_templates = payload
        if not self.prompt_config_file.startswith("memory://"):
            path = Path(self.prompt_config_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(payload.model_dump(mode="json"), sort_keys=False),
                encoding="utf-8",
            )
        return PromptTemplateUpdateResult(
            prompts=PromptTemplateView(
                config_file=self.prompt_config_file,
                **payload.model_dump(),
            )
        )

    async def list_logs(
        self,
        *,
        account_id: str | None = None,
        log_type: str | None = None,
        limit: int = 100,
    ) -> list[AILogListItem]:
        items = self.logs
        if account_id:
            items = [item for item in items if item["account_id"] == account_id]
        if log_type:
            items = [item for item in items if item["log_type"] == log_type]
        return [
            AILogListItem(
                id=item["id"],
                created_at=item["created_at"],
                account_id=item["account_id"],
                log_type=item["log_type"],
                status=item["status"],
                provider=item["provider"],
                model_id=item["model_id"],
                request_preview=str(item["request_payload"])[:180],
                response_preview=str(item["response_payload"])[:180]
                if item["response_payload"]
                else None,
                error_message=item["error_message"],
            )
            for item in reversed(items[-limit:])
        ]

    async def get_log(self, log_id: int) -> AILogDetailView:
        for item in self.logs:
            if item["id"] == log_id:
                return AILogDetailView(
                    id=item["id"],
                    created_at=item["created_at"],
                    account_id=item["account_id"],
                    log_type=item["log_type"],
                    status=item["status"],
                    provider=item["provider"],
                    model_id=item["model_id"],
                    request_preview=str(item["request_payload"])[:180],
                    response_preview=str(item["response_payload"])[:180]
                    if item["response_payload"]
                    else None,
                    error_message=item["error_message"],
                    request_payload=item["request_payload"],
                    response_payload=item["response_payload"],
                )
        raise KeyError(log_id)

    async def get_log_summary(
        self,
        *,
        account_id: str | None = None,
        window_hours: int = 24,
    ) -> AILogSummaryView:
        if self.session_factory is not None:
            async with self.session_factory() as session:
                query = select(AILogRecord).order_by(
                    AILogRecord.created_at.desc(),
                    AILogRecord.id.desc(),
                )
                if account_id:
                    query = query.where(AILogRecord.account_id == account_id)
                rows = (await session.execute(query)).scalars().all()
            items = [
                {
                    "created_at": row.created_at,
                    "account_id": row.account_id,
                    "log_type": row.log_type,
                    "status": row.status,
                    "response_payload": row.response_payload,
                }
                for row in rows
            ]
        else:
            items = self.logs
            if account_id:
                items = [item for item in items if item["account_id"] == account_id]

        decision_success_count = 0
        decision_failed_count = 0
        learning_count = 0
        prompt_test_count = 0
        auto_scored_tweets = 0
        auto_score_failed_tweets = 0
        auto_score_skipped_ai_disabled = 0
        filtered_replies_count = 0
        filtered_retweets_count = 0
        latest_auto_score_batch_at = None
        latest_auto_score_batch_scored = None
        latest_auto_score_batch_failed = None

        ordered_items = sorted(items, key=lambda item: item["created_at"], reverse=True)
        for item in ordered_items:
            if item["log_type"] == "decision":
                if item["status"] == "success":
                    decision_success_count += 1
                else:
                    decision_failed_count += 1
            elif item["log_type"] == "learning":
                learning_count += 1
            elif item["log_type"] == "prompt_test":
                prompt_test_count += 1
            elif item["log_type"] == "auto_score_batch":
                payload = item["response_payload"] or {}
                auto_scored_tweets += int(payload.get("scored_count", 0) or 0)
                auto_score_failed_tweets += int(payload.get("failed_count", 0) or 0)
                if latest_auto_score_batch_at is None:
                    latest_auto_score_batch_at = item["created_at"]
                    latest_auto_score_batch_scored = int(payload.get("scored_count", 0) or 0)
                    latest_auto_score_batch_failed = int(payload.get("failed_count", 0) or 0)
            elif item["log_type"] == "auto_score_skip":
                payload = item["response_payload"] or {}
                if payload.get("reason") == "ai_disabled":
                    auto_score_skipped_ai_disabled += int(payload.get("skipped_count", 0) or 0)
            elif item["log_type"] == "fetch_filter":
                payload = item["response_payload"] or {}
                filtered_replies_count += int(payload.get("reply_filtered", 0) or 0)
                filtered_retweets_count += int(payload.get("retweet_filtered", 0) or 0)

        return AILogSummaryView(
            window_hours=window_hours,
            total_logs=len(items),
            decision_success_count=decision_success_count,
            decision_failed_count=decision_failed_count,
            learning_count=learning_count,
            prompt_test_count=prompt_test_count,
            auto_scored_tweets=auto_scored_tweets,
            auto_score_failed_tweets=auto_score_failed_tweets,
            auto_score_skipped_ai_disabled=auto_score_skipped_ai_disabled,
            filtered_replies_count=filtered_replies_count,
            filtered_retweets_count=filtered_retweets_count,
            latest_auto_score_batch_at=latest_auto_score_batch_at,
            latest_auto_score_batch_scored=latest_auto_score_batch_scored,
            latest_auto_score_batch_failed=latest_auto_score_batch_failed,
        )

    async def _append_log(
        self,
        *,
        account_id: str | None,
        fetched_tweet_id: int | None,
        log_type: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any] | None,
        status: str = "success",
        error_message: str | None = None,
    ) -> None:
        created_at = datetime.now(UTC)
        self.logs.append(
            {
                "id": len(self.logs) + 1,
                "created_at": created_at,
                "account_id": account_id,
                "fetched_tweet_id": fetched_tweet_id,
                "log_type": log_type,
                "status": status,
                "provider": "mock",
                "model_id": None,
                "request_payload": request_payload,
                "response_payload": response_payload,
                "error_message": error_message,
            }
        )
        if self.session_factory is None:
            return
        async with self.session_factory() as session:
            session.add(
                AILogRecord(
                    account_id=account_id,
                    fetched_tweet_id=fetched_tweet_id,
                    log_type=log_type,
                    status=status,
                    provider="mock",
                    model_id=None,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    error_message=error_message,
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            await session.commit()


class FakeTwitterActionExecutor(TwitterActionExecutor):
    def __init__(self) -> None:
        self.follows: list[tuple[str, str]] = []
        self.likes: list[tuple[str, str]] = []
        self.replies: list[tuple[str, str, str]] = []

    async def follow(self, account: AccountConfig, *, user_handle: str) -> dict[str, Any]:
        self.follows.append((account.id, user_handle))
        return {"user_handle": user_handle}

    async def like(self, account: AccountConfig, *, tweet_id: str) -> dict[str, Any]:
        self.likes.append((account.id, tweet_id))
        return {"tweet_id": tweet_id}

    async def reply(
        self,
        account: AccountConfig,
        *,
        tweet_id: str,
        text: str,
    ) -> dict[str, Any]:
        self.replies.append((account.id, tweet_id, text))
        return {"tweet_id": tweet_id, "text": text}


@dataclass(slots=True)
class TestContext:
    container: ServiceContainer
    settings: Settings
    config_dir: Path
    cookie_dir: Path
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    fake_source: FakeTwitterDataSource
    fake_llm: FakeLLMService
    fake_executor: FakeTwitterActionExecutor

    async def get_state(self, account_id: str) -> AccountRuntimeState:
        async with self.session_factory() as session:
            state = await session.get(AccountRuntimeState, account_id)
            assert state is not None
            return state

    async def get_cursor(
        self,
        account_id: str,
        source_type: SourceType,
        source_key: str,
    ) -> FetchCursor | None:
        async with self.session_factory() as session:
            query = select(FetchCursor).where(
                FetchCursor.account_id == account_id,
                FetchCursor.source_type == source_type,
                FetchCursor.source_key == source_key,
            )
            return (await session.execute(query)).scalar_one_or_none()

    async def tweet_count(self, account_id: str) -> int:
        async with self.session_factory() as session:
            query = select(func.count(FetchedTweet.id)).where(FetchedTweet.account_id == account_id)
            return int((await session.execute(query)).scalar_one())

    async def action_count(
        self,
        account_id: str,
        *,
        status: ActionStatus | None = None,
    ) -> int:
        async with self.session_factory() as session:
            query = select(func.count(ActionRequest.id)).where(
                ActionRequest.account_id == account_id
            )
            if status:
                query = query.where(ActionRequest.status == status)
            return int((await session.execute(query)).scalar_one())


@pytest_asyncio.fixture
async def make_test_context(tmp_path: Path) -> Callable[..., Any]:
    created: list[TestContext] = []

    async def _make_test_context(
        *,
        accounts: list[dict[str, Any]],
        settings_overrides: dict[str, Any] | None = None,
        llm_service: FakeLLMService | None = None,
        action_executor: FakeTwitterActionExecutor | None = None,
    ) -> TestContext:
        case_root = tmp_path / f"case_{len(created)}"
        config_dir = case_root / "accounts"
        cookie_dir = case_root / "cookies"
        config_dir.mkdir(parents=True, exist_ok=True)
        cookie_dir.mkdir(parents=True, exist_ok=True)

        for account in accounts:
            account_id = account["id"]
            cookie_path = cookie_dir / f"{account_id}.json"
            cookie_path.write_text("{}", encoding="utf-8")
            payload = {
                "id": account_id,
                "twitter_handle": account.get("twitter_handle", f"@{account_id}"),
                "enabled": account.get("enabled", True),
                "execution_mode": account.get("execution_mode", "read_only"),
                "cookie_file": str(cookie_path),
                "targets": {
                    "timeline": account.get("timeline", True),
                    "follow_users_enabled": account.get("follow_users_enabled", True),
                    "follow_users": account.get("follow_users", []),
                    "search_keywords_enabled": account.get("search_keywords_enabled", True),
                    "search_keywords": account.get("search_keywords", []),
                },
                "fetch_schedule": account.get(
                    "fetch_schedule",
                    {
                        "base_interval_minutes": 5,
                        "interval_jitter_minutes": 0,
                        "quiet_hours": None,
                    },
                ),
                "behavior_budget": account.get(
                    "behavior_budget",
                    {
                        "daily_likes_max": 30,
                        "daily_replies_max": 8,
                        "daily_follows_max": 5,
                        "active_hours": [0, 23],
                        "min_interval_minutes": 15,
                    },
                ),
            }
            if "proxy" in account:
                payload["proxy"] = account["proxy"]
            (config_dir / f"{account_id}.yaml").write_text(
                yaml.safe_dump(payload, sort_keys=False),
                encoding="utf-8",
            )

        db_path = case_root / "test.db"
        overrides = settings_overrides or {}
        settings = Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{db_path}",
            redis_url="redis://unused/0",
            config_dir=config_dir,
            cookie_dir=cookie_dir,
            scheduled_fetch_interval_seconds=0,
            worker_poll_interval_seconds=1,
            **overrides,
        )
        engine = create_engine(settings)
        await create_schema(engine)
        session_factory = create_session_factory(engine)
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        fake_source = FakeTwitterDataSource()
        fake_llm = llm_service or FakeLLMService()
        fake_llm.prompt_config_file = str(settings.resolve_path(settings.ai_prompt_config_file))
        fake_llm.session_factory = session_factory
        fake_executor = action_executor or FakeTwitterActionExecutor()
        datasource_factory = DataSourceFactory(settings, primary_source=fake_source)
        container = await build_container(
            settings,
            redis_client=redis,
            engine=engine,
            session_factory=session_factory,
            datasource_factory=datasource_factory,
            llm_service=fake_llm,
            action_executor=fake_executor,
        )
        context = TestContext(
            container=container,
            settings=settings,
            config_dir=config_dir,
            cookie_dir=cookie_dir,
            engine=engine,
            session_factory=session_factory,
            fake_source=fake_source,
            fake_llm=fake_llm,
            fake_executor=fake_executor,
        )
        created.append(context)
        return context

    yield _make_test_context

    for context in created:
        await shutdown_container(context.container)
