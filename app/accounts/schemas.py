from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.runtime.enums import (
    AccountLifecycleStatus,
    CookieFreshness,
    ExecutionMode,
    FetchErrorCode,
    LLMProvider,
    PauseReason,
    ProxyHealth,
    SourceType,
)


class ProxyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    label: str | None = None
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        allowed = {"http", "https", "socks5", "socks5h"}
        parts = urlsplit(value)
        if parts.scheme not in allowed:
            raise ValueError(f"proxy scheme must be one of {sorted(allowed)}")
        if not parts.netloc:
            raise ValueError("proxy URL must include host")
        return value

    @property
    def masked_url(self) -> str:
        parts = urlsplit(self.url)
        if "@" in parts.netloc:
            _, host = parts.netloc.rsplit("@", 1)
            netloc = f"***@{host}"
        else:
            netloc = parts.netloc
        return f"{parts.scheme}://{netloc}"


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    daily_likes_max: int = Field(default=30, ge=0)
    daily_replies_max: int = Field(default=8, ge=0)
    daily_follows_max: int = Field(default=5, ge=0)
    active_hours: tuple[int, int] = (8, 23)
    min_interval_minutes: int = Field(default=15, ge=0)

    @field_validator("active_hours")
    @classmethod
    def validate_hours(cls, value: tuple[int, int]) -> tuple[int, int]:
        if len(value) != 2:
            raise ValueError("active_hours must contain exactly 2 items")
        start, end = value
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError("active hours must be between 0 and 23")
        if start > end:
            raise ValueError("active_hours must be ordered")
        return value


class FetchScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_interval_minutes: int = Field(default=5, ge=1, le=1440)
    interval_jitter_minutes: int = Field(default=0, ge=0, le=720)
    quiet_hours: tuple[int, int] | None = None

    @field_validator("quiet_hours", mode="before")
    @classmethod
    def normalize_quiet_hours(cls, value: Any) -> tuple[int, int] | None:
        if value in (None, "", []):
            return None
        return value

    @field_validator("quiet_hours")
    @classmethod
    def validate_quiet_hours(cls, value: tuple[int, int] | None) -> tuple[int, int] | None:
        if value is None:
            return None
        if len(value) != 2:
            raise ValueError("quiet_hours must contain exactly 2 items")
        start, end = value
        if not 0 <= start <= 23 or not 0 <= end <= 23:
            raise ValueError("quiet hours must be between 0 and 23")
        if start == end:
            raise ValueError("quiet_hours start and end must be different")
        return value


class FollowUserTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str
    count: int = Field(default=20, ge=1, le=40)

    @field_validator("handle")
    @classmethod
    def normalize_handle(cls, value: str) -> str:
        return value if value.startswith("@") else f"@{value}"


class KeywordTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    count: int = Field(default=20, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query cannot be empty")
        return value.strip()


class TargetsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeline: bool = True
    follow_users_enabled: bool = True
    search_keywords_enabled: bool = True
    follow_users: list[FollowUserTarget] = Field(default_factory=list)
    search_keywords: list[KeywordTarget] = Field(default_factory=list)

    @field_validator("follow_users", mode="before")
    @classmethod
    def normalize_follow_users(cls, value: Any) -> list[dict[str, Any]]:
        items = value or []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"handle": item})
            else:
                normalized.append(item)
        return normalized

    @field_validator("search_keywords", mode="before")
    @classmethod
    def normalize_keyword_queries(cls, value: Any) -> list[dict[str, Any]]:
        items = value or []
        normalized: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, str):
                normalized.append({"query": item})
            else:
                normalized.append(item)
        return normalized


class FetchSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_key: str
    enabled: bool = True
    limit: int = Field(default=20, ge=1, le=40)


class PersonaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Operator"
    role: str = "Thoughtful X account operator"
    tone: str = "Clear, concise, and warm"
    language: str = "English"
    forbidden_topics: list[str] = Field(default_factory=list)
    reply_style: str = "Offer one concrete thought or question in under 40 words."

    @field_validator("forbidden_topics", mode="before")
    @classmethod
    def normalize_forbidden_topics(cls, value: Any) -> list[str]:
        items = value or []
        normalized: list[str] = []
        for item in items:
            cleaned = str(item).strip()
            if cleaned:
                normalized.append(cleaned)
        return normalized


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    twitter_handle: str
    enabled: bool = True
    execution_mode: ExecutionMode = ExecutionMode.READ_ONLY
    cookie_file: Path
    proxy: ProxyConfig | None = None
    targets: TargetsConfig = Field(default_factory=TargetsConfig)
    fetch_schedule: FetchScheduleConfig = Field(default_factory=FetchScheduleConfig)
    behavior_budget: BudgetConfig = Field(default_factory=BudgetConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    writing_guide_file: Path | None = None
    source_file: Path | None = None
    resolved_cookie_file: Path | None = None
    config_revision: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("id cannot be empty")
        if any(char.isspace() for char in cleaned):
            raise ValueError("id cannot contain whitespace")
        return cleaned

    @field_validator("twitter_handle")
    @classmethod
    def normalize_handle(cls, value: str) -> str:
        cleaned = value.strip()
        return cleaned if cleaned.startswith("@") else f"@{cleaned}"

    @field_validator("cookie_file")
    @classmethod
    def validate_cookie_suffix(cls, value: Path) -> Path:
        if value.suffix != ".json":
            raise ValueError("cookie_file must end with .json")
        return value

    @model_validator(mode="after")
    def apply_defaults(self) -> AccountConfig:
        if self.writing_guide_file is None:
            self.writing_guide_file = Path(f"config/writing_guides/{self.id}.md")
        return self

    def build_fetch_sources(self, default_limit: int) -> list[FetchSourceConfig]:
        sources: list[FetchSourceConfig] = []
        if self.targets.timeline:
            sources.append(
                FetchSourceConfig(
                    source_type=SourceType.TIMELINE,
                    source_key="home_following",
                    limit=default_limit,
                )
            )
        if self.targets.follow_users_enabled:
            for item in self.targets.follow_users:
                sources.append(
                    FetchSourceConfig(
                        source_type=SourceType.WATCH_USER,
                        source_key=item.handle,
                        limit=item.count,
                    )
                )
        if self.targets.search_keywords_enabled:
            for item in self.targets.search_keywords:
                sources.append(
                    FetchSourceConfig(
                        source_type=SourceType.KEYWORD_SEARCH,
                        source_key=item.query,
                        limit=item.count,
                    )
                )
        return sources

    def ensure_runtime_fields(
        self,
        *,
        source_file: Path,
        resolved_cookie_file: Path,
    ) -> AccountConfig:
        revision = sha256(source_file.read_bytes()).hexdigest()
        return self.model_copy(
            update={
                "source_file": source_file,
                "resolved_cookie_file": resolved_cookie_file,
                "config_revision": revision,
            }
        )


class AccountAdminView(BaseModel):
    id: str
    twitter_handle: str
    enabled: bool
    execution_mode: ExecutionMode
    source_file: str
    config_revision: str
    cookie_file: str
    fetch_schedule: FetchScheduleConfig
    proxy_enabled: bool
    proxy_url_masked: str | None
    fetch_sources: list[FetchSourceConfig]
    lifecycle_status: AccountLifecycleStatus
    pause_reason: PauseReason
    last_auth_check: datetime | None
    cookie_freshness: CookieFreshness
    proxy_health: ProxyHealth
    failure_streak: int
    last_fetch_started_at: datetime | None
    last_fetch_finished_at: datetime | None
    last_error_code: FetchErrorCode | None
    last_error_message: str | None


class DashboardSummary(BaseModel):
    total_accounts: int
    enabled_accounts: int
    usable_accounts: int
    paused_accounts: int
    total_tweets: int
    latest_fetch_at: datetime | None


class BudgetMeterView(BaseModel):
    action_type: str
    used: int
    max: int
    remaining: int
    ratio: float


class RuntimeSettingsView(BaseModel):
    current_env_file: str
    app_timezone: str
    ai_enabled: bool
    fetch_recent_window_hours: int
    fetch_latest_first: bool
    fetch_include_replies: bool
    fetch_include_retweets: bool
    llm_provider: LLMProvider
    llm_base_url: str | None
    llm_model_id: str | None
    llm_api_key_configured: bool
    llm_api_key_masked: str | None


class RuntimeSettingsUpdateRequest(BaseModel):
    ai_enabled: bool = False
    fetch_recent_window_hours: int = Field(default=24, ge=0, le=720)
    fetch_latest_first: bool = True
    fetch_include_replies: bool = True
    fetch_include_retweets: bool = True
    llm_provider: LLMProvider
    llm_base_url: str | None = None
    llm_model_id: str | None = None
    llm_api_key: str | None = None
    replace_api_key: bool = False


class RuntimeSettingsUpdateResult(BaseModel):
    runtime_settings: RuntimeSettingsView
    persisted_env_file: str
    auto_scored_tweets: int = 0


class FollowerSnapshotPoint(BaseModel):
    snapshot_date: date
    follower_count: int


class AccountDashboardItem(BaseModel):
    id: str
    twitter_handle: str
    enabled: bool
    execution_mode: ExecutionMode
    persona_name: str
    writing_guide_file: str
    fetch_schedule: FetchScheduleConfig
    lifecycle_status: AccountLifecycleStatus
    pause_reason: PauseReason
    cookie_freshness: CookieFreshness
    proxy_health: ProxyHealth
    failure_streak: int
    tweet_count: int
    latest_tweet_at: datetime | None
    last_fetch_finished_at: datetime | None
    last_error_code: FetchErrorCode | None
    last_error_message: str | None
    follower_count: int | None = None
    follower_delta: int | None = None
    follower_history: list[FollowerSnapshotPoint] = Field(default_factory=list)
    budgets: list[BudgetMeterView]
    daily_reset_in_seconds: int
    next_action_in_seconds: int


class OperationLogView(BaseModel):
    account_id: str
    operation_type: str
    status: str
    error_code: str | None
    message: str | None
    metadata: dict[str, Any] | None = None
    created_at: datetime


class DashboardView(BaseModel):
    summary: DashboardSummary
    runtime_settings: RuntimeSettingsView
    accounts: list[AccountDashboardItem]
    recent_operations: list[OperationLogView]


class TweetActionSummary(BaseModel):
    id: int
    action_type: str
    status: str
    ai_draft: str | None = None
    edited_draft: str | None = None
    final_draft: str | None = None
    relevance_score: int | None = None
    reply_confidence: int | None = None
    requested_execution_mode: ExecutionMode
    applied_execution_mode: ExecutionMode | None
    created_at: datetime
    updated_at: datetime


class TweetDecisionSummary(BaseModel):
    status: str
    relevance_score: int | None = None
    reply_confidence: int | None = None
    rationale: str | None = None
    created_at: datetime


TweetInteractionState = Literal["unscored", "scored_no_action", "acted"]


class TweetAuthorCoverageItem(BaseModel):
    account_id: str
    twitter_handle: str
    execution_mode: ExecutionMode
    lifecycle_status: AccountLifecycleStatus
    follows_author: bool
    can_add_follow: bool
    follow_reason: str
    latest_follow_action: TweetActionSummary | None = None


class TweetListItem(BaseModel):
    id: int
    tweet_id: str
    account_id: str
    source_type: SourceType
    source_key: str
    author_handle: str | None
    text: str
    lang: str | None
    created_at_twitter: datetime | None
    fetched_at: datetime
    tweet_url: str
    interaction_state: TweetInteractionState
    latest_decision: TweetDecisionSummary | None = None
    latest_reply_action: TweetActionSummary | None = None
    latest_like_action: TweetActionSummary | None = None


class TweetDetailView(TweetListItem):
    raw_payload: dict[str, Any] | None = None
    author_coverage: list[TweetAuthorCoverageItem] = Field(default_factory=list)
    actions: list[TweetActionSummary] = Field(default_factory=list)


class TweetMaintenanceRequest(BaseModel):
    account_id: str | None = None


class TweetCleanupResult(BaseModel):
    account_id: str | None = None
    deleted_tweets: int
    deleted_ai_logs: int
    deleted_actions: int
    deleted_missing_account_tweets: int = 0
    deleted_outside_window_tweets: int = 0
    deleted_filtered_reply_tweets: int = 0
    deleted_filtered_retweet_tweets: int = 0
    deleted_disabled_scope_tweets: int = 0


class TweetBackfillResult(BaseModel):
    account_id: str | None = None
    candidate_tweets: int
    scored_tweets: int
    failed_tweets: int


class AccountConfigEditView(BaseModel):
    id: str
    twitter_handle: str
    enabled: bool
    execution_mode: ExecutionMode
    cookie_file: str
    proxy: ProxyConfig | None = None
    targets: TargetsConfig
    fetch_schedule: FetchScheduleConfig
    behavior_budget: BudgetConfig
    persona: PersonaConfig
    writing_guide_file: str


class AccountConfigDocumentView(BaseModel):
    account: AccountConfigEditView
    source_file: str
    config_revision: str
    lifecycle_status: AccountLifecycleStatus
    pause_reason: PauseReason
    cookie_freshness: CookieFreshness
    proxy_health: ProxyHealth
    failure_streak: int
    last_auth_check: datetime | None
    last_fetch_finished_at: datetime | None
    last_error_code: FetchErrorCode | None
    last_error_message: str | None
    recent_operations: list[OperationLogView] = Field(default_factory=list)


class ReloadSummary(BaseModel):
    loaded_accounts: int
    new_accounts: int
    updated_accounts: int
    removed_accounts: int


class AccountConfigUpdateResult(BaseModel):
    account: AccountConfigDocumentView
    reload_summary: ReloadSummary


class FetchEnqueueResponse(BaseModel):
    account_id: str
    enqueued: bool
    detail: str | None = None


class AccountStateChangeResponse(BaseModel):
    account_id: str
    lifecycle_status: AccountLifecycleStatus
    pause_reason: PauseReason


class FollowTargetUpdateResponse(BaseModel):
    tweet_record_id: int
    target_account_id: str
    author_handle: str
    added_to_follow_scope: bool
    fetch_enqueued: bool
    detail: str


class AccountDeleteResponse(BaseModel):
    account_id: str
    deleted_config_file: bool
    deleted_cookie_file: bool
    deleted_writing_guide_file: bool
    reload_summary: ReloadSummary


class CookieImportCandidateView(BaseModel):
    source_file: str
    format_name: str
    suggested_account_id: str
    suggested_twitter_handle: str
    twitter_cookie_count: int
    detected_domains: list[str]
    has_auth_token: bool
    has_ct0: bool
    warnings: list[str] = Field(default_factory=list)


class CookieImportRequest(BaseModel):
    source_file: str
    id: str | None = None
    twitter_handle: str | None = None
    enabled: bool = True
    execution_mode: ExecutionMode = ExecutionMode.READ_ONLY
    extra_yaml: str | None = None


class CookieImportResult(BaseModel):
    source_file: str
    imported_cookie_file: str
    cookie_count: int
    has_auth_token: bool
    has_ct0: bool
    validation_ok: bool
    validation_detail: str | None = None
    validation_error_code: FetchErrorCode | None = None
    reload_summary: ReloadSummary
    account: AccountConfigDocumentView
