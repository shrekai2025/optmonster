from __future__ import annotations

import asyncio
from collections.abc import Iterable

from app.accounts.loader import AccountConfigLoader, AccountGroupConfigLoader
from app.accounts.schemas import AccountConfig, AccountGroupConfig


class AccountRegistry:
    def __init__(
        self,
        account_loader: AccountConfigLoader,
        *,
        group_loader: AccountGroupConfigLoader | None = None,
    ) -> None:
        self.loader = account_loader
        self.group_loader = group_loader
        self._lock = asyncio.Lock()
        self._accounts: dict[str, AccountConfig] = {}
        self._groups: dict[str, AccountGroupConfig] = {}

    async def reload(self) -> list[AccountConfig]:
        async with self._lock:
            groups = self.group_loader.load_all() if self.group_loader is not None else []
            self._groups = {group.id: group for group in groups}
            accounts = self.loader.load_all(groups_by_id=self._groups)
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

    async def list_groups(self) -> list[AccountGroupConfig]:
        async with self._lock:
            return list(self._groups.values())

    async def get_group(self, group_id: str) -> AccountGroupConfig | None:
        async with self._lock:
            return self._groups.get(group_id)
