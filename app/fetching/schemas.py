from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.runtime.enums import FetchErrorCode


class NormalizedTweet(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tweet_id: str
    author_handle: str | None = None
    text: str
    lang: str | None = None
    is_reply: bool = False
    is_retweet: bool = False
    view_count: int | None = None
    like_count: int | None = None
    retweet_count: int | None = None
    reply_count: int | None = None
    created_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    raw_payload: dict[str, Any] | None = None


class FetchBatchResult(BaseModel):
    items: list[NormalizedTweet]
    next_cursor: str | None = None


class SessionValidationResult(BaseModel):
    ok: bool
    detail: str | None = None
    error_code: FetchErrorCode | None = None


class AccountProfileSnapshot(BaseModel):
    follower_count: int | None = None
    twitter_handle: str | None = None


class FetchRunResponse(BaseModel):
    account_id: str
    trigger: str
    status: str
    sources_processed: int = 0
    tweets_inserted: int = 0
    skipped_reason: str | None = None
    error_code: FetchErrorCode | None = None
    error_message: str | None = None
