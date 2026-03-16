from __future__ import annotations

from typing import Any

from app.accounts.schemas import AccountConfig
from app.fetching.datasource import TwitterDataSource
from app.fetching.errors import FetchError, classify_exception
from app.fetching.schemas import (
    AccountProfileSnapshot,
    FetchBatchResult,
    NormalizedTweet,
    SessionValidationResult,
)
from app.fetching.text_extract import pick_best_tweet_text
from app.runtime.enums import FetchErrorCode, PauseReason
from app.runtime.settings import Settings

try:  # pragma: no cover - exercised when twikit is installed
    from twikit import Client
except Exception:  # pragma: no cover - import fallback for tests
    Client = None


class TwikitDataSource(TwitterDataSource):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def validate_session(self, account: AccountConfig) -> SessionValidationResult:
        client = self._build_client(account)
        try:
            await client.user()
            return SessionValidationResult(ok=True, detail="session valid")
        except Exception as exc:
            fetch_error = classify_exception(exc)
            return SessionValidationResult(
                ok=False,
                detail=fetch_error.detail,
                error_code=fetch_error.code,
            )
        finally:
            await self._close_client(client)

    async def get_account_profile(self, account: AccountConfig) -> AccountProfileSnapshot:
        client = self._build_client(account)
        try:
            user = await client.user()
            return AccountProfileSnapshot(
                follower_count=self._extract_follower_count(user),
                twitter_handle=self._extract_twitter_handle(user) or account.twitter_handle,
            )
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            await self._close_client(client)

    async def fetch_home_timeline(
        self,
        account: AccountConfig,
        *,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        client = self._build_client(account)
        try:
            result = await client.get_latest_timeline(count=limit, cursor=cursor)
            return self._normalize_result(result)
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            await self._close_client(client)

    async def fetch_user_tweets(
        self,
        account: AccountConfig,
        *,
        user_handle: str,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        client = self._build_client(account)
        try:
            user = await client.get_user_by_screen_name(user_handle.lstrip("@"))
            result = await client.get_user_tweets(user.id, "Tweets", count=limit, cursor=cursor)
            return self._normalize_result(result)
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            await self._close_client(client)

    async def search_recent(
        self,
        account: AccountConfig,
        *,
        query: str,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        client = self._build_client(account)
        try:
            result = await client.search_tweet(query, "Latest", count=min(limit, 20), cursor=cursor)
            return self._normalize_result(result)
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            await self._close_client(client)

    def _build_client(self, account: AccountConfig) -> Any:
        if Client is None:
            raise FetchError(
                code=FetchErrorCode.SCHEMA_CHANGED,
                detail="twikit is not installed",
                retryable=False,
                pause_reason=PauseReason.SCHEMA_CHANGED,
            )
        proxy_url = account.proxy.url if account.proxy and account.proxy.enabled else None
        client = Client(language=self.settings.twikit_locale, proxy=proxy_url)
        client.load_cookies(str(account.resolved_cookie_file))
        return client

    async def _close_client(self, client: Any) -> None:
        http_client = getattr(client, "http", None)
        if http_client is not None:
            await http_client.aclose()

    def _normalize_result(self, result: Any) -> FetchBatchResult:
        items = [self._normalize_tweet(tweet) for tweet in list(result)]
        next_cursor = getattr(result, "next_cursor", None)
        return FetchBatchResult(items=items, next_cursor=next_cursor)

    def _normalize_tweet(self, tweet: Any) -> NormalizedTweet:
        tweet_id = str(getattr(tweet, "id", ""))
        raw_payload = getattr(tweet, "_data", None)
        text = pick_best_tweet_text(
            getattr(tweet, "full_text", None) or getattr(tweet, "text", ""),
            raw_payload if isinstance(raw_payload, dict) else None,
        )
        if not tweet_id or not text:
            raise ValueError("twikit tweet payload missing id or text")

        user = getattr(tweet, "user", None)
        screen_name = getattr(user, "screen_name", None)
        author_handle = f"@{screen_name}" if screen_name else None
        created_at = getattr(tweet, "created_at_datetime", None)
        if raw_payload is not None and not isinstance(raw_payload, dict):
            raise ValueError("twikit tweet raw payload must be a dict")

        return NormalizedTweet(
            tweet_id=tweet_id,
            author_handle=author_handle,
            text=text,
            lang=getattr(tweet, "lang", None),
            is_reply=self._is_reply(tweet, raw_payload),
            is_retweet=self._is_retweet(tweet, raw_payload),
            created_at=created_at,
            raw_payload=raw_payload,
        )

    def _is_reply(self, tweet: Any, raw_payload: dict[str, Any] | None) -> bool:
        for attr in (
            "in_reply_to_status_id",
            "in_reply_to_status_id_str",
            "in_reply_to_user_id",
            "in_reply_to_user_id_str",
            "reply_to",
            "is_reply",
        ):
            if getattr(tweet, attr, None):
                return True
        return self._payload_contains_any(
            raw_payload,
            (
                "in_reply_to_status_id",
                "in_reply_to_status_id_str",
                "in_reply_to_user_id",
                "in_reply_to_user_id_str",
            ),
        )

    def _is_retweet(self, tweet: Any, raw_payload: dict[str, Any] | None) -> bool:
        for attr in (
            "retweeted_tweet",
            "retweeted_status",
            "retweeted_status_result",
            "is_retweet",
        ):
            if getattr(tweet, attr, None):
                return True
        return self._payload_contains_any(
            raw_payload,
            (
                "retweeted_status",
                "retweeted_status_result",
            ),
        )

    def _payload_contains_any(
        self,
        payload: Any,
        keys: tuple[str, ...],
    ) -> bool:
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in keys and value:
                    return True
                if self._payload_contains_any(value, keys):
                    return True
        if isinstance(payload, list):
            for item in payload:
                if self._payload_contains_any(item, keys):
                    return True
        return False

    def _extract_follower_count(self, user: Any) -> int | None:
        for attr in ("followers_count", "followersCount"):
            value = getattr(user, attr, None)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        data = getattr(user, "_data", None)
        if isinstance(data, dict):
            legacy_value = data.get("followers_count") or data.get("followersCount")
            if legacy_value is not None:
                try:
                    return int(legacy_value)
                except (TypeError, ValueError):
                    return None
        return None

    def _extract_twitter_handle(self, user: Any) -> str | None:
        screen_name = getattr(user, "screen_name", None)
        if screen_name:
            return screen_name if str(screen_name).startswith("@") else f"@{screen_name}"
        return None
