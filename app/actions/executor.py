from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.accounts.schemas import AccountConfig
from app.fetching.errors import FetchError, classify_exception
from app.runtime.enums import FetchErrorCode, PauseReason
from app.runtime.settings import Settings

try:  # pragma: no cover - exercised when twikit is installed
    from twikit import Client
except Exception:  # pragma: no cover - import fallback for tests
    Client = None


class TwitterActionExecutor(ABC):
    @abstractmethod
    async def follow(self, account: AccountConfig, *, user_handle: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def like(self, account: AccountConfig, *, tweet_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def reply(
        self,
        account: AccountConfig,
        *,
        tweet_id: str,
        text: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class TwikitActionExecutor(TwitterActionExecutor):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def follow(self, account: AccountConfig, *, user_handle: str) -> dict[str, Any]:
        client = self._build_client(account)
        try:
            user = await client.get_user_by_screen_name(user_handle.lstrip("@"))
            follow_method = getattr(client, "follow_user", None)
            if follow_method is None:
                raise AttributeError("twikit client does not support follow_user")
            result = await follow_method(str(user.id))
            return {"user_handle": user_handle, "user_id": str(user.id), "result": bool(result)}
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            await self._close_client(client)

    async def like(self, account: AccountConfig, *, tweet_id: str) -> dict[str, Any]:
        client = self._build_client(account)
        try:
            like_method = getattr(client, "favorite_tweet", None) or getattr(
                client,
                "like_tweet",
                None,
            )
            if like_method is None:
                raise AttributeError("twikit client does not support like_tweet/favorite_tweet")
            result = await like_method(tweet_id)
            return {"tweet_id": tweet_id, "result": bool(result)}
        except Exception as exc:
            raise classify_exception(exc) from exc
        finally:
            await self._close_client(client)

    async def reply(
        self,
        account: AccountConfig,
        *,
        tweet_id: str,
        text: str,
    ) -> dict[str, Any]:
        client = self._build_client(account)
        try:
            create_tweet = getattr(client, "create_tweet", None)
            if create_tweet is None:
                raise AttributeError("twikit client does not support create_tweet")
            try:
                result = await create_tweet(text=text, reply_to=tweet_id)
            except TypeError:
                result = await create_tweet(text=text, reply_to_tweet_id=tweet_id)
            return {"tweet_id": tweet_id, "text": text, "result": bool(result)}
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
