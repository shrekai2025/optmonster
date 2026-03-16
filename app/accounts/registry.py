from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.accounts.loader import AccountConfigLoader
from app.accounts.schemas import AccountConfig


class AccountRegistry:
    def __init__(self, loader: AccountConfigLoader) -> None:
        self.loader = loader
        self._lock = asyncio.Lock()
        self._accounts: dict[str, AccountConfig] = {}

    async def reload(self) -> list[AccountConfig]:
        async with self._lock:
            accounts = self.loader.load_all()
            self._accounts = {account.id: account for account in accounts}
            return list(self._accounts.values())

    async def list_accounts(self) -> list[AccountConfig]:
        async with self._lock:
            return list(self._accounts.values())

    async def enabled_accounts(self) -> list[AccountConfig]:
        async with self._lock:
            return [account for account in self._accounts.values() if account.enabled]

    async def get(self, account_id: str) -> AccountConfig | None:
        async with self._lock:
            return self._accounts.get(account_id)

    async def ids(self) -> set[str]:
        async with self._lock:
            return set(self._accounts)

    async def values(self) -> Iterable[AccountConfig]:
        async with self._lock:
            return tuple(self._accounts.values())
