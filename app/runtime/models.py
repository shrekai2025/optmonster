from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Integer, MetaData, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.runtime.enums import (
    AccountLifecycleStatus,
    ActionStatus,
    ActionType,
    CookieFreshness,
    ExecutionMode,
    FetchErrorCode,
    LearningStatus,
    OperationStatus,
    OperationType,
    PauseReason,
    ProxyHealth,
    SourceType,
)

JSON_PAYLOAD = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AccountRuntimeState(TimestampMixin, Base):
    __tablename__ = "account_runtime_states"

    account_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    twitter_handle: Mapped[str] = mapped_column(String(100), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AccountLifecycleStatus.ENABLED,
    )
    config_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    last_auth_check: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cookie_freshness: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CookieFreshness.UNKNOWN,
    )
    proxy_health: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProxyHealth.UNKNOWN,
    )
    failure_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pause_reason: Mapped[str | None] = mapped_column(String(64), default=PauseReason.NONE)
    last_fetch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_fetch_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_fetch_not_before_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    execution_mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ExecutionMode.READ_ONLY,
    )


class FetchCursor(TimestampMixin, Base):
    __tablename__ = "fetch_cursors"
    __table_args__ = (
        UniqueConstraint("account_id", "source_type", "source_key", name="uq_fetch_cursor_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(255))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)


class FetchedTweet(TimestampMixin, Base):
    __tablename__ = "fetched_tweets"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "source_type",
            "source_key",
            "tweet_id",
            name="uq_fetched_tweet_dedupe",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    tweet_id: Mapped[str] = mapped_column(String(64), nullable=False)
    author_handle: Mapped[str | None] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str | None] = mapped_column(String(16))
    view_count: Mapped[int | None] = mapped_column(Integer)
    like_count: Mapped[int | None] = mapped_column(Integer)
    retweet_count: Mapped[int | None] = mapped_column(Integer)
    reply_count: Mapped[int | None] = mapped_column(Integer)
    created_at_twitter: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_PAYLOAD)


class AccountFollowerSnapshot(TimestampMixin, Base):
    __tablename__ = "account_follower_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "snapshot_date",
            name="uq_account_follower_snapshot_day",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    follower_count: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OperationLog(TimestampMixin, Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON_PAYLOAD)


class AILogRecord(TimestampMixin, Base):
    __tablename__ = "ai_log_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str | None] = mapped_column(String(100), index=True)
    fetched_tweet_id: Mapped[int | None] = mapped_column(Integer, index=True)
    log_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str | None] = mapped_column(String(64))
    model_id: Mapped[str | None] = mapped_column(String(200))
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_PAYLOAD)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON_PAYLOAD)
    error_message: Mapped[str | None] = mapped_column(Text)


class ActionRequest(TimestampMixin, Base):
    __tablename__ = "action_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ActionStatus.PENDING_APPROVAL,
        index=True,
    )
    trigger_source: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_execution_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    applied_execution_mode: Mapped[str | None] = mapped_column(String(32))
    fetched_tweet_id: Mapped[int | None] = mapped_column(Integer, index=True)
    target_tweet_id: Mapped[str | None] = mapped_column(String(64))
    target_user_handle: Mapped[str | None] = mapped_column(String(100))
    content_draft: Mapped[str | None] = mapped_column(Text)
    ai_draft: Mapped[str | None] = mapped_column(Text)
    edited_draft: Mapped[str | None] = mapped_column(Text)
    final_draft: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[int | None] = mapped_column(Integer)
    reply_confidence: Mapped[int | None] = mapped_column(Integer)
    llm_provider: Mapped[str | None] = mapped_column(String(64))
    llm_model: Mapped[str | None] = mapped_column(String(200))
    learning_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=LearningStatus.NONE,
    )
    learning_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    budget_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON_PAYLOAD)
    audit_log: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON_PAYLOAD)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "AILogRecord",
    "AccountFollowerSnapshot",
    "AccountRuntimeState",
    "ActionRequest",
    "Base",
    "CookieFreshness",
    "FetchCursor",
    "FetchErrorCode",
    "FetchedTweet",
    "LearningStatus",
    "OperationLog",
    "OperationStatus",
    "OperationType",
    "PauseReason",
    "ProxyHealth",
    "SourceType",
    "ActionStatus",
    "ActionType",
]
