from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevance_score: int = Field(ge=0, le=10)
    like: bool
    reply_draft: str | None = None
    reply_confidence: int = Field(ge=0, le=10)
    rationale: str | None = None

    @field_validator("reply_draft")
    @classmethod
    def normalize_reply_draft(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class GuideRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice: str
    dos: list[str]
    donts: list[str]


class PromptTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=12000)

    @field_validator("prompt")
    @classmethod
    def normalize_prompt(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("prompt cannot be empty")
        return cleaned


class PromptTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model_id: str | None = None
    content: str


class PromptTemplateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_system_template: str = Field(min_length=1)
    decision_user_template: str = Field(min_length=1)
    learning_system_template: str = Field(min_length=1)
    learning_user_template: str = Field(min_length=1)

    @field_validator(
        "decision_system_template",
        "decision_user_template",
        "learning_system_template",
        "learning_user_template",
    )
    @classmethod
    def normalize_template(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("template cannot be empty")
        return cleaned


class PromptTemplateView(PromptTemplateConfig):
    config_file: str


class PromptTemplateUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompts: PromptTemplateView


class AILogListItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    created_at: datetime
    account_id: str | None = None
    log_type: str
    status: str
    provider: str | None = None
    model_id: str | None = None
    request_preview: str
    response_preview: str | None = None
    error_message: str | None = None


class AILogDetailView(AILogListItem):
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None


class AILogSummaryView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_hours: int
    total_logs: int
    decision_success_count: int
    decision_failed_count: int
    learning_count: int
    prompt_test_count: int
    auto_scored_tweets: int
    auto_score_failed_tweets: int
    auto_score_skipped_ai_disabled: int
    filtered_replies_count: int
    filtered_retweets_count: int
    latest_auto_score_batch_at: datetime | None = None
    latest_auto_score_batch_scored: int | None = None
    latest_auto_score_batch_failed: int | None = None
