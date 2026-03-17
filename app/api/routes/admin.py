from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.accounts.schemas import (
    AccountAdminView,
    AccountConfigDocumentView,
    AccountConfigEditView,
    AccountGroupDeleteResponse,
    AccountGroupDocumentView,
    AccountGroupEditView,
    AccountGroupListItem,
    AccountGroupUpdateResult,
    AccountExecutionModeUpdateRequest,
    AccountExecutionModeUpdateResult,
    AccountConfigUpdateResult,
    AccountDeleteResponse,
    AccountStateChangeResponse,
    CookieImportCandidateView,
    CookieImportRequest,
    CookieImportResult,
    DashboardView,
    FetchEnqueueResponse,
    FollowTargetUpdateResponse,
    ReloadSummary,
    RuntimeSettingsUpdateRequest,
    RuntimeSettingsUpdateResult,
    RuntimeSettingsView,
    TweetBackfillResult,
    TweetClearResult,
    TweetCleanupResult,
    TweetDetailView,
    TweetListItem,
    TweetMaintenanceRequest,
)
from app.actions.schemas import (
    ActionDecisionRequest,
    ActionModifyRequest,
    ActionRequestView,
    FollowActionCreateRequest,
    GenerateReplyRequest,
    LikeActionCreateRequest,
    ReplyApprovalCreateRequest,
    ReplyWorkspaceItem,
    TweetGenerationView,
)
from app.api.deps import get_container
from app.fetching.schemas import SessionValidationResult
from app.llm.schemas import (
    AILogDetailView,
    AILogListItem,
    AILogSummaryView,
    PromptTemplateConfig,
    PromptTemplateUpdateResult,
    PromptTemplateView,
    PromptTestRequest,
    PromptTestResponse,
)
from app.runtime.container import ServiceContainer
from app.runtime.enums import AccountLifecycleStatus, ActionStatus, ExecutionMode, SourceType

router = APIRouter(tags=["admin"])
ContainerDep = Annotated[ServiceContainer, Depends(get_container)]
ActionStatusFilter = Annotated[ActionStatus | None, Query(alias="status")]


@router.get("/accounts", response_model=list[AccountAdminView])
async def list_accounts(container: ContainerDep) -> list[AccountAdminView]:
    return await container.account_service.list_accounts(
        fetch_limit_default=container.settings.fetch_limit_default
    )


@router.get("/groups", response_model=list[AccountGroupListItem])
async def list_groups(container: ContainerDep) -> list[AccountGroupListItem]:
    return await container.account_service.list_groups()


@router.post(
    "/groups",
    response_model=AccountGroupUpdateResult,
    status_code=status.HTTP_201_CREATED,
)
async def create_group(
    payload: AccountGroupEditView,
    container: ContainerDep,
) -> AccountGroupUpdateResult:
    try:
        return await container.account_service.create_group_config(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/groups/{group_id}", response_model=AccountGroupDocumentView)
async def get_group_config(
    group_id: str,
    container: ContainerDep,
) -> AccountGroupDocumentView:
    try:
        return await container.account_service.get_group_config(group_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="group not found",
        ) from exc


@router.put("/groups/{group_id}", response_model=AccountGroupUpdateResult)
async def update_group_config(
    group_id: str,
    payload: AccountGroupEditView,
    container: ContainerDep,
) -> AccountGroupUpdateResult:
    try:
        return await container.account_service.update_group_config(group_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="group not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.delete("/groups/{group_id}", response_model=AccountGroupDeleteResponse)
async def delete_group_config(
    group_id: str,
    container: ContainerDep,
) -> AccountGroupDeleteResponse:
    try:
        return await container.account_service.delete_group_config(group_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="group not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/cookie-import/candidates", response_model=list[CookieImportCandidateView])
async def list_cookie_import_candidates(container: ContainerDep) -> list[CookieImportCandidateView]:
    return await container.account_service.list_cookie_import_candidates()


@router.post(
    "/cookie-import/accounts",
    response_model=CookieImportResult,
    status_code=status.HTTP_201_CREATED,
)
async def import_account_from_cookie(
    payload: CookieImportRequest,
    container: ContainerDep,
) -> CookieImportResult:
    try:
        return await container.account_service.import_account_from_cookie(
            payload,
            validate_session=True,
            validate_session_func=container.fetch_service.validate_session,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/accounts/{account_id}/config", response_model=AccountConfigDocumentView)
async def get_account_config(
    account_id: str,
    container: ContainerDep,
) -> AccountConfigDocumentView:
    try:
        return await container.account_service.get_account_config(account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.put("/accounts/{account_id}/config", response_model=AccountConfigUpdateResult)
async def update_account_config(
    account_id: str,
    payload: AccountConfigEditView,
    container: ContainerDep,
) -> AccountConfigUpdateResult:
    try:
        return await container.account_service.update_account_config(account_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.put(
    "/accounts/{account_id}/execution-mode",
    response_model=AccountExecutionModeUpdateResult,
)
async def update_account_execution_mode(
    account_id: str,
    payload: AccountExecutionModeUpdateRequest,
    container: ContainerDep,
) -> AccountExecutionModeUpdateResult:
    try:
        return await container.account_service.update_account_execution_mode(
            account_id,
            payload.execution_mode,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.delete("/accounts/{account_id}", response_model=AccountDeleteResponse)
async def delete_account(
    account_id: str,
    container: ContainerDep,
) -> AccountDeleteResponse:
    try:
        return await container.account_service.delete_account(account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.get("/dashboard", response_model=DashboardView)
async def get_dashboard(container: ContainerDep) -> DashboardView:
    return await container.account_service.get_dashboard(
        fetch_limit_default=container.settings.fetch_limit_default
    )


@router.get("/runtime-settings", response_model=RuntimeSettingsView)
async def get_runtime_settings(container: ContainerDep) -> RuntimeSettingsView:
    return container.account_service.get_runtime_settings()


@router.put("/runtime-settings", response_model=RuntimeSettingsUpdateResult)
async def update_runtime_settings(
    payload: RuntimeSettingsUpdateRequest,
    container: ContainerDep,
) -> RuntimeSettingsUpdateResult:
    was_enabled = container.settings.ai_enabled
    result = await container.account_service.update_runtime_settings(payload)
    if payload.ai_enabled and not was_enabled:
        result.auto_scored_tweets = await container.fetch_service.score_existing_unscored_tweets()
    return result


@router.post("/runtime-settings/test", response_model=PromptTestResponse)
async def test_runtime_settings_prompt(
    payload: PromptTestRequest,
    container: ContainerDep,
) -> PromptTestResponse:
    try:
        return await container.llm_service.test_prompt(payload.prompt)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/runtime-prompts", response_model=PromptTemplateView)
async def get_runtime_prompts(container: ContainerDep) -> PromptTemplateView:
    return container.llm_service.get_prompt_templates()


@router.put("/runtime-prompts", response_model=PromptTemplateUpdateResult)
async def update_runtime_prompts(
    payload: PromptTemplateConfig,
    container: ContainerDep,
) -> PromptTemplateUpdateResult:
    try:
        return container.llm_service.update_prompt_templates(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/ai-logs", response_model=list[AILogListItem])
async def list_ai_logs(
    container: ContainerDep,
    account_id: str | None = None,
    log_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=300),
) -> list[AILogListItem]:
    return await container.llm_service.list_logs(
        account_id=account_id,
        log_type=log_type,
        limit=limit,
    )


@router.get("/ai-logs/summary", response_model=AILogSummaryView)
async def get_ai_log_summary(
    container: ContainerDep,
    account_id: str | None = None,
) -> AILogSummaryView:
    return await container.llm_service.get_log_summary(account_id=account_id)


@router.get("/ai-logs/{log_id}", response_model=AILogDetailView)
async def get_ai_log(
    log_id: int,
    container: ContainerDep,
) -> AILogDetailView:
    try:
        return await container.llm_service.get_log(log_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ai log not found",
        ) from exc


@router.get("/tweets", response_model=list[TweetListItem])
async def list_tweets(
    container: ContainerDep,
    account_id: str | None = None,
    source_type: SourceType | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[TweetListItem]:
    return await container.account_service.list_tweets(
        account_id=account_id,
        source_type=source_type,
        limit=limit,
    )


@router.post("/tweets/cleanup", response_model=TweetCleanupResult)
async def cleanup_tweets(
    payload: TweetMaintenanceRequest,
    container: ContainerDep,
) -> TweetCleanupResult:
    try:
        return await container.account_service.cleanup_stale_tweets(
            account_id=payload.account_id
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post("/tweets/clear", response_model=TweetClearResult)
async def clear_tweets(
    payload: TweetMaintenanceRequest,
    container: ContainerDep,
) -> TweetClearResult:
    try:
        return await container.account_service.clear_tweets(account_id=payload.account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post("/tweets/backfill-ai", response_model=TweetBackfillResult)
async def backfill_tweet_ai(
    payload: TweetMaintenanceRequest,
    container: ContainerDep,
) -> TweetBackfillResult:
    try:
        return await container.fetch_service.backfill_recent_unscored_tweets(
            account_id=payload.account_id,
            limit_per_account=max(container.settings.fetch_limit_default, 50),
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.get("/tweets/{tweet_record_id}", response_model=TweetDetailView)
async def get_tweet_detail(
    tweet_record_id: int,
    container: ContainerDep,
) -> TweetDetailView:
    try:
        return await container.account_service.get_tweet_detail(tweet_record_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="tweet not found",
        ) from exc


@router.post(
    "/tweets/{tweet_record_id}/follow-targets/{account_id}",
    response_model=FollowTargetUpdateResponse,
)
async def add_follow_target_for_tweet_author(
    tweet_record_id: int,
    account_id: str,
    container: ContainerDep,
) -> FollowTargetUpdateResponse:
    try:
        tweet = await container.account_service.get_tweet_detail(tweet_record_id)
        author_handle = tweet.author_handle
        if not author_handle:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tweet author handle is missing",
            )

        updated_config, added = await container.account_service.ensure_follow_target(
            account_id,
            author_handle,
            default_count=container.settings.fetch_limit_default,
        )
        scope_owner_type = "group" if updated_config.account.group_id else "account"
        scope_owner_id = updated_config.account.group_id or updated_config.account.id
        scope_owner_label = (
            updated_config.group_name
            or updated_config.account.group_id
            or updated_config.account.id
        )

        fetch_enqueued = False
        detail = (
            f"author already in {scope_owner_type} follow scope"
        )
        if added:
            enqueue_result = await container.fetch_service.enqueue_fetch(account_id)
            fetch_enqueued = enqueue_result.enqueued
            detail = f"author added to {scope_owner_type} follow scope"
            if (
                updated_config.account.execution_mode != ExecutionMode.READ_ONLY
                and updated_config.account.enabled
                and updated_config.lifecycle_status == AccountLifecycleStatus.ENABLED
            ):
                await container.action_service.create_follow_request(
                    FollowActionCreateRequest(
                        account_id=account_id,
                        target_user_handle=author_handle,
                        trigger_source="tweet_workspace_follow",
                        fetched_tweet_id=tweet_record_id,
                    )
                )
                detail = (
                    f"author added to {scope_owner_type} follow scope and follow action queued"
                    if fetch_enqueued
                    else f"author added to {scope_owner_type} follow scope and follow action created"
                )
            elif fetch_enqueued:
                detail = f"author added to {scope_owner_type} follow scope; next fetch is queued"

        return FollowTargetUpdateResponse(
            tweet_record_id=tweet_record_id,
            target_account_id=account_id,
            author_handle=author_handle,
            scope_owner_type=scope_owner_type,
            scope_owner_id=scope_owner_id,
            scope_owner_label=scope_owner_label,
            added_to_follow_scope=added,
            fetch_enqueued=fetch_enqueued,
            detail=detail,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="tweet or account not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post(
    "/tweets/{tweet_record_id}/reply/generate",
    response_model=TweetGenerationView,
)
async def generate_reply_for_tweet(
    tweet_record_id: int,
    payload: GenerateReplyRequest,
    container: ContainerDep,
) -> TweetGenerationView:
    try:
        return await container.action_service.generate_reply_for_tweet(tweet_record_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="tweet or account not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("/approvals", response_model=list[ActionRequestView])
async def list_approvals(
    container: ContainerDep,
    account_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ActionRequestView]:
    return await container.action_service.list_approvals(
        account_id=account_id,
        limit=limit,
    )


@router.get("/actions", response_model=list[ActionRequestView])
async def list_actions(
    container: ContainerDep,
    account_id: str | None = None,
    status_filter: ActionStatusFilter = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ActionRequestView]:
    return await container.action_service.list_actions(
        account_id=account_id,
        status=status_filter,
        limit=limit,
    )


@router.get("/reply-workspace", response_model=list[ReplyWorkspaceItem])
async def list_reply_workspace(
    container: ContainerDep,
    account_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> list[ReplyWorkspaceItem]:
    return await container.action_service.list_reply_workspace(
        account_id=account_id,
        limit=limit,
    )


@router.post(
    "/actions/replies",
    response_model=ActionRequestView,
    status_code=status.HTTP_201_CREATED,
)
async def create_reply_request(
    payload: ReplyApprovalCreateRequest,
    container: ContainerDep,
) -> ActionRequestView:
    try:
        return await container.action_service.create_reply_request(payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post(
    "/actions/likes",
    response_model=ActionRequestView,
    status_code=status.HTTP_201_CREATED,
)
async def create_like_request(
    payload: LikeActionCreateRequest,
    container: ContainerDep,
) -> ActionRequestView:
    try:
        return await container.action_service.create_like_request(payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post("/actions/{action_id}/approve", response_model=ActionRequestView)
async def approve_action(
    action_id: int,
    payload: ActionDecisionRequest,
    container: ContainerDep,
) -> ActionRequestView:
    try:
        return await container.action_service.approve_action(action_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="action not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post("/actions/{action_id}/modify", response_model=ActionRequestView)
async def modify_action(
    action_id: int,
    payload: ActionModifyRequest,
    container: ContainerDep,
) -> ActionRequestView:
    try:
        return await container.action_service.modify_action(action_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="action not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post("/actions/{action_id}/skip", response_model=ActionRequestView)
async def skip_action(
    action_id: int,
    payload: ActionDecisionRequest,
    container: ContainerDep,
) -> ActionRequestView:
    try:
        return await container.action_service.skip_action(action_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="action not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc


@router.post("/approvals/{action_id}/approve", response_model=ActionRequestView)
async def approve_action_legacy(
    action_id: int,
    payload: ActionDecisionRequest,
    container: ContainerDep,
) -> ActionRequestView:
    return await approve_action(action_id, payload, container)


@router.post("/approvals/{action_id}/reject", response_model=ActionRequestView)
async def reject_action_legacy(
    action_id: int,
    payload: ActionDecisionRequest,
    container: ContainerDep,
) -> ActionRequestView:
    return await skip_action(action_id, payload, container)


@router.post(
    "/accounts/{account_id}/disable",
    response_model=AccountStateChangeResponse,
)
async def disable_account(
    account_id: str,
    container: ContainerDep,
) -> AccountStateChangeResponse:
    try:
        return await container.account_service.disable_account(account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post(
    "/accounts/{account_id}/enable",
    response_model=AccountStateChangeResponse,
)
async def enable_account(
    account_id: str,
    container: ContainerDep,
) -> AccountStateChangeResponse:
    try:
        return await container.account_service.enable_account(account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post("/config/reload", response_model=ReloadSummary)
async def reload_configs(container: ContainerDep) -> ReloadSummary:
    return await container.account_service.reload_configs()


@router.post(
    "/accounts/{account_id}/validate-session",
    response_model=SessionValidationResult,
)
async def validate_session(
    account_id: str,
    container: ContainerDep,
) -> SessionValidationResult:
    try:
        return await container.fetch_service.validate_session(account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc


@router.post("/accounts/{account_id}/fetch-now", response_model=FetchEnqueueResponse)
async def fetch_now(
    account_id: str,
    container: ContainerDep,
) -> FetchEnqueueResponse:
    try:
        return await container.fetch_service.enqueue_fetch(account_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        ) from exc
