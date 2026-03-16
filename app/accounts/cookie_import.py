from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ALLOWED_DOMAINS = {"x.com", "twitter.com"}
COOKIE_FILE_SUFFIXES = {".txt", ".json", ".cookies", ".cookie"}


@dataclass(slots=True)
class CookieImportPreview:
    source_path: Path
    format_name: str
    suggested_account_id: str
    suggested_twitter_handle: str
    cookie_payload: dict[str, str]
    detected_domains: list[str]
    warnings: list[str]

    @property
    def twitter_cookie_count(self) -> int:
        return len(self.cookie_payload)

    @property
    def has_auth_token(self) -> bool:
        return "auth_token" in self.cookie_payload

    @property
    def has_ct0(self) -> bool:
        return "ct0" in self.cookie_payload


def scan_cookie_candidates(import_dir: Path) -> list[CookieImportPreview]:
    previews: list[CookieImportPreview] = []
    if not import_dir.exists():
        return previews

    for path in sorted(import_dir.iterdir()):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in COOKIE_FILE_SUFFIXES:
            continue
        try:
            preview = load_cookie_preview(path)
        except ValueError:
            continue
        if preview.twitter_cookie_count == 0:
            continue
        previews.append(preview)
    return previews


def load_cookie_preview(path: Path) -> CookieImportPreview:
    content = path.read_text(encoding="utf-8", errors="ignore")
    if _looks_like_netscape_cookie_file(content):
        cookie_payload, domains = _parse_netscape_cookie_file(content)
        format_name = "netscape_txt"
    else:
        cookie_payload, domains, format_name = _parse_json_cookie_file(content)

    suggested_handle = _suggest_twitter_handle(path.stem)
    suggested_id = _suggest_account_id(path.stem)
    warnings: list[str] = []
    if "auth_token" not in cookie_payload:
        warnings.append("missing auth_token")
    if "ct0" not in cookie_payload:
        warnings.append("missing ct0")

    return CookieImportPreview(
        source_path=path.resolve(),
        format_name=format_name,
        suggested_account_id=suggested_id,
        suggested_twitter_handle=suggested_handle,
        cookie_payload=cookie_payload,
        detected_domains=domains,
        warnings=warnings,
    )


def _looks_like_netscape_cookie_file(content: str) -> bool:
    return "Netscape HTTP Cookie File" in content or "\t" in content


def _parse_netscape_cookie_file(content: str) -> tuple[dict[str, str], list[str]]:
    selected: dict[str, tuple[int, str]] = {}
    seen_domains: set[str] = set()

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw_line.split("\t")
        if len(parts) != 7:
            continue
        raw_domain, _, _, _, _, name, value = parts
        domain = raw_domain.strip().lstrip(".").lower()
        if domain not in ALLOWED_DOMAINS:
            continue
        seen_domains.add(domain)
        priority = 2 if domain == "x.com" else 1
        current = selected.get(name)
        if current is None or priority >= current[0]:
            selected[name] = (priority, value)

    cookie_payload = {name: value for name, (_, value) in selected.items()}
    return cookie_payload, sorted(seen_domains)


def _parse_json_cookie_file(content: str) -> tuple[dict[str, str], list[str], str]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("unsupported cookie file format") from exc

    if isinstance(payload, dict):
        string_values = {str(key): str(value) for key, value in payload.items()}
        if not string_values:
            raise ValueError("cookie payload is empty")
        return string_values, [], "json_object"

    if isinstance(payload, list):
        selected: dict[str, tuple[int, str]] = {}
        seen_domains: set[str] = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            value = item.get("value")
            domain = str(item.get("domain", "")).strip().lstrip(".").lower()
            if not name or value is None:
                continue
            if domain and domain not in ALLOWED_DOMAINS:
                continue
            if domain:
                seen_domains.add(domain)
            priority = 2 if domain == "x.com" else 1
            current = selected.get(name)
            if current is None or priority >= current[0]:
                selected[name] = (priority, str(value))
        cookie_payload = {name: value for name, (_, value) in selected.items()}
        if not cookie_payload:
            raise ValueError("cookie payload is empty")
        return cookie_payload, sorted(seen_domains), "json_array"

    raise ValueError("unsupported cookie JSON structure")


def _suggest_account_id(stem: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_").lower()
    return normalized or "imported_account"


def _suggest_twitter_handle(stem: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]+", "", stem).strip("_")
    return f"@{normalized or 'imported_account'}"
