from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.registry import AccountRegistry
from app.actions.executor import TwitterActionExecutor
from app.actions.schemas import (
    ActionDecisionRequest,
    ActionModifyRequest,
    ActionRequestView,
    FollowActionCreateRequest,
    GenerateReplyRequest,
    LikeActionCreateRequest,
    ReplyApprovalCreateRequest,
    ReplyWorkspaceItem,
    TweetDecisionPreview,
    TweetGenerationView,
)
from app.actions.writing_guides import WritingGuideService
from app.fetching.text_extract import pick_best_tweet_text
from app.llm.schemas import DecisionResult
from app.llm.service import LLMService
from app.runtime.enums import (
    AccountLifecycleStatus,
    ActionErrorCode,
    ActionStatus,
    ActionType,
    ExecutionMode,
    LearningStatus,
    OperationStatus,
    OperationType,
    PauseReason,
)
from app.runtime.models import (
    AccountRuntimeState,
    ActionRequest,
    AILogRecord,
    FetchedTweet,
    OperationLog,
)
from app.runtime.redis import RuntimeCoordinator
from app.runtime.settings import Settings


@dataclass(slots=True)
class BudgetDecision:
    allowed: bool
    error_code: ActionErrorCode | None
    message: str | None
    snapshot: dict[str, Any]


class ActionService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        registry: AccountRegistry,
        coordinator: RuntimeCoordinator,
        settings: Settings,
        llm_service: LLMService,
        action_executor: TwitterActionExecutor,
        writing_guide_service: WritingGuideService,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry
        self.coordinator = coordinator
        self.settings = settings
        self.llm_service = llm_service
        self.action_executor = action_executor
        self.writing_guide_service = writing_guide_service

    async def list_approvals(
        self,
        *,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[ActionRequestView]:
        return await self.list_actions(
            account_id=account_id,
            status=ActionStatus.PENDING_APPROVAL,
            limit=limit,
        )

    async def list_actions(
        self,
        *,
        account_id: str | None = None,
        status: ActionStatus | None = None,
        limit: int = 100,
    ) -> list[ActionRequestView]:
        query = select(ActionRequest).order_by(
            ActionRequest.created_at.desc(),
            ActionRequest.id.desc(),
        )
        if account_id:
            query = query.where(ActionRequest.account_id == account_id)
        if status:
            query = query.where(ActionRequest.status == status)

        async with self.session_factory() as session:
            actions = (await session.execute(query.limit(limit))).scalars().all()
        return [self._to_view(action) for action in actions]

    async def list_reply_workspace(
        self,
        *,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[ReplyWorkspaceItem]:
        accounts = {account.id: account for account in await self.registry.list_accounts()}
        query = (
            select(ActionRequest, FetchedTweet)
            .outerjoin(FetchedTweet, FetchedTweet.id == ActionRequest.fetched_tweet_id)
            .where(
                ActionRequest.action_type == ActionType.REPLY,
                ActionRequest.status == ActionStatus.PENDING_APPROVAL,
            )
            .order_by(ActionRequest.created_at.desc(), ActionRequest.id.desc())
        )
        if account_id:
            query = query.where(ActionRequest.account_id == account_id)
        query = query.limit(limit)

        async with self.session_factory() as session:
            rows = (await session.execute(query)).all()

        items: list[ReplyWorkspaceItem] = []
        for action, tweet in rows:
            account = accounts.get(action.account_id)
            tweet_text = ""
            tweet_author_handle = action.target_user_handle
            tweet_url = None
            if tweet is not None:
                tweet_text = pick_best_tweet_text(tweet.text, tweet.raw_payload)
                tweet_author_handle = tweet.author_handle or tweet_author_handle
                tweet_url = f"https://x.com/i/status/{tweet.tweet_id}"

            items.append(
                ReplyWorkspaceItem(
                    id=action.id,
                    account_id=action.account_id,
                    account_twitter_handle=account.twitter_handle if account else action.account_id,
                    status=ActionStatus(action.status),
                    requested_execution_mode=ExecutionMode(action.requested_execution_mode),
                    fetched_tweet_id=action.fetched_tweet_id,
                    target_tweet_id=action.target_tweet_id,
                    target_user_handle=action.target_user_handle,
                    tweet_text=tweet_text,
                    tweet_author_handle=tweet_author_handle,
                    tweet_url=tweet_url,
                    ai_draft=action.ai_draft,
                    edited_draft=action.edited_draft,
                    final_draft=action.final_draft,
                    relevance_score=action.relevance_score,
                    reply_confidence=action.reply_confidence,
                    error_code=action.error_code,
                    error_message=action.error_message,
                    created_at=self._as_utc(action.created_at) or action.created_at,
                    updated_at=self._as_utc(action.updated_at) or action.updated_at,
                )
            )
        return items

    async def generate_reply_for_tweet(
        self,
        tweet_record_id: int,
        payload: GenerateReplyRequest,
    ) -> TweetGenerationView:
        if not self.settings.ai_enabled:
            raise ValueError("AI is disabled")
        account = await self._get_account(payload.account_id)
        async with self.session_factory() as session:
            tweet = await session.get(FetchedTweet, tweet_record_id)
            if tweet is None:
                raise KeyError(tweet_record_id)
            if tweet.account_id != account.id:
                raise ValueError("tweet does not belong to account")
            reusable_decision = await self._find_reusable_decision(
                session,
                canonical_tweet_id=tweet.tweet_id,
                exclude_fetched_tweet_id=tweet.id,
            )
            tweet_text = pick_best_tweet_text(tweet.text, tweet.raw_payload)

        if reusable_decision is not None:
            decision, source_account_id, source_fetched_tweet_id = reusable_decision
            await self._record_reused_decision(
                account_id=account.id,
                fetched_tweet_id=tweet.id,
                canonical_tweet_id=tweet.tweet_id,
                source_account_id=source_account_id,
                source_fetched_tweet_id=source_fetched_tweet_id,
                decision=decision,
            )
        else:
            writing_guide = await self.writing_guide_service.read_text(account)
            decision = await self.llm_service.generate_decision(
                account=account,
                tweet_text=tweet_text,
                author_handle=tweet.author_handle,
                writing_guide=writing_guide,
                fetched_tweet_id=tweet.id,
            )

        like_action = None
        if decision.like:
            like_action = await self.create_like_request(
                LikeActionCreateRequest(
                    account_id=account.id,
                    target_tweet_id=tweet.tweet_id,
                    target_user_handle=tweet.author_handle,
                    trigger_source=payload.trigger_source,
                    fetched_tweet_id=tweet.id,
                ),
                ai_metadata={
                    "relevance_score": decision.relevance_score,
                    "reply_confidence": decision.reply_confidence,
                    "llm_provider": self.settings.llm_provider.value,
                    "llm_model": self.settings.llm_model_id,
                },
            )

        reply_action = None
        if decision.reply_draft:
            reply_action = await self.create_reply_request(
                ReplyApprovalCreateRequest(
                    account_id=account.id,
                    target_tweet_id=tweet.tweet_id,
                    target_user_handle=tweet.author_handle,
                    content_draft=decision.reply_draft,
                    trigger_source=payload.trigger_source,
                    fetched_tweet_id=tweet.id,
                ),
                ai_metadata={
                    "relevance_score": decision.relevance_score,
                    "reply_confidence": decision.reply_confidence,
                    "llm_provider": self.settings.llm_provider.value,
                    "llm_model": self.settings.llm_model_id,
                    "ai_draft": decision.reply_draft,
                    "final_draft": decision.reply_draft,
                },
            )

        return TweetGenerationView(
            tweet_record_id=tweet.id,
            account_id=account.id,
            decision=TweetDecisionPreview.model_validate(decision.model_dump()),
            like_action=like_action,
            reply_action=reply_action,
        )

    async def create_reply_request(
        self,
        payload: ReplyApprovalCreateRequest,
        *,
        ai_metadata: dict[str, Any] | None = None,
    ) -> ActionRequestView:
        account = await self._get_account(payload.account_id)
        now = datetime.now(UTC)
        metadata = ai_metadata or {}
        async with self.session_factory() as session:
            reusable = await self._find_existing_action(
                session,
                account_id=account.id,
                action_type=ActionType.REPLY,
                target_tweet_id=payload.target_tweet_id,
                target_user_handle=payload.target_user_handle,
                fetched_tweet_id=payload.fetched_tweet_id,
                reusable_statuses={
                    ActionStatus.PENDING_APPROVAL,
                    ActionStatus.APPROVED,
                    ActionStatus.EXECUTING,
                    ActionStatus.SUCCEEDED,
                },
            )
            if reusable is not None:
                if reusable.status == ActionStatus.APPROVED:
                    await self.coordinator.enqueue_action(reusable.id)
                return self._to_view(reusable)

            action = ActionRequest(
                account_id=account.id,
                action_type=ActionType.REPLY,
                status=ActionStatus.PENDING_APPROVAL,
                trigger_source=payload.trigger_source,
                requested_execution_mode=account.execution_mode,
                fetched_tweet_id=payload.fetched_tweet_id,
                target_tweet_id=payload.target_tweet_id,
                target_user_handle=payload.target_user_handle,
                content_draft=payload.content_draft,
                ai_draft=metadata.get("ai_draft"),
                final_draft=metadata.get("final_draft") or payload.content_draft,
                relevance_score=metadata.get("relevance_score"),
                reply_confidence=metadata.get("reply_confidence"),
                llm_provider=metadata.get("llm_provider"),
                llm_model=metadata.get("llm_model"),
                learning_status=LearningStatus.NONE,
                budget_snapshot=await self._budget_snapshot(account.id, ActionType.REPLY, now),
                audit_log=[],
                expires_at=now + timedelta(hours=payload.expires_in_hours),
            )
            self._append_audit(action, "created", now, detail="reply approval created")
            session.add(action)
            await self._log_operation(
                session,
                account_id=account.id,
                status=OperationStatus.SUCCESS,
                message="reply approval created",
                metadata={
                    "action_type": ActionType.REPLY,
                    "target_tweet_id": payload.target_tweet_id,
                    "fetched_tweet_id": payload.fetched_tweet_id,
                },
            )
            await session.commit()
            await session.refresh(action)
        return self._to_view(action)

    async def create_like_request(
        self,
        payload: LikeActionCreateRequest,
        *,
        ai_metadata: dict[str, Any] | None = None,
    ) -> ActionRequestView:
        account = await self._get_account(payload.account_id)
        now = datetime.now(UTC)
        metadata = ai_metadata or {}
        async with self.session_factory() as session:
            reusable = await self._find_existing_action(
                session,
                account_id=account.id,
                action_type=ActionType.LIKE,
                target_tweet_id=payload.target_tweet_id,
                target_user_handle=payload.target_user_handle,
                fetched_tweet_id=payload.fetched_tweet_id,
                reusable_statuses={
                    ActionStatus.APPROVED,
                    ActionStatus.EXECUTING,
                    ActionStatus.SUCCEEDED,
                },
            )
            if reusable is not None:
                if reusable.status == ActionStatus.APPROVED:
                    await self.coordinator.enqueue_action(reusable.id)
                return self._to_view(reusable)

            action = ActionRequest(
                account_id=account.id,
                action_type=ActionType.LIKE,
                status=ActionStatus.APPROVED,
                trigger_source=payload.trigger_source,
                requested_execution_mode=account.execution_mode,
                fetched_tweet_id=payload.fetched_tweet_id,
                target_tweet_id=payload.target_tweet_id,
                target_user_handle=payload.target_user_handle,
                relevance_score=metadata.get("relevance_score"),
                reply_confidence=metadata.get("reply_confidence"),
                llm_provider=metadata.get("llm_provider"),
                llm_model=metadata.get("llm_model"),
                learning_status=LearningStatus.NONE,
                budget_snapshot=await self._budget_snapshot(account.id, ActionType.LIKE, now),
                audit_log=[],
                approved_at=now,
                expires_at=now + timedelta(hours=24),
            )
            self._append_audit(action, "created", now, detail="like action created")
            self._append_audit(action, "auto_approved", now, detail="likes skip approval")
            session.add(action)
            await self._log_operation(
                session,
                account_id=account.id,
                status=OperationStatus.SUCCESS,
                message="like action created",
                metadata={
                    "action_type": ActionType.LIKE,
                    "target_tweet_id": payload.target_tweet_id,
                    "fetched_tweet_id": payload.fetched_tweet_id,
                },
            )
            await session.commit()
            await session.refresh(action)

        await self.coordinator.enqueue_action(action.id)
        return self._to_view(action)

    async def create_follow_request(
        self,
        payload: FollowActionCreateRequest,
    ) -> ActionRequestView:
        account = await self._get_account(payload.account_id)
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            reusable = await self._find_existing_action(
                session,
                account_id=account.id,
                action_type=ActionType.FOLLOW,
                target_tweet_id=None,
                target_user_handle=payload.target_user_handle,
                fetched_tweet_id=payload.fetched_tweet_id,
                reusable_statuses={
                    ActionStatus.APPROVED,
                    ActionStatus.EXECUTING,
                    ActionStatus.SUCCEEDED,
                },
            )
            if reusable is not None:
                if reusable.status == ActionStatus.APPROVED:
                    await self.coordinator.enqueue_action(reusable.id)
                return self._to_view(reusable)

            action = ActionRequest(
                account_id=account.id,
                action_type=ActionType.FOLLOW,
                status=ActionStatus.APPROVED,
                trigger_source=payload.trigger_source,
                requested_execution_mode=account.execution_mode,
                fetched_tweet_id=payload.fetched_tweet_id,
                target_user_handle=payload.target_user_handle,
                learning_status=LearningStatus.NONE,
                budget_snapshot=await self._budget_snapshot(account.id, ActionType.FOLLOW, now),
                audit_log=[],
                approved_at=now,
                expires_at=now + timedelta(hours=payload.expires_in_hours),
            )
            self._append_audit(action, "created", now, detail="follow action created")
            self._append_audit(action, "auto_approved", now, detail="follow skips approval")
            session.add(action)
            await self._log_operation(
                session,
                account_id=account.id,
                status=OperationStatus.SUCCESS,
                message="follow action created",
                metadata={
                    "action_type": ActionType.FOLLOW,
                    "target_user_handle": payload.target_user_handle,
                    "fetched_tweet_id": payload.fetched_tweet_id,
                },
            )
            await session.commit()
            await session.refresh(action)

        await self.coordinator.enqueue_action(action.id)
        return self._to_view(action)

    async def approve_action(
        self,
        action_id: int,
        decision: ActionDecisionRequest,
    ) -> ActionRequestView:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            action = await self._get_action(session, action_id)
            if action.status != ActionStatus.PENDING_APPROVAL:
                raise ValueError("action is not pending approval")
            expires_at = self._as_utc(action.expires_at)
            if expires_at and expires_at <= now:
                action.status = ActionStatus.EXPIRED
                self._append_audit(
                    action,
                    "expired",
                    now,
                    detail="approval expired before approval",
                )
            else:
                action.status = ActionStatus.APPROVED
                action.approved_at = now
                if not action.final_draft:
                    action.final_draft = (
                        action.edited_draft or action.ai_draft or action.content_draft
                    )
                self._append_audit(
                    action,
                    "approved",
                    now,
                    detail=decision.reason or "approved from admin console",
                )
            await self._log_operation(
                session,
                account_id=action.account_id,
                status=OperationStatus.SUCCESS,
                message=f"action {action.status}",
                metadata={"action_id": action.id, "status": action.status},
            )
            await session.commit()
            await session.refresh(action)

        if action.status == ActionStatus.APPROVED:
            await self.coordinator.enqueue_action(action.id)
        return self._to_view(action)

    async def reject_action(
        self,
        action_id: int,
        decision: ActionDecisionRequest,
    ) -> ActionRequestView:
        return await self.skip_action(action_id, decision)

    async def skip_action(
        self,
        action_id: int,
        decision: ActionDecisionRequest,
    ) -> ActionRequestView:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            action = await self._get_action(session, action_id)
            if action.status != ActionStatus.PENDING_APPROVAL:
                raise ValueError("action is not pending approval")
            action.status = ActionStatus.REJECTED
            action.rejected_at = now
            self._append_audit(
                action,
                "skipped",
                now,
                detail=decision.reason or "skipped from admin console",
            )
            await self._log_operation(
                session,
                account_id=action.account_id,
                status=OperationStatus.SUCCESS,
                message="action skipped",
                metadata={"action_id": action.id, "status": action.status},
            )
            await session.commit()
            await session.refresh(action)
        return self._to_view(action)

    async def modify_action(
        self,
        action_id: int,
        payload: ActionModifyRequest,
    ) -> ActionRequestView:
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            action = await self._get_action(session, action_id)
            if action.action_type != ActionType.REPLY:
                raise ValueError("only reply actions can be modified")
            if action.status != ActionStatus.PENDING_APPROVAL:
                raise ValueError("action is not pending approval")

            action.status = ActionStatus.APPROVED
            action.approved_at = now
            action.edited_draft = payload.final_draft
            action.final_draft = payload.final_draft
            action.learning_status = LearningStatus.PENDING
            self._append_audit(
                action,
                "modified",
                now,
                detail=payload.reason or "reply edited before approval",
            )
            self._append_audit(action, "approved", now, detail="approved after edit")
            await self._log_operation(
                session,
                account_id=action.account_id,
                status=OperationStatus.SUCCESS,
                message="reply modified and approved",
                metadata={"action_id": action.id},
            )
            await session.commit()
            await session.refresh(action)

        await self.coordinator.enqueue_action(action.id)
        return self._to_view(action)

    async def process_action(self, action_id: int) -> ActionRequestView | None:
        async with self.session_factory() as session:
            try:
                action = await self._get_action(session, action_id)
            except KeyError:
                return None
            if action.status != ActionStatus.APPROVED:
                return self._to_view(action)
            account = await self.registry.get(action.account_id)
            state = await session.get(AccountRuntimeState, action.account_id)
            if account is None or state is None:
                await self._fail_action(
                    session,
                    action,
                    error_code=ActionErrorCode.ACCOUNT_PAUSED,
                    message="account is not available",
                )
                return self._to_view(action)

        token = await self.coordinator.acquire_account_lock(action.account_id)
        if token is None:
            await self.coordinator.enqueue_action(action_id)
            return None

        try:
            async with self.session_factory() as session:
                try:
                    action = await self._get_action(session, action_id)
                except KeyError:
                    return None
                state = await session.get(AccountRuntimeState, action.account_id)
                account = await self.registry.get(action.account_id)
                now = datetime.now(UTC)

                if action.status != ActionStatus.APPROVED:
                    return self._to_view(action)
                if account is None:
                    await self._fail_action(
                        session,
                        action,
                        error_code=ActionErrorCode.ACCOUNT_PAUSED,
                        message="account is not available",
                    )
                    return self._to_view(action)
                expires_at = self._as_utc(action.expires_at)
                if expires_at and expires_at <= now:
                    action.status = ActionStatus.EXPIRED
                    self._append_audit(
                        action,
                        "expired",
                        now,
                        detail="action expired before execution",
                    )
                    await session.commit()
                    await session.refresh(action)
                    return self._to_view(action)
                if state is not None and state.pause_reason == PauseReason.ADMIN_DISABLED:
                    await self._fail_action(
                        session,
                        action,
                        error_code=ActionErrorCode.ADMIN_DISABLED,
                        message="account is admin disabled",
                    )
                    return self._to_view(action)
                if state is None or state.lifecycle_status != AccountLifecycleStatus.ENABLED:
                    await self._fail_action(
                        session,
                        action,
                        error_code=ActionErrorCode.ACCOUNT_PAUSED,
                        message="account is paused",
                    )
                    return self._to_view(action)

                budget_decision = await self._evaluate_budget(account, action.action_type, now)
                action.budget_snapshot = budget_decision.snapshot
                if not budget_decision.allowed:
                    await self._fail_action(
                        session,
                        action,
                        error_code=budget_decision.error_code,
                        message=budget_decision.message or "budget blocked action",
                    )
                    return self._to_view(action)

                if account.execution_mode == ExecutionMode.READ_ONLY:
                    await self._fail_action(
                        session,
                        action,
                        error_code=ActionErrorCode.EXECUTION_MODE_BLOCKED,
                        message="account is read_only",
                    )
                    return self._to_view(action)

                applied_mode = (
                    ExecutionMode.LIVE
                    if account.execution_mode == ExecutionMode.LIVE
                    else ExecutionMode.DRY_RUN
                )
                action.status = ActionStatus.EXECUTING
                action.applied_execution_mode = applied_mode
                if action.edited_draft:
                    action.learning_status = LearningStatus.PENDING
                self._append_audit(
                    action,
                    "executing",
                    now,
                    detail=f"action executing in {applied_mode.value} mode",
                )
                await session.commit()

                try:
                    execution_detail = await self._execute_action(account, action, applied_mode)
                except Exception as exc:
                    await self._fail_action(
                        session,
                        action,
                        error_code=ActionErrorCode.EXECUTION_FAILED,
                        message=str(exc) or "execution failed",
                    )
                    return self._to_view(action)

                cooldown_until = await self.coordinator.record_action(
                    action.account_id,
                    action.action_type,
                    now,
                    min_interval_minutes=account.behavior_budget.min_interval_minutes,
                )
                action.status = ActionStatus.SUCCEEDED
                action.executed_at = now
                action.error_code = None
                action.error_message = None
                self._append_audit(
                    action,
                    "succeeded",
                    now,
                    detail=execution_detail,
                    cooldown_until=cooldown_until.isoformat(),
                )

                await self._log_operation(
                    session,
                    account_id=action.account_id,
                    status=OperationStatus.SUCCESS,
                    message=f"action executed in {applied_mode.value} mode",
                    metadata={"action_id": action.id, "action_type": action.action_type},
                )
                await session.commit()

                if action.action_type == ActionType.REPLY and action.edited_draft:
                    try:
                        await self.writing_guide_service.apply_learning(account, action.id)
                        action.learning_status = LearningStatus.APPLIED
                        action.learning_applied_at = datetime.now(UTC)
                        self._append_audit(
                            action,
                            "learning_applied",
                            action.learning_applied_at,
                            detail="writing guide updated",
                        )
                    except Exception as exc:
                        action.learning_status = LearningStatus.FAILED
                        self._append_audit(
                            action,
                            "learning_failed",
                            datetime.now(UTC),
                            detail=str(exc),
                        )
                    await session.commit()
                else:
                    action.learning_status = LearningStatus.NONE
                    await session.commit()

                await session.refresh(action)
                return self._to_view(action)
        finally:
            await self.coordinator.release_account_lock(action.account_id, token)

    async def _execute_action(
        self,
        account,
        action: ActionRequest,
        applied_mode: ExecutionMode,
    ) -> str:
        if applied_mode == ExecutionMode.DRY_RUN:
            return "dry_run succeeded"

        if action.action_type == ActionType.LIKE:
            await self.action_executor.like(account, tweet_id=action.target_tweet_id or "")
            return "live like succeeded"

        if action.action_type == ActionType.REPLY:
            text = (
                action.final_draft
                or action.edited_draft
                or action.ai_draft
                or action.content_draft
            )
            if not text:
                raise ValueError("reply text is missing")
            await self.action_executor.reply(
                account,
                tweet_id=action.target_tweet_id or "",
                text=text,
            )
            return "live reply succeeded"

        if action.action_type == ActionType.FOLLOW:
            if not action.target_user_handle:
                raise ValueError("follow target handle is missing")
            await self.action_executor.follow(
                account,
                user_handle=action.target_user_handle,
            )
            return "live follow succeeded"

        raise ValueError(f"unsupported action type: {action.action_type}")

    async def _evaluate_budget(
        self,
        account,
        action_type: str,
        now: datetime,
    ) -> BudgetDecision:
        budget = account.behavior_budget
        start_hour, end_hour = budget.active_hours
        current_hour = now.astimezone(self.settings.timezone).hour
        if current_hour < start_hour or current_hour > end_hour:
            return BudgetDecision(
                allowed=False,
                error_code=ActionErrorCode.OUTSIDE_ACTIVE_HOURS,
                message="outside active hours",
                snapshot={
                    "active_hours": [start_hour, end_hour],
                    "evaluated_at": now.isoformat(),
                    "current_hour": current_hour,
                },
            )

        cooldown_until = await self.coordinator.get_action_cooldown_until(account.id)
        if cooldown_until and cooldown_until > now:
            return BudgetDecision(
                allowed=False,
                error_code=ActionErrorCode.INTERVAL_NOT_ELAPSED,
                message="minimum interval has not elapsed",
                snapshot={
                    "evaluated_at": now.isoformat(),
                    "cooldown_until": cooldown_until.isoformat(),
                    "min_interval_minutes": budget.min_interval_minutes,
                },
            )

        limit = self._limit_for_action(account, action_type)
        used = await self.coordinator.get_daily_action_count(account.id, action_type, now)
        snapshot = {
            "evaluated_at": now.isoformat(),
            "daily_limit": limit,
            "daily_used": used,
            "active_hours": list(budget.active_hours),
            "min_interval_minutes": budget.min_interval_minutes,
            "action_type": action_type,
        }
        if used >= limit:
            return BudgetDecision(
                allowed=False,
                error_code=ActionErrorCode.BUDGET_EXCEEDED,
                message="daily budget exceeded",
                snapshot=snapshot,
            )

        return BudgetDecision(allowed=True, error_code=None, message=None, snapshot=snapshot)

    async def _budget_snapshot(
        self,
        account_id: str,
        action_type: ActionType,
        now: datetime,
    ) -> dict[str, Any]:
        used = await self.coordinator.get_daily_action_count(account_id, action_type, now)
        return {
            "created_at": now.isoformat(),
            "action_type": action_type,
            "daily_used": used,
        }

    async def _fail_action(
        self,
        session: AsyncSession,
        action: ActionRequest,
        *,
        error_code: ActionErrorCode | None,
        message: str,
    ) -> None:
        now = datetime.now(UTC)
        action.status = ActionStatus.FAILED
        action.error_code = error_code
        action.error_message = message
        self._append_audit(action, "failed", now, detail=message, error_code=error_code)
        await self._log_operation(
            session,
            account_id=action.account_id,
            status=OperationStatus.FAILED,
            message=message,
            error_code=error_code,
            metadata={"action_id": action.id, "action_type": action.action_type},
        )
        await session.commit()
        await session.refresh(action)

    async def _get_account(self, account_id: str):
        account = await self.registry.get(account_id)
        if account is None:
            raise KeyError(account_id)
        return account

    async def _get_action(self, session: AsyncSession, action_id: int) -> ActionRequest:
        action = await session.get(ActionRequest, action_id)
        if action is None:
            raise KeyError(action_id)
        return action

    async def _find_reusable_decision(
        self,
        session: AsyncSession,
        *,
        canonical_tweet_id: str,
        exclude_fetched_tweet_id: int | None,
    ) -> tuple[DecisionResult, str | None, int | None] | None:
        query = (
            select(AILogRecord, FetchedTweet.account_id, FetchedTweet.id)
            .join(FetchedTweet, FetchedTweet.id == AILogRecord.fetched_tweet_id)
            .where(
                AILogRecord.log_type == "decision",
                AILogRecord.status == "success",
                FetchedTweet.tweet_id == canonical_tweet_id,
            )
            .order_by(AILogRecord.created_at.desc(), AILogRecord.id.desc())
        )
        if exclude_fetched_tweet_id is not None:
            query = query.where(FetchedTweet.id != exclude_fetched_tweet_id)
        row = (await session.execute(query.limit(1))).first()
        if row is None:
            return None
        log, joined_account_id, joined_fetched_tweet_id = row
        payload = log.response_payload or {}
        raw_result = payload.get("result")
        if not isinstance(raw_result, dict):
            return None
        decision = DecisionResult.model_validate(raw_result)
        source_account_id = payload.get("source_account_id") or joined_account_id
        source_fetched_tweet_id = payload.get("source_fetched_tweet_id") or joined_fetched_tweet_id
        return decision, source_account_id, source_fetched_tweet_id

    async def _record_reused_decision(
        self,
        *,
        account_id: str,
        fetched_tweet_id: int,
        canonical_tweet_id: str,
        source_account_id: str | None,
        source_fetched_tweet_id: int | None,
        decision: DecisionResult,
    ) -> None:
        async with self.session_factory() as session:
            session.add(
                AILogRecord(
                    account_id=account_id,
                    fetched_tweet_id=fetched_tweet_id,
                    log_type="decision",
                    status="success",
                    provider=self.settings.llm_provider.value,
                    model_id=self.settings.llm_model_id,
                    request_payload={
                        "canonical_tweet_id": canonical_tweet_id,
                        "reused": True,
                    },
                    response_payload={
                        "reused": True,
                        "source_account_id": source_account_id,
                        "source_fetched_tweet_id": source_fetched_tweet_id,
                        "result": decision.model_dump(mode="json"),
                    },
                )
            )
            await session.commit()

    async def _log_operation(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        status: OperationStatus,
        message: str,
        error_code: ActionErrorCode | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            OperationLog(
                account_id=account_id,
                operation_type=OperationType.ACTION,
                status=status,
                error_code=error_code,
                message=message,
                metadata_json=metadata,
            )
        )

    async def _find_existing_action(
        self,
        session: AsyncSession,
        *,
        account_id: str,
        action_type: ActionType,
        target_tweet_id: str | None,
        target_user_handle: str | None = None,
        fetched_tweet_id: int | None,
        reusable_statuses: set[ActionStatus],
    ) -> ActionRequest | None:
        query = select(ActionRequest).where(
            ActionRequest.account_id == account_id,
            ActionRequest.action_type == action_type,
        )
        if target_tweet_id is not None:
            query = query.where(ActionRequest.target_tweet_id == target_tweet_id)
        if target_user_handle is not None:
            query = query.where(ActionRequest.target_user_handle == target_user_handle)
        query = query.order_by(ActionRequest.created_at.desc(), ActionRequest.id.desc())
        if fetched_tweet_id is not None:
            query = query.where(ActionRequest.fetched_tweet_id == fetched_tweet_id)

        actions = (await session.execute(query)).scalars().all()
        allowed_statuses = {status.value for status in reusable_statuses}
        for action in actions:
            if action.status in allowed_statuses:
                return action
        return None

    def _append_audit(
        self,
        action: ActionRequest,
        event: str,
        at: datetime,
        **payload: Any,
    ) -> None:
        existing = list(action.audit_log or [])
        existing.append({"event": event, "at": at.isoformat(), **payload})
        action.audit_log = existing

    def _limit_for_action(self, account, action_type: str) -> int:
        budget = account.behavior_budget
        if action_type == ActionType.LIKE:
            return budget.daily_likes_max
        if action_type == ActionType.REPLY:
            return budget.daily_replies_max
        return budget.daily_follows_max

    def _to_view(self, action: ActionRequest) -> ActionRequestView:
        return ActionRequestView(
            id=action.id,
            account_id=action.account_id,
            action_type=ActionType(action.action_type),
            status=ActionStatus(action.status),
            trigger_source=action.trigger_source,
            requested_execution_mode=ExecutionMode(action.requested_execution_mode),
            applied_execution_mode=(
                ExecutionMode(action.applied_execution_mode)
                if action.applied_execution_mode
                else None
            ),
            fetched_tweet_id=action.fetched_tweet_id,
            target_tweet_id=action.target_tweet_id,
            target_user_handle=action.target_user_handle,
            content_draft=action.content_draft,
            ai_draft=action.ai_draft,
            edited_draft=action.edited_draft,
            final_draft=action.final_draft,
            relevance_score=action.relevance_score,
            reply_confidence=action.reply_confidence,
            llm_provider=action.llm_provider,
            llm_model=action.llm_model,
            learning_status=LearningStatus(action.learning_status),
            learning_applied_at=self._as_utc(action.learning_applied_at),
            budget_snapshot=action.budget_snapshot,
            audit_log=list(action.audit_log or []),
            error_code=action.error_code,
            error_message=action.error_message,
            approved_at=self._as_utc(action.approved_at),
            rejected_at=self._as_utc(action.rejected_at),
            executed_at=self._as_utc(action.executed_at),
            expires_at=self._as_utc(action.expires_at),
            created_at=self._as_utc(action.created_at),
            updated_at=self._as_utc(action.updated_at),
        )

    def _as_utc(self, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
