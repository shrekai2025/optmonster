from __future__ import annotations

from pathlib import Path

import yaml

from app.accounts.schemas import AccountConfig, AccountGroupConfig


class AccountGroupConfigLoader:
    def __init__(self, *, config_dir: Path) -> None:
        self.config_dir = config_dir

    def load_all(self) -> list[AccountGroupConfig]:
        if not self.config_dir.exists():
            return []

        groups: list[AccountGroupConfig] = []
        seen_ids: set[str] = set()
        for path in sorted(self.config_dir.glob("*.y*ml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if raw is None:
                raise ValueError(f"group config is empty: {path}")

            group = AccountGroupConfig.model_validate(raw).ensure_runtime_fields(
                source_file=path.resolve()
            )
            if group.id in seen_ids:
                raise ValueError(f"duplicate group id found: {group.id}")
            seen_ids.add(group.id)
            groups.append(group)
        return groups


class AccountConfigLoader:
    def __init__(self, *, config_dir: Path, default_cookie_dir: Path) -> None:
        self.config_dir = config_dir
        self.default_cookie_dir = default_cookie_dir

    def load_all(
        self,
        *,
        groups_by_id: dict[str, AccountGroupConfig] | None = None,
    ) -> list[AccountConfig]:
        if not self.config_dir.exists():
            return []

        accounts: list[AccountConfig] = []
        seen_ids: set[str] = set()

        for path in sorted(self.config_dir.glob("*.y*ml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            if raw is None:
                raise ValueError(f"account config is empty: {path}")

            account = AccountConfig.model_validate(raw)
            if account.group_id:
                if groups_by_id is None or account.group_id not in groups_by_id:
                    raise ValueError(f"unknown group id for {account.id}: {account.group_id}")
                account = account.apply_group(groups_by_id[account.group_id])
            resolved_cookie = self.resolve_cookie_path(account.cookie_file)
            if not resolved_cookie.exists():
                raise ValueError(f"cookie file does not exist for {account.id}: {resolved_cookie}")

            account = account.ensure_runtime_fields(
                source_file=path.resolve(),
                resolved_cookie_file=resolved_cookie,
            )
            if account.id in seen_ids:
                raise ValueError(f"duplicate account id found: {account.id}")
            seen_ids.add(account.id)
            accounts.append(account)

        return accounts

    def resolve_cookie_path(self, cookie_file: Path) -> Path:
        if cookie_file.is_absolute():
            return cookie_file.resolve()
        if cookie_file.parent == Path("."):
            return (self.default_cookie_dir / cookie_file.name).resolve()
        return cookie_file.resolve()
