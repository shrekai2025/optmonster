from __future__ import annotations

from app.accounts.schemas import AccountConfig
from app.fetching.datasource import TwitterDataSource
from app.fetching.schemas import AccountProfileSnapshot, FetchBatchResult, SessionValidationResult


class BrowserFallbackDataSource(TwitterDataSource):
    async def validate_session(self, account: AccountConfig) -> SessionValidationResult:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")

    async def get_account_profile(self, account: AccountConfig) -> AccountProfileSnapshot:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")

    async def fetch_home_timeline(
        self,
        account: AccountConfig,
        *,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")

    async def fetch_user_tweets(
        self,
        account: AccountConfig,
        *,
        user_handle: str,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")

    async def fetch_for_you_timeline(
        self,
        account: AccountConfig,
        *,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")

    async def search_recent(
        self,
        account: AccountConfig,
        *,
        query: str,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")

    async def fetch_popular_timeline(
        self,
        account: AccountConfig,
        *,
        cursor: str | None,
        limit: int,
    ) -> FetchBatchResult:
        raise NotImplementedError("browser fallback datasource is not implemented in v1")
