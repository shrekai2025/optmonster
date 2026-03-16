from __future__ import annotations

from enum import StrEnum


class AccountLifecycleStatus(StrEnum):
    ENABLED = "enabled"
    PAUSED = "paused"


class CookieFreshness(StrEnum):
    UNKNOWN = "unknown"
    VALID = "valid"
    EXPIRED = "expired"


class ProxyHealth(StrEnum):
    DIRECT = "direct"
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class ExecutionMode(StrEnum):
    DRY_RUN = "dry_run"
    LIVE = "live"
    READ_ONLY = "read_only"


class PauseReason(StrEnum):
    ADMIN_DISABLED = "admin_disabled"
    AUTH_EXPIRED = "auth_expired"
    CONFIG_REMOVED = "config_removed"
    MANUAL = "manual"
    NONE = "none"
    PROXY_FAILED = "proxy_failed"
    REPEATED_FAILURES = "repeated_failures"
    SCHEMA_CHANGED = "schema_changed"


class OperationType(StrEnum):
    ACTION = "action"
    FETCH = "fetch"
    VALIDATE_SESSION = "validate_session"


class OperationStatus(StrEnum):
    FAILED = "failed"
    SKIPPED = "skipped"
    SUCCESS = "success"


class FetchErrorCode(StrEnum):
    AUTH_EXPIRED = "auth_expired"
    PROXY_FAILED = "proxy_failed"
    RATE_LIMITED = "rate_limited"
    SCHEMA_CHANGED = "schema_changed"
    TRANSIENT_NETWORK = "transient_network"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    KEYWORD_SEARCH = "keyword_search"
    TIMELINE = "timeline"
    WATCH_USER = "watch_user"


class ActionType(StrEnum):
    FOLLOW = "follow"
    LIKE = "like"
    REPLY = "reply"


class ActionStatus(StrEnum):
    APPROVED = "approved"
    EXECUTING = "executing"
    EXPIRED = "expired"
    FAILED = "failed"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    SUCCEEDED = "succeeded"


class ActionErrorCode(StrEnum):
    ACCOUNT_PAUSED = "account_paused"
    ADMIN_DISABLED = "admin_disabled"
    BUDGET_EXCEEDED = "budget_exceeded"
    EXECUTION_MODE_BLOCKED = "execution_mode_blocked"
    EXECUTION_FAILED = "execution_failed"
    INTERVAL_NOT_ELAPSED = "interval_not_elapsed"
    LLM_FAILED = "llm_failed"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"


class LearningStatus(StrEnum):
    APPLIED = "applied"
    FAILED = "failed"
    NONE = "none"
    PENDING = "pending"


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    MOCK = "mock"
    OPENAI_COMPATIBLE = "openai_compatible"
