from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.cookie_import import (
    CookieImportPreview,
    load_cookie_preview,
    scan_cookie_candidates,
)
from app.accounts.registry import AccountRegistry
from app.accounts.schemas import (
    AccountAdminView,
    AccountConfig,
    AccountConfigDocumentView,
    AccountConfigEditView,
    AccountConfigUpdateResult,
    AccountDashboardItem,
    AccountDeleteResponse,
    AccountStateChangeResponse,
    BudgetMeterView,
    CookieImportCandidateView,
    CookieImportRequest,
    CookieImportResult,
    DashboardSummary,
    DashboardView,
    FollowerSnapshotPoint,
    OperationLogView,
    ReloadSummary,
    RuntimeSettingsUpdateRequest,
    RuntimeSettingsUpdateResult,
    RuntimeSettingsView,
    TweetActionSummary,
    TweetAuthorCoverageItem,
    TweetCleanupResult,
    TweetDecisionSummary,
    TweetDetailView,
    TweetInteractionState,
    TweetListItem,
)
from app.fetching.text_extract import pick_best_tweet_text
from app.runtime.enums import (
    AccountLifecycleStatus,
    ActionStatus,
    ActionType,
    CookieFreshness,
    ExecutionMode,
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


class AccountService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        registry: AccountRegistry,
        coordinator: RuntimeCoordinator,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.coordinator = coordinator
        self.settings = settings

    async def reload_configs(self) -> ReloadSummary:
        accounts = await self.registry.reload()
        now = datetime.now(UTC)
        loaded_ids = {account.id for account in accounts}

        async with self.session_factory() as session:
            existing = {
                state.account_id: state
                for state in (await session.execute(select(AccountRuntimeState))).scalars()
            }
            new_accounts = 0
            updated_accounts = 0

            for account in accounts:
                state = existing.get(account.id)
                default_status = (
                    AccountLifecycleStatus.ENABLED
                    if account.enabled
                    else AccountLifecycleStatus.PAUSED
                )
                default_pause_reason = PauseReason.NONE if account.enabled else PauseReason.MANUAL
                default_proxy_health = ProxyHealth.UNKNOWN if account.proxy else ProxyHealth.DIRECT

                if state is None:
                    state = AccountRuntimeState(
                        account_id=account.id,
                        twitter_handle=account.twitter_handle,
                        lifecycle_status=default_status,
                        config_revision=account.config_revision or "",
                        source_path=str(account.source_file),
                        cookie_freshness=CookieFreshness.UNKNOWN,
                        proxy_health=default_proxy_health,
                        pause_reason=default_pause_reason,
                        failure_streak=0,
                        execution_mode=account.execution_mode,
                    )
                    session.add(state)
                    new_accounts += 1
                else:
                    state.twitter_handle = account.twitter_handle
                    state.config_revision = account.config_revision or ""
                    state.source_path = str(account.source_file)
                    state.execution_mode = account.execution_mode
                    if state.pause_reason == PauseReason.CONFIG_REMOVED:
                        state.pause_reason = default_pause_reason
                        state.lifecycle_status = default_status
                    elif state.pause_reason == PauseReason.ADMIN_DISABLED:
                        state.lifecycle_status = AccountLifecycleStatus.PAUSED
                    elif not account.enabled:
                        state.lifecycle_status = AccountLifecycleStatus.PAUSED
                        state.pause_reason = PauseReason.MANUAL
                    elif (
                        state.pause_reason == PauseReason.MANUAL
                        and state.lifecycle_status == AccountLifecycleStatus.PAUSED
                    ):
                        state.lifecycle_status = AccountLifecycleStatus.ENABLED
                        state.pause_reason = PauseReason.NONE
                    if not account.proxy:
                        state.proxy_health = ProxyHealth.DIRECT
                    updated_accounts += 1
                state.updated_at = now

            removed_accounts = 0
            for account_id, state in existing.items():
                if account_id in loaded_ids:
                    continue
                state.lifecycle_status = AccountLifecycleStatus.PAUSED
                state.pause_reason = PauseReason.CONFIG_REMOVED
                state.updated_at = now
                removed_accounts += 1

            await session.commit()

        return ReloadSummary(
            loaded_accounts=len(accounts),
            new_accounts=new_accounts,
            updated_accounts=updated_accounts,
            removed_accounts=removed_accounts,
        )

    async def list_accounts(self, *, fetch_limit_default: int) -> list[AccountAdminView]:
        accounts = await self.registry.list_accounts()
        async with self.session_factory() as session:
            states = {
                state.account_id: state
                for state in (await session.execute(select(AccountRuntimeState))).scalars()
            }

        views: list[AccountAdminView] = []
        for account in accounts:
            state = states.get(account.id)
            if state is None:
                continue
            views.append(
                AccountAdminView(
                    id=account.id,
                    twitter_handle=account.twitter_handle,
                    enabled=account.enabled,
                    execution_mode=account.execution_mode,
                    source_file=str(account.source_file),
                    config_revision=account.config_revision or "",
                    cookie_file=str(account.resolved_cookie_file),
                    fetch_schedule=account.fetch_schedule.model_copy(deep=True),
                    proxy_enabled=bool(account.proxy and account.proxy.enabled),
                    proxy_url_masked=account.proxy.masked_url if account.proxy else None,
                    fetch_sources=account.build_fetch_sources(fetch_limit_default),
                    lifecycle_status=AccountLifecycleStatus(state.lifecycle_status),
                    pause_reason=PauseReason(state.pause_reason or PauseReason.NONE),
                    last_auth_check=state.last_auth_check,
                    cookie_freshness=CookieFreshness(state.cookie_freshness),
                    proxy_health=ProxyHealth(state.proxy_health),
                    failure_streak=state.failure_streak,
                    last_fetch_started_at=state.last_fetch_started_at,
                    last_fetch_finished_at=state.last_fetch_finished_at,
                    last_error_code=state.last_error_code,
                    last_error_message=state.last_error_message,
                )
            )
        return views

    async def get_account_config(self, account_id: str) -> AccountConfigDocumentView:
        account = await self.registry.get(account_id)
        if account is None:
            raise KeyError(account_id)
        async with self.session_factory() as session:
            state = await session.get(AccountRuntimeState, account.id)
            if state is None:
                raise KeyError(account.id)
            operations = (
                await session.execute(
                    select(OperationLog)
                    .where(OperationLog.account_id == account.id)
                    .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
                    .limit(30)
                )
            ).scalars().all()
        return self._to_config_document(account, state, operations)

    async def list_cookie_import_candidates(self) -> list[CookieImportCandidateView]:
        import_dir = self.settings.resolve_path(self.settings.cookie_import_dir)
        previews = scan_cookie_candidates(import_dir)
        return [self._to_cookie_import_candidate(preview) for preview in previews]

    async def update_runtime_settings(
        self,
        payload: RuntimeSettingsUpdateRequest,
    ) -> RuntimeSettingsUpdateResult:
        env_file = self.settings.resolve_path(self.settings.app_env_file)
        env_values = self._read_env_file(env_file)
        env_values["AI_ENABLED"] = "true" if payload.ai_enabled else "false"
        env_values["FETCH_RECENT_WINDOW_HOURS"] = str(payload.fetch_recent_window_hours)
        env_values["FETCH_LATEST_FIRST"] = "true" if payload.fetch_latest_first else "false"
        env_values["FETCH_INCLUDE_REPLIES"] = (
            "true" if payload.fetch_include_replies else "false"
        )
        env_values["FETCH_INCLUDE_RETWEETS"] = (
            "true" if payload.fetch_include_retweets else "false"
        )
        env_values["LLM_PROVIDER"] = payload.llm_provider.value
        env_values["LLM_BASE_URL"] = payload.llm_base_url or ""
        env_values["LLM_MODEL_ID"] = payload.llm_model_id or ""
        if payload.replace_api_key:
            env_values["LLM_API_KEY"] = payload.llm_api_key or ""

        self._write_env_file(env_file, env_values)

        self.settings.ai_enabled = payload.ai_enabled
        self.settings.fetch_recent_window_hours = payload.fetch_recent_window_hours
        self.settings.fetch_latest_first = payload.fetch_latest_first
        self.settings.fetch_include_replies = payload.fetch_include_replies
        self.settings.fetch_include_retweets = payload.fetch_include_retweets
        self.settings.llm_provider = payload.llm_provider
        self.settings.llm_base_url = payload.llm_base_url or None
        self.settings.llm_model_id = payload.llm_model_id or None
        if payload.replace_api_key:
            self.settings.llm_api_key = payload.llm_api_key or None

        return RuntimeSettingsUpdateResult(
            runtime_settings=self._runtime_settings_view(),
            persisted_env_file=str(env_file),
        )

    def get_runtime_settings(self) -> RuntimeSettingsView:
        return self._runtime_settings_view()

    async def import_account_from_cookie(
        self,
        payload: CookieImportRequest,
        *,
        validate_session: bool,
        validate_session_func,
    ) -> CookieImportResult:
        import_dir = self.settings.resolve_path(self.settings.cookie_import_dir)
        source_path = (import_dir / payload.source_file).resolve()
        if not source_path.is_file() or source_path.parent != import_dir:
            raise ValueError("cookie source file is not allowed")

        preview = load_cookie_preview(source_path)
        if not preview.has_auth_token:
            raise ValueError("cookie file does not contain auth_token")

        account_id = payload.id or preview.suggested_account_id
        twitter_handle = payload.twitter_handle or preview.suggested_twitter_handle

        existing = await self.registry.get(account_id)
        if existing is not None:
            raise ValueError(f"account already exists: {account_id}")

        extra_payload = self._parse_extra_yaml(payload.extra_yaml)
        cookie_target = self.settings.resolve_path(self.settings.cookie_dir / f"{account_id}.json")
        config_target = self.settings.resolve_path(self.settings.config_dir / f"{account_id}.yaml")
        if cookie_target.exists():
            raise ValueError(f"cookie file already exists: {cookie_target}")
        if config_target.exists():
            raise ValueError(f"account config already exists: {config_target}")

        merged_payload = self._deep_merge_dicts(
            {
                "targets": {"timeline": True, "follow_users": [], "search_keywords": []},
            },
            extra_payload,
        )
        merged_payload.update(
            {
                "id": account_id,
                "twitter_handle": twitter_handle,
                "enabled": payload.enabled,
                "execution_mode": payload.execution_mode,
                "cookie_file": str(self.settings.cookie_dir / f"{account_id}.json"),
            }
        )

        candidate = AccountConfig.model_validate(merged_payload)
        serialized = self._serialize_config(candidate)
        self._write_json_atomic(cookie_target, preview.cookie_payload)
        self._write_yaml_atomic(config_target, serialized)

        try:
            summary = await self.reload_configs()
            validation_result = None
            if validate_session:
                validation_result = await validate_session_func(account_id)
            account_document = await self.get_account_config(account_id)
            return CookieImportResult(
                source_file=payload.source_file,
                imported_cookie_file=str(cookie_target),
                cookie_count=preview.twitter_cookie_count,
                has_auth_token=preview.has_auth_token,
                has_ct0=preview.has_ct0,
                validation_ok=validation_result.ok if validation_result else False,
                validation_detail=validation_result.detail if validation_result else None,
                validation_error_code=validation_result.error_code if validation_result else None,
                reload_summary=summary,
                account=account_document,
            )
        except Exception:
            if config_target.exists():
                config_target.unlink()
            if cookie_target.exists():
                cookie_target.unlink()
            await self.reload_configs()
            raise

    async def delete_account(self, account_id: str) -> AccountDeleteResponse:
        current = await self.registry.get(account_id)
        if current is None or current.source_file is None:
            raise KeyError(account_id)

        config_path = current.source_file.resolve()
        cookie_path = (
            current.resolved_cookie_file
            or self.registry.loader.resolve_cookie_path(current.cookie_file)
        ).resolve()
        guide_path = self.settings.resolve_path(current.writing_guide_file)

        async with self.session_factory() as session:
            await session.execute(
                delete(ActionRequest).where(ActionRequest.account_id == account_id)
            )
            await session.execute(
                delete(FetchedTweet).where(FetchedTweet.account_id == account_id)
            )
            await session.execute(
                delete(OperationLog).where(OperationLog.account_id == account_id)
            )
            await session.execute(
                delete(AccountFollowerSnapshot).where(
                    AccountFollowerSnapshot.account_id == account_id
                )
            )
            await session.execute(
                delete(FetchCursor).where(FetchCursor.account_id == account_id)
            )
            await session.execute(
                delete(AccountRuntimeState).where(AccountRuntimeState.account_id == account_id)
            )
            await session.commit()

        deleted_config_file = self._unlink_if_exists(config_path)
        managed_cookie_root = self.settings.resolve_path(self.settings.cookie_dir)
        deleted_cookie_file = False
        if self._is_within_path(cookie_path, managed_cookie_root):
            deleted_cookie_file = self._unlink_if_exists(cookie_path)
        deleted_writing_guide_file = False
        managed_guide_root = self.settings.resolve_path(self.settings.writing_guides_dir)
        if self._is_within_path(guide_path, managed_guide_root):
            deleted_writing_guide_file = self._unlink_if_exists(guide_path)

        summary = await self.reload_configs()
        return AccountDeleteResponse(
            account_id=account_id,
            deleted_config_file=deleted_config_file,
            deleted_cookie_file=deleted_cookie_file,
            deleted_writing_guide_file=deleted_writing_guide_file,
            reload_summary=summary,
        )

    async def update_account_config(
        self,
        account_id: str,
        payload: AccountConfigEditView,
    ) -> AccountConfigUpdateResult:
        current = await self.registry.get(account_id)
        if current is None or current.source_file is None:
            raise KeyError(account_id)

        candidate = AccountConfig.model_validate(
            {
                "id": payload.id,
                "twitter_handle": payload.twitter_handle,
                "enabled": payload.enabled,
                "execution_mode": payload.execution_mode,
                "cookie_file": payload.cookie_file,
                "proxy": payload.proxy.model_dump(mode="json") if payload.proxy else None,
                "targets": payload.targets.model_dump(mode="json"),
                "fetch_schedule": payload.fetch_schedule.model_dump(mode="json"),
                "behavior_budget": payload.behavior_budget.model_dump(mode="json"),
                "persona": payload.persona.model_dump(mode="json"),
                "writing_guide_file": payload.writing_guide_file,
            }
        )
        resolved_cookie = self.registry.loader.resolve_cookie_path(candidate.cookie_file)
        if not resolved_cookie.exists():
            raise ValueError(f"cookie file does not exist: {resolved_cookie}")

        source_file = current.source_file.resolve()
        serialized = self._serialize_config(candidate)
        self._write_yaml_atomic(source_file, serialized)

        summary = await self.reload_configs()
        updated_id = candidate.id
        updated = await self.get_account_config(updated_id)
        return AccountConfigUpdateResult(account=updated, reload_summary=summary)

    async def disable_account(self, account_id: str) -> AccountStateChangeResponse:
        account = await self.registry.get(account_id)
        if account is None:
            raise KeyError(account_id)

        async with self.session_factory() as session:
            state = await session.get(AccountRuntimeState, account_id)
            if state is None:
                raise KeyError(account_id)
            state.lifecycle_status = AccountLifecycleStatus.PAUSED
            state.pause_reason = PauseReason.ADMIN_DISABLED
            state.updated_at = datetime.now(UTC)
            await session.commit()

        return AccountStateChangeResponse(
            account_id=account_id,
            lifecycle_status=AccountLifecycleStatus.PAUSED,
            pause_reason=PauseReason.ADMIN_DISABLED,
        )

    async def enable_account(self, account_id: str) -> AccountStateChangeResponse:
        account = await self.registry.get(account_id)
        if account is None:
            raise KeyError(account_id)

        async with self.session_factory() as session:
            state = await session.get(AccountRuntimeState, account_id)
            if state is None:
                raise KeyError(account_id)
            if account.enabled:
                state.lifecycle_status = AccountLifecycleStatus.ENABLED
                state.pause_reason = PauseReason.NONE
            else:
                state.lifecycle_status = AccountLifecycleStatus.PAUSED
                state.pause_reason = PauseReason.MANUAL
            state.updated_at = datetime.now(UTC)
            await session.commit()
            lifecycle_status = AccountLifecycleStatus(state.lifecycle_status)
            pause_reason = PauseReason(state.pause_reason or PauseReason.NONE)

        return AccountStateChangeResponse(
            account_id=account_id,
            lifecycle_status=lifecycle_status,
            pause_reason=pause_reason,
        )

    async def get_dashboard(
        self,
        *,
        fetch_limit_default: int,
        recent_operations_limit: int = 12,
    ) -> DashboardView:
        account_views = await self.list_accounts(fetch_limit_default=fetch_limit_default)
        now = datetime.now(UTC)

        async with self.session_factory() as session:
            tweet_stats_result = await session.execute(
                select(
                    FetchedTweet.account_id,
                    func.count(FetchedTweet.id),
                    func.max(FetchedTweet.fetched_at),
                ).group_by(FetchedTweet.account_id)
            )
            tweet_stats = {
                account_id: {
                    "tweet_count": int(tweet_count),
                    "latest_tweet_at": latest_tweet_at,
                }
                for account_id, tweet_count, latest_tweet_at in tweet_stats_result.all()
            }
            totals_row = (
                await session.execute(
                    select(func.count(FetchedTweet.id), func.max(FetchedTweet.fetched_at))
                )
            ).one()
            total_tweets = int(totals_row[0] or 0)
            latest_fetch_at = totals_row[1]
            snapshot_rows = (
                await session.execute(
                    select(AccountFollowerSnapshot).order_by(
                        AccountFollowerSnapshot.account_id.asc(),
                        AccountFollowerSnapshot.snapshot_date.asc(),
                        AccountFollowerSnapshot.captured_at.asc(),
                    )
                )
            ).scalars().all()
            operations = (
                await session.execute(
                    select(OperationLog)
                    .order_by(OperationLog.created_at.desc(), OperationLog.id.desc())
                    .limit(recent_operations_limit)
                )
            ).scalars()

        dashboard_accounts: list[AccountDashboardItem] = []
        usable_accounts = 0
        registry_accounts = {account.id: account for account in await self.registry.list_accounts()}
        follower_history_map: dict[str, list[AccountFollowerSnapshot]] = defaultdict(list)
        for snapshot in snapshot_rows:
            follower_history_map[snapshot.account_id].append(snapshot)

        for account_view in account_views:
            stats = tweet_stats.get(account_view.id, {})
            registry_account = registry_accounts[account_view.id]
            budgets = await self._build_budget_meters(registry_account, now)
            daily_reset_in_seconds = self.coordinator.seconds_until_daily_reset(now)
            cooldown_until = await self.coordinator.get_action_cooldown_until(account_view.id)
            next_action_in_seconds = 0
            if cooldown_until and cooldown_until > now:
                next_action_in_seconds = int((cooldown_until - now).total_seconds())
            follower_history_rows = follower_history_map.get(account_view.id, [])[-14:]
            follower_count = None
            follower_delta = None
            follower_history = [
                FollowerSnapshotPoint(
                    snapshot_date=row.snapshot_date,
                    follower_count=row.follower_count,
                )
                for row in follower_history_rows
            ]
            if follower_history_rows:
                follower_count = follower_history_rows[-1].follower_count
            if len(follower_history_rows) >= 2:
                follower_delta = (
                    follower_history_rows[-1].follower_count
                    - follower_history_rows[-2].follower_count
                )

            if (
                account_view.enabled
                and account_view.lifecycle_status == AccountLifecycleStatus.ENABLED
                and account_view.cookie_freshness != CookieFreshness.EXPIRED
                and account_view.proxy_health != ProxyHealth.UNHEALTHY
            ):
                usable_accounts += 1

            dashboard_accounts.append(
                AccountDashboardItem(
                    id=account_view.id,
                    twitter_handle=account_view.twitter_handle,
                    enabled=account_view.enabled,
                    execution_mode=account_view.execution_mode,
                    persona_name=registry_account.persona.name,
                    writing_guide_file=str(registry_account.writing_guide_file),
                    fetch_schedule=registry_account.fetch_schedule.model_copy(deep=True),
                    lifecycle_status=account_view.lifecycle_status,
                    pause_reason=account_view.pause_reason,
                    cookie_freshness=account_view.cookie_freshness,
                    proxy_health=account_view.proxy_health,
                    failure_streak=account_view.failure_streak,
                    tweet_count=int(stats.get("tweet_count", 0)),
                    latest_tweet_at=stats.get("latest_tweet_at"),
                    last_fetch_finished_at=account_view.last_fetch_finished_at,
                    last_error_code=account_view.last_error_code,
                    last_error_message=account_view.last_error_message,
                    follower_count=follower_count,
                    follower_delta=follower_delta,
                    follower_history=follower_history,
                    budgets=budgets,
                    daily_reset_in_seconds=daily_reset_in_seconds,
                    next_action_in_seconds=next_action_in_seconds,
                )
            )

        recent_operations = [
            OperationLogView(
                account_id=operation.account_id,
                operation_type=operation.operation_type,
                status=operation.status,
                error_code=operation.error_code,
                message=operation.message,
                metadata=operation.metadata_json,
                created_at=operation.created_at,
            )
            for operation in operations
        ]

        enabled_accounts = sum(1 for account in account_views if account.enabled)
        paused_accounts = sum(
            1
            for account in account_views
            if account.lifecycle_status == AccountLifecycleStatus.PAUSED
        )

        return DashboardView(
            summary=DashboardSummary(
                total_accounts=len(account_views),
                enabled_accounts=enabled_accounts,
                usable_accounts=usable_accounts,
                paused_accounts=paused_accounts,
                total_tweets=total_tweets,
                latest_fetch_at=latest_fetch_at,
            ),
            runtime_settings=self._runtime_settings_view(),
            accounts=dashboard_accounts,
            recent_operations=recent_operations,
        )

    async def list_tweets(
        self,
        *,
        account_id: str | None = None,
        source_type: SourceType | None = None,
        limit: int = 100,
    ) -> list[TweetListItem]:
        query = select(FetchedTweet).order_by(
            FetchedTweet.fetched_at.desc(),
            FetchedTweet.id.desc(),
        )
        if account_id:
            query = query.where(FetchedTweet.account_id == account_id)
        if source_type:
            query = query.where(FetchedTweet.source_type == source_type)

        async with self.session_factory() as session:
            tweets = (await session.execute(query.limit(limit))).scalars().all()
            action_map = await self._latest_action_map(
                session,
                [tweet.id for tweet in tweets],
            )
            decision_map = await self._latest_decision_map(
                session,
                [tweet.id for tweet in tweets],
            )

        items = [
            TweetListItem(
                id=tweet.id,
                tweet_id=tweet.tweet_id,
                account_id=tweet.account_id,
                source_type=SourceType(tweet.source_type),
                source_key=tweet.source_key,
                author_handle=tweet.author_handle,
                text=tweet.text,
                lang=tweet.lang,
                created_at_twitter=tweet.created_at_twitter,
                fetched_at=tweet.fetched_at,
                tweet_url=f"https://x.com/i/status/{tweet.tweet_id}",
                interaction_state=self._tweet_interaction_state(
                    action_map.get(tweet.id, {}),
                    decision_map.get(tweet.id),
                ),
                latest_decision=decision_map.get(tweet.id),
                latest_reply_action=action_map.get(tweet.id, {}).get(ActionType.REPLY),
                latest_like_action=action_map.get(tweet.id, {}).get(ActionType.LIKE),
            )
            for tweet in tweets
        ]
        return items

    async def cleanup_stale_tweets(
        self,
        *,
        account_id: str | None,
    ) -> TweetCleanupResult:
        if account_id is not None and await self.registry.get(account_id) is None:
            raise KeyError(account_id)

        now = datetime.now(UTC)
        cutoff = self._tweet_recent_cutoff(now)
        accounts = {
            account.id: account
            for account in await self.registry.list_accounts()
        }

        query = select(FetchedTweet)
        if account_id:
            query = query.where(FetchedTweet.account_id == account_id)

        async with self.session_factory() as session:
            tweets = (await session.execute(query)).scalars().all()

            delete_ids: list[int] = []
            reasons = {
                "missing_account": 0,
                "outside_window": 0,
                "filtered_reply": 0,
                "filtered_retweet": 0,
                "disabled_scope": 0,
            }
            for tweet in tweets:
                reason = self._cleanup_reason_for_tweet(
                    tweet,
                    accounts=accounts,
                    cutoff=cutoff,
                )
                if reason is None:
                    continue
                delete_ids.append(tweet.id)
                reasons[reason] += 1

            deleted_actions = 0
            deleted_ai_logs = 0
            deleted_tweets = 0
            if delete_ids:
                deleted_actions = await self._execute_delete(
                    session,
                    delete(ActionRequest).where(ActionRequest.fetched_tweet_id.in_(delete_ids)),
                )
                deleted_ai_logs = await self._execute_delete(
                    session,
                    delete(AILogRecord).where(AILogRecord.fetched_tweet_id.in_(delete_ids)),
                )
                deleted_tweets = await self._execute_delete(
                    session,
                    delete(FetchedTweet).where(FetchedTweet.id.in_(delete_ids)),
                )
                await session.commit()

        return TweetCleanupResult(
            account_id=account_id,
            deleted_tweets=deleted_tweets,
            deleted_ai_logs=deleted_ai_logs,
            deleted_actions=deleted_actions,
            deleted_missing_account_tweets=reasons["missing_account"],
            deleted_outside_window_tweets=reasons["outside_window"],
            deleted_filtered_reply_tweets=reasons["filtered_reply"],
            deleted_filtered_retweet_tweets=reasons["filtered_retweet"],
            deleted_disabled_scope_tweets=reasons["disabled_scope"],
        )

    async def get_tweet_detail(self, tweet_record_id: int) -> TweetDetailView:
        async with self.session_factory() as session:
            tweet = await session.get(FetchedTweet, tweet_record_id)
            if tweet is None:
                raise KeyError(tweet_record_id)
            actions = (
                await session.execute(
                    select(ActionRequest)
                    .where(ActionRequest.fetched_tweet_id == tweet_record_id)
                    .order_by(ActionRequest.created_at.desc(), ActionRequest.id.desc())
                )
            ).scalars().all()
            follow_actions = []
            if tweet.author_handle:
                follow_actions = (
                    await session.execute(
                        select(ActionRequest)
                        .where(
                            ActionRequest.action_type == ActionType.FOLLOW,
                            ActionRequest.target_user_handle == tweet.author_handle,
                        )
                        .order_by(ActionRequest.updated_at.desc(), ActionRequest.id.desc())
                )
            ).scalars().all()
            decision_map = await self._latest_decision_map(session, [tweet_record_id])

        summaries = [self._to_tweet_action_summary(action) for action in actions]
        latest_by_type: dict[str, TweetActionSummary] = {}
        for summary in summaries:
            latest_by_type.setdefault(summary.action_type, summary)
        author_coverage = await self._build_author_coverage(
            author_handle=tweet.author_handle,
            follow_actions=follow_actions,
        )

        return TweetDetailView(
            id=tweet.id,
            tweet_id=tweet.tweet_id,
            account_id=tweet.account_id,
            source_type=SourceType(tweet.source_type),
            source_key=tweet.source_key,
            author_handle=tweet.author_handle,
            text=pick_best_tweet_text(tweet.text, tweet.raw_payload),
            lang=tweet.lang,
            created_at_twitter=tweet.created_at_twitter,
            fetched_at=tweet.fetched_at,
            tweet_url=f"https://x.com/i/status/{tweet.tweet_id}",
            interaction_state=self._tweet_interaction_state(
                latest_by_type,
                decision_map.get(tweet.id),
            ),
            latest_decision=decision_map.get(tweet.id),
            raw_payload=tweet.raw_payload,
            author_coverage=author_coverage,
            latest_reply_action=latest_by_type.get(ActionType.REPLY),
            latest_like_action=latest_by_type.get(ActionType.LIKE),
            actions=summaries,
        )

    async def ensure_follow_target(
        self,
        account_id: str,
        user_handle: str,
        *,
        default_count: int,
    ) -> tuple[AccountConfigDocumentView, bool]:
        current = await self.registry.get(account_id)
        if current is None or current.source_file is None:
            raise KeyError(account_id)

        normalized_handle = self._normalize_handle(user_handle)
        if normalized_handle.lower() == current.twitter_handle.lower():
            raise ValueError("account cannot follow itself")

        existing_handles = {item.handle.lower() for item in current.targets.follow_users}
        if normalized_handle.lower() in existing_handles:
            return await self.get_account_config(account_id), False

        payload = AccountConfigEditView.model_validate(
            {
                **self._to_config_edit_view(current).model_dump(mode="json"),
                "targets": {
                    **current.targets.model_dump(mode="json"),
                    "follow_users": [
                        *current.targets.model_dump(mode="json").get("follow_users", []),
                        {"handle": normalized_handle, "count": default_count},
                    ],
                },
            }
        )
        updated = await self.update_account_config(account_id, payload)
        return updated.account, True

    async def _build_budget_meters(
        self,
        account: AccountConfig,
        now: datetime,
    ) -> list[BudgetMeterView]:
        limits = {
            ActionType.LIKE: account.behavior_budget.daily_likes_max,
            ActionType.REPLY: account.behavior_budget.daily_replies_max,
            ActionType.FOLLOW: account.behavior_budget.daily_follows_max,
        }
        meters: list[BudgetMeterView] = []
        for action_type, limit in limits.items():
            used = await self.coordinator.get_daily_action_count(account.id, action_type, now)
            remaining = max(limit - used, 0)
            ratio = float(remaining / limit) if limit else 0.0
            meters.append(
                BudgetMeterView(
                    action_type=action_type,
                    used=used,
                    max=limit,
                    remaining=remaining,
                    ratio=ratio,
                )
            )
        return meters

    async def _build_author_coverage(
        self,
        *,
        author_handle: str | None,
        follow_actions: list[ActionRequest],
    ) -> list[TweetAuthorCoverageItem]:
        if not author_handle:
            return []

        normalized_handle = self._normalize_handle(author_handle)
        accounts = await self.registry.list_accounts()
        latest_follow_action_by_account: dict[str, TweetActionSummary] = {}
        for action in follow_actions:
            if action.account_id in latest_follow_action_by_account:
                continue
            latest_follow_action_by_account[action.account_id] = self._to_tweet_action_summary(
                action
            )

        async with self.session_factory() as session:
            states = {
                state.account_id: state
                for state in (await session.execute(select(AccountRuntimeState))).scalars()
            }

        coverage: list[TweetAuthorCoverageItem] = []
        for account in accounts:
            state = states.get(account.id)
            if state is None:
                continue
            is_self = account.twitter_handle.lower() == normalized_handle.lower()
            in_follow_targets = any(
                item.handle.lower() == normalized_handle.lower()
                for item in account.targets.follow_users
            )
            follow_action = latest_follow_action_by_account.get(account.id)
            action_counts_as_following = (
                follow_action is not None
                and follow_action.status
                in {
                    ActionStatus.APPROVED.value,
                    ActionStatus.EXECUTING.value,
                    ActionStatus.SUCCEEDED.value,
                }
            )
            follows_author = is_self or in_follow_targets or action_counts_as_following
            reason = "none"
            if is_self:
                reason = "self"
            elif in_follow_targets:
                reason = "follow_target"
            elif action_counts_as_following:
                reason = "follow_action"

            coverage.append(
                TweetAuthorCoverageItem(
                    account_id=account.id,
                    twitter_handle=account.twitter_handle,
                    execution_mode=account.execution_mode,
                    lifecycle_status=AccountLifecycleStatus(state.lifecycle_status),
                    follows_author=follows_author,
                    can_add_follow=not follows_author,
                    follow_reason=reason,
                    latest_follow_action=follow_action,
                )
            )

        coverage.sort(
            key=lambda item: (
                not item.follows_author,
                item.account_id,
            )
        )
        return coverage

    async def _latest_action_map(
        self,
        session: AsyncSession,
        tweet_ids: list[int],
    ) -> dict[int, dict[str, TweetActionSummary]]:
        if not tweet_ids:
            return {}
        actions = (
            await session.execute(
                select(ActionRequest)
                .where(ActionRequest.fetched_tweet_id.in_(tweet_ids))
                .order_by(ActionRequest.updated_at.desc(), ActionRequest.id.desc())
            )
        ).scalars()
        mapped: dict[int, dict[str, TweetActionSummary]] = {}
        for action in actions:
            if action.fetched_tweet_id is None:
                continue
            bucket = mapped.setdefault(action.fetched_tweet_id, {})
            if action.action_type in bucket:
                continue
            bucket[action.action_type] = self._to_tweet_action_summary(action)
        return mapped

    async def _latest_decision_map(
        self,
        session: AsyncSession,
        tweet_ids: list[int],
    ) -> dict[int, TweetDecisionSummary]:
        if not tweet_ids:
            return {}
        logs = (
            await session.execute(
                select(AILogRecord)
                .where(
                    AILogRecord.fetched_tweet_id.in_(tweet_ids),
                    AILogRecord.log_type == "decision",
                )
                .order_by(AILogRecord.created_at.desc(), AILogRecord.id.desc())
            )
        ).scalars()
        fallback: dict[int, TweetDecisionSummary] = {}
        preferred: dict[int, TweetDecisionSummary] = {}
        for log in logs:
            if log.fetched_tweet_id is None:
                continue
            summary = self._to_tweet_decision_summary(log)
            fallback.setdefault(log.fetched_tweet_id, summary)
            if summary.status == "success":
                preferred.setdefault(log.fetched_tweet_id, summary)
        mapped = fallback
        mapped.update(preferred)
        return mapped

    def _to_tweet_decision_summary(self, log: AILogRecord) -> TweetDecisionSummary:
        result = {}
        if log.response_payload and isinstance(log.response_payload, dict):
            raw_result = log.response_payload.get("result")
            if isinstance(raw_result, dict):
                result = raw_result
        return TweetDecisionSummary(
            status=log.status,
            relevance_score=result.get("relevance_score"),
            reply_confidence=result.get("reply_confidence"),
            rationale=result.get("rationale"),
            created_at=log.created_at,
        )

    def _tweet_interaction_state(
        self,
        action_bucket: dict[str, TweetActionSummary],
        decision: TweetDecisionSummary | None,
    ) -> TweetInteractionState:
        if action_bucket:
            return "acted"
        if decision and decision.status == "success":
            return "scored_no_action"
        return "unscored"

    def _serialize_config(self, account: AccountConfig) -> dict[str, Any]:
        payload = {
            "id": account.id,
            "twitter_handle": account.twitter_handle,
            "enabled": account.enabled,
            "execution_mode": account.execution_mode.value,
            "cookie_file": str(account.cookie_file),
            "targets": account.targets.model_dump(mode="json"),
            "fetch_schedule": account.fetch_schedule.model_dump(mode="json"),
            "behavior_budget": account.behavior_budget.model_dump(mode="json"),
            "persona": account.persona.model_dump(mode="json"),
            "writing_guide_file": str(account.writing_guide_file),
        }
        if account.proxy is not None:
            payload["proxy"] = account.proxy.model_dump(mode="json")
        return payload

    def _tweet_recent_cutoff(self, now: datetime) -> datetime | None:
        if self.settings.fetch_recent_window_hours <= 0:
            return None
        return now - timedelta(hours=self.settings.fetch_recent_window_hours)

    def _cleanup_reason_for_tweet(
        self,
        tweet: FetchedTweet,
        *,
        accounts: dict[str, AccountConfig],
        cutoff: datetime | None,
    ) -> str | None:
        account = accounts.get(tweet.account_id)
        if account is None:
            return "missing_account"
        if cutoff is not None:
            created_at = self._normalize_timestamp(tweet.created_at_twitter or tweet.fetched_at)
            if created_at < cutoff:
                return "outside_window"
        if not self.settings.fetch_include_replies and self._stored_tweet_is_reply(tweet):
            return "filtered_reply"
        if not self.settings.fetch_include_retweets and self._stored_tweet_is_retweet(tweet):
            return "filtered_retweet"
        if not self._tweet_matches_account_scope(tweet, account):
            return "disabled_scope"
        return None

    def _tweet_matches_account_scope(self, tweet: FetchedTweet, account: AccountConfig) -> bool:
        source_type = SourceType(tweet.source_type)
        if source_type == SourceType.TIMELINE:
            return account.targets.timeline
        if source_type == SourceType.WATCH_USER:
            return account.targets.follow_users_enabled and any(
                item.handle.lower() == tweet.source_key.lower()
                for item in account.targets.follow_users
            )
        if source_type == SourceType.KEYWORD_SEARCH:
            return account.targets.search_keywords_enabled and any(
                item.query == tweet.source_key
                for item in account.targets.search_keywords
            )
        return True

    def _normalize_timestamp(self, value: datetime | None) -> datetime:
        if value is None:
            return datetime.min.replace(tzinfo=UTC)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _stored_tweet_is_reply(self, tweet: FetchedTweet) -> bool:
        payload = tweet.raw_payload or {}
        legacy = payload.get("legacy") if isinstance(payload, dict) else None
        if isinstance(legacy, dict) and legacy.get("in_reply_to_status_id_str"):
            return True
        if isinstance(payload, dict) and payload.get("in_reply_to_status_id"):
            return True
        return False

    def _stored_tweet_is_retweet(self, tweet: FetchedTweet) -> bool:
        payload = tweet.raw_payload or {}
        legacy = payload.get("legacy") if isinstance(payload, dict) else None
        if isinstance(legacy, dict) and legacy.get("retweeted_status_result"):
            return True
        if isinstance(payload, dict) and payload.get("retweeted_status_result"):
            return True
        text = tweet.text.strip().lower()
        return text.startswith("rt @")

    async def _execute_delete(self, session: AsyncSession, statement) -> int:
        result = await session.execute(statement)
        return int(result.rowcount or 0)

    def _to_cookie_import_candidate(
        self,
        preview: CookieImportPreview,
    ) -> CookieImportCandidateView:
        return CookieImportCandidateView(
            source_file=preview.source_path.name,
            format_name=preview.format_name,
            suggested_account_id=preview.suggested_account_id,
            suggested_twitter_handle=preview.suggested_twitter_handle,
            twitter_cookie_count=preview.twitter_cookie_count,
            detected_domains=preview.detected_domains,
            has_auth_token=preview.has_auth_token,
            has_ct0=preview.has_ct0,
            warnings=list(preview.warnings),
        )

    def _runtime_settings_view(self) -> RuntimeSettingsView:
        env_file = self.settings.resolve_path(self.settings.app_env_file)
        return RuntimeSettingsView(
            current_env_file=str(env_file),
            app_timezone=self.settings.app_timezone,
            ai_enabled=self.settings.ai_enabled,
            fetch_recent_window_hours=self.settings.fetch_recent_window_hours,
            fetch_latest_first=self.settings.fetch_latest_first,
            fetch_include_replies=self.settings.fetch_include_replies,
            fetch_include_retweets=self.settings.fetch_include_retweets,
            llm_provider=self.settings.llm_provider,
            llm_base_url=self.settings.llm_base_url,
            llm_model_id=self.settings.llm_model_id,
            llm_api_key_configured=bool(self.settings.llm_api_key),
            llm_api_key_masked=self.settings.llm_api_key_masked,
        )

    def _to_config_edit_view(self, account: AccountConfig) -> AccountConfigEditView:
        return AccountConfigEditView(
            id=account.id,
            twitter_handle=account.twitter_handle,
            enabled=account.enabled,
            execution_mode=account.execution_mode,
            cookie_file=str(account.cookie_file),
            proxy=account.proxy,
            targets=account.targets.model_copy(deep=True),
            fetch_schedule=account.fetch_schedule.model_copy(deep=True),
            behavior_budget=account.behavior_budget.model_copy(deep=True),
            persona=account.persona.model_copy(deep=True),
            writing_guide_file=str(account.writing_guide_file),
        )

    def _to_config_document(
        self,
        account: AccountConfig,
        state: AccountRuntimeState,
        operations: list[OperationLog] | None = None,
    ) -> AccountConfigDocumentView:
        return AccountConfigDocumentView(
            account=self._to_config_edit_view(account),
            source_file=str(account.source_file),
            config_revision=account.config_revision or "",
            lifecycle_status=AccountLifecycleStatus(state.lifecycle_status),
            pause_reason=PauseReason(state.pause_reason or PauseReason.NONE),
            cookie_freshness=CookieFreshness(state.cookie_freshness),
            proxy_health=ProxyHealth(state.proxy_health),
            failure_streak=state.failure_streak,
            last_auth_check=state.last_auth_check,
            last_fetch_finished_at=state.last_fetch_finished_at,
            last_error_code=state.last_error_code,
            last_error_message=state.last_error_message,
            recent_operations=[
                OperationLogView(
                    account_id=operation.account_id,
                    operation_type=operation.operation_type,
                    status=operation.status,
                    error_code=operation.error_code,
                    message=operation.message,
                    metadata=operation.metadata_json,
                    created_at=operation.created_at,
                )
                for operation in (operations or [])
            ],
        )

    def _to_tweet_action_summary(self, action: ActionRequest) -> TweetActionSummary:
        applied_execution_mode = None
        if action.applied_execution_mode:
            applied_execution_mode = ExecutionMode(action.applied_execution_mode)
        return TweetActionSummary(
            id=action.id,
            action_type=action.action_type,
            status=action.status,
            ai_draft=action.ai_draft,
            edited_draft=action.edited_draft,
            final_draft=action.final_draft,
            relevance_score=action.relevance_score,
            reply_confidence=action.reply_confidence,
            requested_execution_mode=ExecutionMode(action.requested_execution_mode),
            applied_execution_mode=applied_execution_mode,
            created_at=action.created_at,
            updated_at=action.updated_at,
        )

    def _normalize_handle(self, value: str) -> str:
        cleaned = value.strip()
        return cleaned if cleaned.startswith("@") else f"@{cleaned}"

    def _parse_extra_yaml(self, value: str | None) -> dict[str, Any]:
        if not value or not value.strip():
            return {}
        parsed = yaml.safe_load(value)
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ValueError("extra_yaml must be a YAML mapping")
        return parsed

    def _deep_merge_dicts(
        self,
        base: dict[str, Any],
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(base)
        for key, value in extra.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = self._deep_merge_dicts(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _write_yaml_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _write_json_atomic(self, path: Path, payload: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _read_env_file(self, path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        if not path.exists():
            return values
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in raw_line:
                continue
            key, value = raw_line.split("=", 1)
            values[key.strip()] = value.strip()
        return values

    def _write_env_file(self, path: Path, values: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp")
        lines = [f"{key}={value}" for key, value in values.items()]
        temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temp_path.replace(path)

    def _unlink_if_exists(self, path: Path) -> bool:
        if not path.exists():
            return False
        path.unlink()
        return True

    def _is_within_path(self, path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return False
