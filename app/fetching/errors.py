from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.runtime.enums import FetchErrorCode, PauseReason

try:  # pragma: no cover - exercised when twikit is installed
    from twikit.errors import TooManyRequests, TwitterException, Unauthorized
except Exception:  # pragma: no cover - import fallback for tests
    class TwitterException(Exception):
        """Fallback twikit base exception."""

    class Unauthorized(TwitterException):
        """Fallback unauthorized exception."""

    class TooManyRequests(TwitterException):
        """Fallback rate limit exception."""


@dataclass(slots=True)
class FetchError(Exception):
    code: FetchErrorCode
    detail: str
    retryable: bool = True
    pause_reason: PauseReason | None = None
    retry_after_seconds: int | None = None

    def __str__(self) -> str:
        return self.detail


def classify_exception(exc: Exception) -> FetchError:
    if isinstance(exc, FetchError):
        return exc
    if isinstance(exc, (httpx.ProxyError, httpx.ConnectError)):
        return FetchError(
            code=FetchErrorCode.PROXY_FAILED,
            detail=str(exc) or "proxy failed",
            retryable=False,
            pause_reason=PauseReason.PROXY_FAILED,
        )
    if isinstance(exc, (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.NetworkError)):
        return FetchError(
            code=FetchErrorCode.TRANSIENT_NETWORK,
            detail=str(exc) or "transient network error",
        )
    if isinstance(exc, Unauthorized):
        return FetchError(
            code=FetchErrorCode.AUTH_EXPIRED,
            detail=str(exc) or "twikit unauthorized",
            retryable=False,
            pause_reason=PauseReason.AUTH_EXPIRED,
        )
    if isinstance(exc, TooManyRequests):
        retry_after_seconds = None
        reset_at = getattr(exc, "rate_limit_reset", None)
        if isinstance(reset_at, int):
            retry_after_seconds = max(reset_at - int(datetime.now(UTC).timestamp()), 0)
        return FetchError(
            code=FetchErrorCode.RATE_LIMITED,
            detail=str(exc) or "twikit rate limited",
            retry_after_seconds=retry_after_seconds,
        )
    if isinstance(exc, ValueError):
        return FetchError(
            code=FetchErrorCode.SCHEMA_CHANGED,
            detail=str(exc) or "schema changed",
            retryable=False,
            pause_reason=PauseReason.SCHEMA_CHANGED,
        )
    if isinstance(exc, TwitterException):
        message = str(exc) or exc.__class__.__name__
        return _classify_twitter_exception(message)
    return FetchError(code=FetchErrorCode.UNKNOWN, detail=str(exc) or exc.__class__.__name__)


def _classify_twitter_exception(message: str) -> FetchError:
    lowered = message.lower()
    if "authorization" in lowered or "login" in lowered or "credential" in lowered:
        return FetchError(
            code=FetchErrorCode.AUTH_EXPIRED,
            detail=message,
            retryable=False,
            pause_reason=PauseReason.AUTH_EXPIRED,
        )
    if "rate limit" in lowered or "too many requests" in lowered:
        return FetchError(code=FetchErrorCode.RATE_LIMITED, detail=message)
    if "proxy" in lowered or "tunnel" in lowered:
        return FetchError(
            code=FetchErrorCode.PROXY_FAILED,
            detail=message,
            retryable=False,
            pause_reason=PauseReason.PROXY_FAILED,
        )
    if "missing" in lowered or "unexpected" in lowered or "parse" in lowered:
        return FetchError(
            code=FetchErrorCode.SCHEMA_CHANGED,
            detail=message,
            retryable=False,
            pause_reason=PauseReason.SCHEMA_CHANGED,
        )
    return FetchError(code=FetchErrorCode.UNKNOWN, detail=message)
