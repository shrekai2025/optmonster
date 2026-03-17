from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.runtime.enums import LLMProvider


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "optmonster"
    app_env: str = "dev"
    app_env_file: Path = Field(default=Path(".env"))
    host: str = "0.0.0.0"
    port: int = 8000
    database_url: str = "sqlite+aiosqlite:///./optmonster.db"
    redis_url: str = "redis://localhost:6379/0"
    config_dir: Path = Field(default=Path("config/accounts"))
    group_config_dir: Path = Field(default=Path("config/groups"))
    cookie_dir: Path = Field(default=Path("config/cookies"))
    cookie_import_dir: Path = Field(default=Path("."))
    worker_poll_interval_seconds: int = 30
    scheduled_fetch_interval_seconds: int = 300
    fetch_limit_default: int = 20
    fetch_recent_window_hours: int = 24
    fetch_latest_first: bool = True
    fetch_include_replies: bool = True
    fetch_include_retweets: bool = True
    popular_tweet_min_views: int = 100000
    popular_tweet_min_likes: int = 500
    popular_tweet_min_retweets: int = 50
    popular_tweet_min_replies: int = 20
    lock_ttl_seconds: int = 300
    pause_after_failures: int = 3
    backoff_base_seconds: int = 30
    backoff_max_seconds: int = 900
    action_interval_jitter_seconds: int = 300
    browser_fallback_enabled: bool = False
    twikit_locale: str = "en-US"
    app_timezone: str = "UTC"
    ai_enabled: bool = False
    llm_provider: LLMProvider = LLMProvider.MOCK
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model_id: str | None = None
    llm_request_timeout_seconds: int = 45
    ai_prompt_config_file: Path = Field(default=Path("config/ai/prompts.yaml"))
    writing_guides_dir: Path = Field(default=Path("config/writing_guides"))

    @property
    def project_root(self) -> Path:
        return Path.cwd()

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    def resolve_path(self, value: Path | str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (self.project_root / path).resolve()

    @property
    def llm_api_key_masked(self) -> str | None:
        if not self.llm_api_key:
            return None
        if len(self.llm_api_key) <= 8:
            return "*" * len(self.llm_api_key)
        return f"{self.llm_api_key[:4]}...{self.llm_api_key[-4:]}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = Path(os.getenv("APP_ENV_FILE", ".env"))
    settings = Settings(_env_file=env_file)
    overrides = _load_runtime_overrides(settings.resolve_path(env_file))
    if "APP_TIMEZONE" in overrides:
        settings.app_timezone = overrides["APP_TIMEZONE"]
    if "AI_ENABLED" in overrides:
        settings.ai_enabled = overrides["AI_ENABLED"].lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if "LLM_PROVIDER" in overrides:
        settings.llm_provider = LLMProvider(overrides["LLM_PROVIDER"])
    if "FETCH_RECENT_WINDOW_HOURS" in overrides:
        settings.fetch_recent_window_hours = int(overrides["FETCH_RECENT_WINDOW_HOURS"])
    if "FETCH_LATEST_FIRST" in overrides:
        settings.fetch_latest_first = overrides["FETCH_LATEST_FIRST"].lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if "FETCH_INCLUDE_REPLIES" in overrides:
        settings.fetch_include_replies = overrides["FETCH_INCLUDE_REPLIES"].lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if "FETCH_INCLUDE_RETWEETS" in overrides:
        settings.fetch_include_retweets = overrides["FETCH_INCLUDE_RETWEETS"].lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    if "POPULAR_TWEET_MIN_VIEWS" in overrides:
        settings.popular_tweet_min_views = int(overrides["POPULAR_TWEET_MIN_VIEWS"])
    if "POPULAR_TWEET_MIN_LIKES" in overrides:
        settings.popular_tweet_min_likes = int(overrides["POPULAR_TWEET_MIN_LIKES"])
    if "POPULAR_TWEET_MIN_RETWEETS" in overrides:
        settings.popular_tweet_min_retweets = int(overrides["POPULAR_TWEET_MIN_RETWEETS"])
    if "POPULAR_TWEET_MIN_REPLIES" in overrides:
        settings.popular_tweet_min_replies = int(overrides["POPULAR_TWEET_MIN_REPLIES"])
    settings.llm_base_url = overrides.get("LLM_BASE_URL", settings.llm_base_url)
    settings.llm_api_key = overrides.get("LLM_API_KEY", settings.llm_api_key)
    settings.llm_model_id = overrides.get("LLM_MODEL_ID", settings.llm_model_id)
    return settings


def _load_runtime_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    overrides: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {
            "APP_TIMEZONE",
            "AI_ENABLED",
            "FETCH_RECENT_WINDOW_HOURS",
            "FETCH_LATEST_FIRST",
            "FETCH_INCLUDE_REPLIES",
            "FETCH_INCLUDE_RETWEETS",
            "POPULAR_TWEET_MIN_VIEWS",
            "POPULAR_TWEET_MIN_LIKES",
            "POPULAR_TWEET_MIN_RETWEETS",
            "POPULAR_TWEET_MIN_REPLIES",
            "LLM_PROVIDER",
            "LLM_BASE_URL",
            "LLM_API_KEY",
            "LLM_MODEL_ID",
        }:
            overrides[key] = value.strip()
    return overrides
