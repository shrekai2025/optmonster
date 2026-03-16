from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.runtime.enums import ActionStatus, ActionType, ExecutionMode, LearningStatus, LLMProvider


class ReplyApprovalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    target_tweet_id: str
    target_user_handle: str | None = None
    content_draft: str = Field(min_length=1)
    trigger_source: str = "console"
    expires_in_hours: int = Field(default=24, ge=1, le=168)
    fetched_tweet_id: int | None = None

    @field_validator("content_draft")
    @classmethod
    def validate_content_draft(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("content_draft cannot be empty")
        return cleaned


class LikeActionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    target_tweet_id: str
    target_user_handle: str | None = None
    trigger_source: str = "console"
    fetched_tweet_id: int | None = None


class FollowActionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    target_user_handle: str
    trigger_source: str = "console"
    expires_in_hours: int = Field(default=24, ge=1, le=168)
    fetched_tweet_id: int | None = None

    @field_validator("target_user_handle")
    @classmethod
    def normalize_target_user_handle(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("target_user_handle cannot be empty")
        return cleaned if cleaned.startswith("@") else f"@{cleaned}"


class ActionDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class ActionModifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_draft: str = Field(min_length=1)
    reason: str | None = None

    @field_validator("final_draft")
    @classmethod
    def validate_final_draft(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("final_draft cannot be empty")
        return cleaned


class TweetDecisionPreview(BaseModel):
    relevance_score: int
    like: bool
    reply_draft: str | None
    reply_confidence: int
    rationale: str | None = None


class GenerateReplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    trigger_source: str = "console"


class TweetGenerationView(BaseModel):
    tweet_record_id: int
    account_id: str
    decision: TweetDecisionPreview
    like_action: ActionRequestView | None
    reply_action: ActionRequestView | None


class ReplyWorkspaceItem(BaseModel):
    id: int
    account_id: str
    account_twitter_handle: str
    status: ActionStatus
    requested_execution_mode: ExecutionMode
    fetched_tweet_id: int | None
    target_tweet_id: str | None
    target_user_handle: str | None
    tweet_text: str
    tweet_author_handle: str | None
    tweet_url: str | None
    ai_draft: str | None
    edited_draft: str | None
    final_draft: str | None
    relevance_score: int | None
    reply_confidence: int | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class ActionRequestView(BaseModel):
    id: int
    account_id: str
    action_type: ActionType
    status: ActionStatus
    trigger_source: str
    requested_execution_mode: ExecutionMode
    applied_execution_mode: ExecutionMode | None
    fetched_tweet_id: int | None
    target_tweet_id: str | None
    target_user_handle: str | None
    content_draft: str | None
    ai_draft: str | None
    edited_draft: str | None
    final_draft: str | None
    relevance_score: int | None
    reply_confidence: int | None
    llm_provider: LLMProvider | None
    llm_model: str | None
    learning_status: LearningStatus
    learning_applied_at: datetime | None
    budget_snapshot: dict[str, Any] | None
    audit_log: list[dict[str, Any]]
    error_code: str | None
    error_message: str | None
    approved_at: datetime | None
    rejected_at: datetime | None
    executed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
