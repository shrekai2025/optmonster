from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.registry import AccountRegistry
from app.accounts.schemas import (
    AccountConfig,
    FetchEnqueueResponse,
    FetchSourceConfig,
    TweetBackfillResult,
)
from app.actions.schemas import GenerateReplyRequest
from app.fetching.errors import FetchError, classify_exception
from app.fetching.factory import DataSourceFactory
from app.fetching.schemas import FetchBatchResult, FetchRunResponse, SessionValidationResult
from app.runtime.enums import (
    AccountLifecycleStatus,
    CookieFreshness,
    FetchErrorCode,
    OperationStatus,
    OperationType,
    PauseReason,
    ProxyHealth,
    SourceType,
)
from app.runtime.models import (
    AccountFollowerSnapshot,
    AccountRuntimeState,
    ActionRequest,
    AILogRecord,
    FetchCursor,
    FetchedTweet,
    OperationLog,
)
from app.runtime.redis import RuntimeCoordinator
from app.runtime.settings import Settings

if TYPE_CHECKING:
    from app.actions.service import ActionService


class FetchService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: AccountRegistry,
        datasource_factory: DataSourceFactory,
        coordinator: RuntimeCoordinator,
        settings: Settings,
        action_service: ActionService | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.datasource_factory = datasource_factory
        self.coordinator = coordinator
        self.settings = settings
        self.action_service = action_service

    async def list_due_accounts(self) -> list[str]:
        now = datetime.now(UTC)
        due_accounts: list[str] = []
        accounts = await self.registry.enabled_accounts()

        async with self.session_factory() as session:
            states = {
                state.account_id: state
                for state in (
                    await session.execute(select(AccountRuntimeState))
                ).scalars()
            }

        for account in accounts:
            state = states.get(account.id)
            if state is None:
                continue
            if state.lifecycle_status != AccountLifecycleStatus.ENABLED:
                continue
            if await self.coordinator.backoff_ttl(account.id):
                continue
            if self._is_within_quiet_hours(account, now):
                continue
            if state.next_fetch_not_before_at and now < state.next_fetch_not_before_at:
                continue
            if state.last_fetch_finished_at is None:
                due_accounts.append(account.id)
                continue
            if state.next_fetch_not_before_at is None:
                elapsed = (now - state.last_fetch_finished_at).total_seconds()
                if elapsed < account.fetch_schedule.base_interval_minutes * 60:
                    continue
                due_accounts.append(account.id)
                continue
            due_accounts.append(account.id)
        return due_accounts

    async def enqueue_fetch(self, account_id: str) -> FetchEnqueueResponse:
        account = await self.registry.get(account_id)
        if account is None:
            raise KeyError(account_id)
        async with self.session_factory() as session:
            state = await self._get_state(session, account_id)
            if state.lifecycle_status != AccountLifecycleStatus.ENABLED:
                return FetchEnqueueResponse(
                    account_id=account_id,
                    enqueued=False,
                    detail=f"account_paused:{state.pause_reason}",
                )
        return FetchEnqueueResponse(
            account_id=account_id,
            enqueued=await self.coordinator.enqueue_fetch(account_id),
        )

    async def validate_session(self, account_id: str) -> SessionValidationResult:
        account = await self._get_account(account_id)
        async with self.session_factory() as session:
            state = await self._get_state(session, account.id)
            if state.pause_reason == PauseReason.ADMIN_DISABLED:
                return SessionValidationResult(
                    ok=False,
                    detail="account is admin disabled",
                )

        result = await self.datasource_factory.get_primary_source().validate_session(account)
        async with self.session_factory() as session:
            state = await self._get_state(session, account.id)
            if result.ok:
                state.last_auth_check = datetime.now(UTC)
                state.cookie_freshness = CookieFreshness.VALID
                state.proxy_health = ProxyHealth.HEALTHY if account.proxy else ProxyHealth.DIRECT
                if state.pause_reason == PauseReason.AUTH_EXPIRED:
                    state.pause_reason = PauseReason.NONE
                    state.lifecycle_status = AccountLifecycleStatus.ENABLED
                state.last_error_code = None
                state.last_error_message = None
                await self._log_operation(
                    session,
                    account_id=account.id,
                    operation_type=OperationType.VALIDATE_SESSION,
                    status=OperationStatus.SUCCESS,
                    message=result.detail,
                )
            else:
                state.last_auth_check = datetime.now(UTC)
                state.last_error_code = result.error_code or FetchErrorCode.UNKNOWN
                state.last_error_message = result.detail
                if result.error_code == FetchErrorCode.PROXY_FAILED:
                    state.proxy_health = ProxyHealth.UNHEALTHY
                    state.lifecycle_status = AccountLifecycleStatus.PAUSED
                    state.pause_reason = PauseReason.PROXY_FAILED
                else:
                    state.cookie_freshness = CookieFreshness.EXPIRED
                    state.lifecycle_status = AccountLifecycleStatus.PAUSED
                    state.pause_reason = PauseReason.AUTH_EXPIRED
                await self._log_operation(
                    session,
                    account_id=account.id,
                    operation_type=OperationType.VALIDATE_SESSION,
                    status=OperationStatus.FAILED,
                    error_code=result.error_code or FetchErrorCode.UNKNOWN,
                    message=result.detail,
                )
            await session.commit()
        if result.ok:
            await self.coordinator.clear_backoff(account.id)
            await self._record_follower_snapshot_if_needed(account)
        return result

    async def fetch_account(self, account_id: str, *, trigger: str) -> FetchRunResponse:
        account = await self.registry.get(account_id)
        if account is None:
            return FetchRunResponse(
                account_id=account_id,
                trigger=trigger,
                status="skipped",
                skipped_reason="account_missing",
            )
        if not account.enabled:
            return FetchRunResponse(
                account_id=account.id,
                trigger=trigger,
                status="skipped",
                skipped_reason="account_disabled",
            )

        token = await self.coordinator.acquire_account_lock(account_id)
        if token is None:
            return FetchRunResponse(
                account_id=account.id,
                trigger=trigger,
                status="skipped",
                skipped_reason="account_locked",
            )

        try:
            if trigger != "manual":
                ttl = await self.coordinator.backoff_ttl(account_id)
                if ttl > 0:
                    return FetchRunResponse(
                        account_id=account.id,
                        trigger=trigger,
                        status="skipped",
                        skipped_reason=f"backoff:{ttl}",
                    )
                now = datetime.now(UTC)
                if self._is_within_quiet_hours(account, now):
                    return FetchRunResponse(
                        account_id=account.id,
                        trigger=trigger,
                        status="skipped",
                        skipped_reason="quiet_hours",
                    )

            async with self.session_factory() as session:
                state = await self._get_state(session, account.id)
                if state.lifecycle_status != AccountLifecycleStatus.ENABLED:
                    return FetchRunResponse(
                        account_id=account.id,
                        trigger=trigger,
                        status="skipped",
                        skipped_reason=f"paused:{state.pause_reason}",
                    )
                if (
                    trigger != "manual"
                    and state.next_fetch_not_before_at
                    and datetime.now(UTC) < state.next_fetch_not_before_at
                ):
                    return FetchRunResponse(
                        account_id=account.id,
                        trigger=trigger,
                        status="skipped",
                        skipped_reason="scheduled_too_early",
                    )
                state.last_fetch_started_at = datetime.now(UTC)
                await session.commit()

            sources_processed = 0
            tweets_inserted = 0
            for source in account.build_fetch_sources(self.settings.fetch_limit_default):
                batch = await self._fetch_source(account, source)
                sources_processed += 1
                tweets_inserted += batch["inserted_count"]
                await self._auto_score_inserted_tweets(
                    account_id=account.id,
                    tweet_ids=batch["inserted_ids"],
                )

            await self.backfill_recent_unscored_tweets(
                account_id=account.id,
                limit_per_account=self.settings.fetch_limit_default,
            )

            await self._mark_fetch_success(account)
            return FetchRunResponse(
                account_id=account.id,
                trigger=trigger,
                status="success",
                sources_processed=sources_processed,
                tweets_inserted=tweets_inserted,
            )
        except Exception as exc:
            fetch_error = classify_exception(exc)
            await self._mark_fetch_failure(account, fetch_error)
            return FetchRunResponse(
                account_id=account.id,
                trigger=trigger,
                status="failed",
                error_code=fetch_error.code,
                error_message=fetch_error.detail,
            )
        finally:
            await self.coordinator.release_account_lock(account_id, token)

    async def _fetch_source(
        self,
        account: AccountConfig,
        source: FetchSourceConfig,
    ) -> dict[str, Any]:
        if self.settings.fetch_latest_first:
            return await self._fetch_source_latest_first(account, source)

        cursor_value: str | None = None
        async with self.session_factory() as session:
            cursor = await self._get_cursor(session, account.id, source)
            cursor_value = cursor.cursor if cursor else None

        result = await self._request_source_batch(
            account,
            source,
            cursor=cursor_value,
        )
        result, filter_counts = self._apply_fetch_content_filters(result)
        await self._record_fetch_filter_log(
            account_id=account.id,
            source=source,
            filter_counts=filter_counts,
        )
        result = self._filter_batch_to_recent_window(
            result,
            cutoff=self._recent_tweet_cutoff(datetime.now(UTC)),
        )
        inserted_ids = await self._persist_batch(
            account,
            source,
            result,
            cursor_to_store=result.next_cursor,
        )
        return {
            "inserted_count": len(inserted_ids),
            "inserted_ids": inserted_ids,
            "next_cursor": result.next_cursor,
        }

    async def _fetch_source_latest_first(
        self,
        account: AccountConfig,
        source: FetchSourceConfig,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        cutoff = self._recent_tweet_cutoff(now)
        collected_items = []
        seen_ids: set[str] = set()
        cursor: str | None = None
        final_next_cursor: str | None = None
        total_filter_counts = {
            "reply_filtered": 0,
            "retweet_filtered": 0,
        }

        while len(collected_items) < source.limit:
            result = await self._request_source_batch(account, source, cursor=cursor)
            final_next_cursor = result.next_cursor
            if not result.items:
                break
            filtered_result, filter_counts = self._apply_fetch_content_filters(result)
            total_filter_counts["reply_filtered"] += filter_counts["reply_filtered"]
            total_filter_counts["retweet_filtered"] += filter_counts["retweet_filtered"]

            for item in filtered_result.items:
                if item.tweet_id in seen_ids:
                    continue
                seen_ids.add(item.tweet_id)
                if not self._tweet_within_recent_window(item, cutoff):
                    continue
                collected_items.append(item)
                if len(collected_items) >= source.limit:
                    break

            if len(collected_items) >= source.limit:
                break
            if final_next_cursor is None:
                break
            if self._page_reaches_recent_window_boundary(result.items, cutoff):
                break
            cursor = final_next_cursor

        await self._record_fetch_filter_log(
            account_id=account.id,
            source=source,
            filter_counts=total_filter_counts,
        )
        merged_result = FetchBatchResult(items=collected_items, next_cursor=final_next_cursor)
        inserted_ids = await self._persist_batch(
            account,
            source,
            merged_result,
            cursor_to_store=None,
        )
        return {
            "inserted_count": len(inserted_ids),
            "inserted_ids": inserted_ids,
            "next_cursor": final_next_cursor,
        }

    async def _request_source_batch(
        self,
        account: AccountConfig,
        source: FetchSourceConfig,
        *,
        cursor: str | None,
    ) -> FetchBatchResult:
        datasource = self.datasource_factory.get_primary_source()
        if source.source_type == SourceType.TIMELINE:
            return await datasource.fetch_home_timeline(
                account,
                cursor=cursor,
                limit=source.limit,
            )
        if source.source_type == SourceType.WATCH_USER:
            return await datasource.fetch_user_tweets(
                account,
                user_handle=source.source_key,
                cursor=cursor,
                limit=source.limit,
            )
        return await datasource.search_recent(
            account,
            query=source.source_key,
            cursor=cursor,
            limit=source.limit,
        )

    async def _persist_batch(
        self,
        account: AccountConfig,
        source: FetchSourceConfig,
        batch: FetchBatchResult,
        *,
        cursor_to_store: str | None,
    ) -> list[int]:
        async with self.session_factory() as session:
            cursor = await self._get_cursor(session, account.id, source, create=True)
            existing_ids = set()
            tweet_ids = [tweet.tweet_id for tweet in batch.items]
            if tweet_ids:
                query = select(FetchedTweet.tweet_id).where(
                    FetchedTweet.account_id == account.id,
                    FetchedTweet.source_type == source.source_type,
                    FetchedTweet.source_key == source.source_key,
                    FetchedTweet.tweet_id.in_(tweet_ids),
                )
                existing_ids = set((await session.execute(query)).scalars())

            inserted_ids: list[int] = []
            for item in batch.items:
                if item.tweet_id in existing_ids:
                    continue
                row = FetchedTweet(
                    account_id=account.id,
                    source_type=source.source_type,
                    source_key=source.source_key,
                    tweet_id=item.tweet_id,
                    author_handle=item.author_handle,
                    text=item.text,
                    lang=item.lang,
                    created_at_twitter=item.created_at,
                    fetched_at=item.fetched_at,
                    raw_payload=item.raw_payload,
                )
                session.add(row)
                await session.flush()
                inserted_ids.append(row.id)

            cursor.cursor = cursor_to_store
            cursor.last_success_at = datetime.now(UTC)
            cursor.last_error_code = None
            cursor.last_error_message = None
            await session.commit()
            return inserted_ids

    def _apply_fetch_content_filters(
        self,
        batch: FetchBatchResult,
    ) -> tuple[FetchBatchResult, dict[str, int]]:
        reply_filtered = 0
        retweet_filtered = 0
        filtered_items = []
        for item in batch.items:
            if item.is_reply and not self.settings.fetch_include_replies:
                reply_filtered += 1
                continue
            if item.is_retweet and not self.settings.fetch_include_retweets:
                retweet_filtered += 1
                continue
            filtered_items.append(item)
        filter_counts = {
            "reply_filtered": reply_filtered,
            "retweet_filtered": retweet_filtered,
        }
        if len(filtered_items) == len(batch.items):
            return batch, filter_counts
        return FetchBatchResult(items=filtered_items, next_cursor=batch.next_cursor), filter_counts

    async def _auto_score_inserted_tweets(
        self,
        *,
        account_id: str,
        tweet_ids: list[int],
        trigger_source: str = "auto_fetch_scoring",
    ) -> None:
        if not tweet_ids:
            return
        if self.action_service is None:
            return
        if not self.settings.ai_enabled:
            await self._record_ai_runtime_log(
                account_id=account_id,
                log_type="auto_score_skip",
                status="skipped",
                request_payload={
                    "trigger_source": trigger_source,
                    "tweet_ids": tweet_ids,
                },
                response_payload={
                    "reason": "ai_disabled",
                    "skipped_count": len(tweet_ids),
                },
            )
            return

        scored_count = 0
        failed_count = 0
        failed_tweet_ids: list[int] = []
        for tweet_id in tweet_ids:
            try:
                await self.action_service.generate_reply_for_tweet(
                    tweet_id,
                    GenerateReplyRequest(
                        account_id=account_id,
                        trigger_source=trigger_source,
                    ),
                )
                scored_count += 1
            except Exception:
                failed_count += 1
                failed_tweet_ids.append(tweet_id)

        status = "success"
        if failed_count and scored_count:
            status = "partial"
        elif failed_count:
            status = "failed"
        await self._record_ai_runtime_log(
            account_id=account_id,
            log_type="auto_score_batch",
            status=status,
            request_payload={
                "trigger_source": trigger_source,
                "tweet_ids": tweet_ids,
            },
            response_payload={
                "scored_count": scored_count,
                "failed_count": failed_count,
                "failed_tweet_ids": failed_tweet_ids,
            },
        )

    async def score_existing_unscored_tweets(
        self,
        *,
        limit_per_account: int = 20,
    ) -> int:
        result = await self.backfill_recent_unscored_tweets(
            account_id=None,
            limit_per_account=limit_per_account,
        )
        return result.scored_tweets

    async def backfill_recent_unscored_tweets(
        self,
        *,
        account_id: str | None,
        limit_per_account: int = 20,
    ) -> TweetBackfillResult:
        if account_id is not None and await self.registry.get(account_id) is None:
            raise KeyError(account_id)
        if not self.settings.ai_enabled or self.action_service is None:
            return TweetBackfillResult(
                account_id=account_id,
                candidate_tweets=0,
                scored_tweets=0,
                failed_tweets=0,
            )

        cutoff = self._recent_tweet_cutoff(datetime.now(UTC))
        scored = 0
        failed = 0
        candidates_total = 0
        accounts = await self.registry.enabled_accounts()
        if account_id is not None:
            accounts = [account for account in accounts if account.id == account_id]
        async with self.session_factory() as session:
            states = {
                state.account_id: state
                for state in (await session.execute(select(AccountRuntimeState))).scalars()
            }

        for account in accounts:
            state = states.get(account.id)
            if state is None or state.lifecycle_status != AccountLifecycleStatus.ENABLED:
                continue

            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(FetchedTweet)
                        .where(FetchedTweet.account_id == account.id)
                        .order_by(FetchedTweet.fetched_at.desc(), FetchedTweet.id.desc())
                        .limit(limit_per_account * 4)
                    )
                ).scalars().all()
                candidates = [
                    row
                    for row in rows
                    if self._tweet_row_within_recent_window(row, cutoff)
                ]
                if not candidates:
                    continue

                tweet_ids = [row.id for row in candidates]
                scored_ids = set(
                    (
                        await session.execute(
                            select(AILogRecord.fetched_tweet_id).where(
                                AILogRecord.fetched_tweet_id.in_(tweet_ids),
                                AILogRecord.log_type == "decision",
                                AILogRecord.status == "success",
                            )
                        )
                    ).scalars().all()
                )
                acted_ids = set(
                    (
                        await session.execute(
                            select(ActionRequest.fetched_tweet_id).where(
                                ActionRequest.fetched_tweet_id.in_(tweet_ids)
                            )
                        )
                    ).scalars().all()
                )
                candidate_ids = [
                    row.id
                    for row in candidates
                    if row.id not in scored_ids and row.id not in acted_ids
                ][:limit_per_account]
                candidates_total += len(candidate_ids)

            account_scored = 0
            account_failed = 0
            for tweet_id in candidate_ids:
                try:
                    await self.action_service.generate_reply_for_tweet(
                        tweet_id,
                        GenerateReplyRequest(
                            account_id=account.id,
                            trigger_source="auto_backfill_scoring",
                        ),
                    )
                    scored += 1
                    account_scored += 1
                except Exception:
                    account_failed += 1
                    failed += 1
                    continue
            if account_scored or account_failed:
                status = "success"
                if account_failed and account_scored:
                    status = "partial"
                elif account_failed:
                    status = "failed"
                await self._record_ai_runtime_log(
                    account_id=account.id,
                    log_type="auto_score_batch",
                    status=status,
                    request_payload={
                        "trigger_source": "auto_backfill_scoring",
                        "limit_per_account": limit_per_account,
                    },
                    response_payload={
                        "scored_count": account_scored,
                        "failed_count": account_failed,
                    },
                )
        return TweetBackfillResult(
            account_id=account_id,
            candidate_tweets=candidates_total,
            scored_tweets=scored,
            failed_tweets=failed,
        )

    def _tweet_row_within_recent_window(
        self,
        tweet: FetchedTweet,
        cutoff: datetime | None,
    ) -> bool:
        if cutoff is None:
            return True
        created_at = self._normalize_timestamp(tweet.created_at_twitter or tweet.fetched_at)
        return created_at >= cutoff

    async def _mark_fetch_success(self, account: AccountConfig) -> None:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            state = await self._get_state(session, account.id)
            state.last_fetch_finished_at = now
            state.next_fetch_not_before_at = self._next_fetch_not_before(account, now)
            state.failure_streak = 0
            state.last_error_code = None
            state.last_error_message = None
            state.cookie_freshness = CookieFreshness.VALID
            state.proxy_health = ProxyHealth.HEALTHY if account.proxy else ProxyHealth.DIRECT
            if state.pause_reason == PauseReason.REPEATED_FAILURES:
                state.pause_reason = PauseReason.NONE
                state.lifecycle_status = AccountLifecycleStatus.ENABLED
            await self._log_operation(
                session,
                account_id=account.id,
                operation_type=OperationType.FETCH,
                status=OperationStatus.SUCCESS,
                message="fetch completed",
            )
            await session.commit()
        await self.coordinator.clear_backoff(account.id)
        await self._record_follower_snapshot_if_needed(account)

    async def _mark_fetch_failure(self, account: AccountConfig, error: FetchError) -> None:
        now = datetime.now(UTC)
        retry_after_seconds = error.retry_after_seconds
        failure_streak = 0
        async with self.session_factory() as session:
            state = await self._get_state(session, account.id)
            state.last_fetch_finished_at = now
            state.next_fetch_not_before_at = self._next_fetch_not_before(account, now)
            state.failure_streak += 1
            failure_streak = state.failure_streak
            state.last_error_code = error.code
            state.last_error_message = error.detail
            if error.code == FetchErrorCode.AUTH_EXPIRED:
                state.cookie_freshness = CookieFreshness.EXPIRED
            if error.code == FetchErrorCode.PROXY_FAILED:
                state.proxy_health = ProxyHealth.UNHEALTHY
            if error.pause_reason is not None:
                state.lifecycle_status = AccountLifecycleStatus.PAUSED
                state.pause_reason = error.pause_reason
            elif state.failure_streak >= self.settings.pause_after_failures:
                state.lifecycle_status = AccountLifecycleStatus.PAUSED
                state.pause_reason = PauseReason.REPEATED_FAILURES
            await self._log_operation(
                session,
                account_id=account.id,
                operation_type=OperationType.FETCH,
                status=OperationStatus.FAILED,
                error_code=error.code,
                message=error.detail,
            )
            await session.commit()

        if retry_after_seconds and retry_after_seconds > 0:
            await self.coordinator.redis.setex(
                f"{self.coordinator.backoff_prefix}{account.id}",
                retry_after_seconds,
                "1",
            )
        elif error.retryable and error.pause_reason is None:
            await self.coordinator.schedule_backoff(account.id, failure_streak)

    async def _get_account(self, account_id: str) -> AccountConfig:
        account = await self.registry.get(account_id)
        if account is None:
            raise KeyError(account_id)
        return account

    async def _get_state(self, session: AsyncSession, account_id: str) -> AccountRuntimeState:
        state = await session.get(AccountRuntimeState, account_id)
        if state is None:
            raise KeyError(f"runtime state not found for {account_id}")
        return state

    def _is_within_quiet_hours(self, account: AccountConfig, now: datetime) -> bool:
        quiet_hours = account.fetch_schedule.quiet_hours
        if quiet_hours is None:
            return False
        local_hour = now.astimezone(self.settings.timezone).hour
        start, end = quiet_hours
        if start < end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end

    def _next_fetch_not_before(self, account: AccountConfig, now: datetime) -> datetime:
        jitter_minutes = 0
        if account.fetch_schedule.interval_jitter_minutes > 0:
            jitter_minutes = random.randint(0, account.fetch_schedule.interval_jitter_minutes)
        total_minutes = account.fetch_schedule.base_interval_minutes + jitter_minutes
        return now + timedelta(minutes=total_minutes)

    def _recent_tweet_cutoff(self, now: datetime) -> datetime | None:
        if self.settings.fetch_recent_window_hours <= 0:
            return None
        return now - timedelta(hours=self.settings.fetch_recent_window_hours)

    def _filter_batch_to_recent_window(
        self,
        batch: FetchBatchResult,
        *,
        cutoff: datetime | None,
    ) -> FetchBatchResult:
        if cutoff is None:
            return batch
        return FetchBatchResult(
            items=[
                item
                for item in batch.items
                if self._tweet_within_recent_window(item, cutoff)
            ],
            next_cursor=batch.next_cursor,
        )

    def _tweet_within_recent_window(
        self,
        item: Any,
        cutoff: datetime | None,
    ) -> bool:
        if cutoff is None:
            return True
        created_at = self._normalize_timestamp(getattr(item, "created_at", None))
        if created_at is None:
            return True
        return created_at >= cutoff

    def _page_reaches_recent_window_boundary(
        self,
        items: list[Any],
        cutoff: datetime | None,
    ) -> bool:
        if cutoff is None:
            return False
        for item in items:
            created_at = self._normalize_timestamp(getattr(item, "created_at", None))
            if created_at is not None and created_at < cutoff:
                return True
        return False

    def _normalize_timestamp(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    async def _get_cursor(
        self,
        session: AsyncSession,
        account_id: str,
        source: FetchSourceConfig,
        *,
        create: bool = False,
    ) -> FetchCursor | None:
        query = select(FetchCursor).where(
            FetchCursor.account_id == account_id,
            FetchCursor.source_type == source.source_type,
            FetchCursor.source_key == source.source_key,
        )
        cursor = (await session.execute(query)).scalar_one_or_none()
        if cursor is None and create:
            cursor = FetchCursor(
                account_id=account_id,
                source_type=source.source_type,
                source_key=source.source_key,
            )
            session.add(cursor)
            await session.flush()
        return cursor

    async def _log_operation(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        operation_type: OperationType,
        status: OperationStatus,
        message: str | None = None,
        error_code: FetchErrorCode | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            OperationLog(
                account_id=account_id,
                operation_type=operation_type,
                status=status,
                error_code=error_code,
                message=message,
                metadata_json=metadata,
            )
        )

    async def _record_follower_snapshot_if_needed(self, account: AccountConfig) -> None:
        now = datetime.now(UTC)
        snapshot_date = now.astimezone(self.settings.timezone).date()

        async with self.session_factory() as session:
            existing = (
                await session.execute(
                    select(AccountFollowerSnapshot).where(
                        AccountFollowerSnapshot.account_id == account.id,
                        AccountFollowerSnapshot.snapshot_date == snapshot_date,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return

        try:
            datasource = self.datasource_factory.get_primary_source()
            profile = await datasource.get_account_profile(account)
        except Exception:
            return
        if profile.follower_count is None:
            return

        async with self.session_factory() as session:
            existing = (
                await session.execute(
                    select(AccountFollowerSnapshot).where(
                        AccountFollowerSnapshot.account_id == account.id,
                        AccountFollowerSnapshot.snapshot_date == snapshot_date,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    AccountFollowerSnapshot(
                        account_id=account.id,
                        snapshot_date=snapshot_date,
                        follower_count=profile.follower_count,
                        captured_at=now,
                    )
                )
            else:
                existing.follower_count = profile.follower_count
                existing.captured_at = now
                existing.updated_at = now
            await session.commit()

    async def _record_fetch_filter_log(
        self,
        *,
        account_id: str,
        source: FetchSourceConfig,
        filter_counts: dict[str, int],
    ) -> None:
        if (
            filter_counts["reply_filtered"] <= 0
            and filter_counts["retweet_filtered"] <= 0
        ):
            return
        await self._record_ai_runtime_log(
            account_id=account_id,
            log_type="fetch_filter",
            status="success",
            request_payload={
                "source_type": source.source_type,
                "source_key": source.source_key,
            },
            response_payload={
                "reply_filtered": filter_counts["reply_filtered"],
                "retweet_filtered": filter_counts["retweet_filtered"],
            },
        )

    async def _record_ai_runtime_log(
        self,
        *,
        account_id: str | None,
        log_type: str,
        status: str,
        request_payload: dict[str, Any] | None,
        response_payload: dict[str, Any] | None,
        fetched_tweet_id: int | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                AILogRecord(
                    account_id=account_id,
                    fetched_tweet_id=fetched_tweet_id,
                    log_type=log_type,
                    status=status,
                    provider=self.settings.llm_provider.value,
                    model_id=self.settings.llm_model_id,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    error_message=error_message,
                )
            )
            await session.commit()
