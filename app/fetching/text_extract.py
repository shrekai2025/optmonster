from __future__ import annotations

from typing import Any


def pick_best_tweet_text(default_text: str | None, raw_payload: dict[str, Any] | None) -> str:
    candidates: list[str] = []
    if default_text and default_text.strip():
        candidates.append(default_text.strip())
    payload_text = extract_text_from_payload(raw_payload)
    if payload_text:
        candidates.append(payload_text)
    if not candidates:
        return ""
    return max(candidates, key=lambda value: len(value.strip()))


def extract_text_from_payload(raw_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(raw_payload, dict):
        return None

    candidates: list[str] = []
    for path in (
        ("note_tweet", "note_tweet_results", "result", "text"),
        ("note_tweet", "note_tweet_results", "result", "richtext", "text"),
        ("legacy", "note_tweet", "note_tweet_results", "result", "text"),
        ("legacy", "full_text"),
        ("full_text",),
    ):
        value = _value_at_path(raw_payload, path)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    for key in ("full_text", "text"):
        value = _find_first_key(raw_payload, key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    if not candidates:
        return None
    return max(candidates, key=lambda value: len(value.strip()))


def _value_at_path(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _find_first_key(payload: Any, key: str) -> Any:
    if isinstance(payload, dict):
        if key in payload and payload[key]:
            return payload[key]
        for value in payload.values():
            found = _find_first_key(value, key)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_first_key(item, key)
            if found:
                return found
    return None
