from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

import app.fetching.errors as errors_module
import app.fetching.twikit_source as twikit_module
from app.accounts.schemas import AccountConfig
from app.fetching.errors import classify_exception
from app.fetching.schemas import FetchBatchResult, NormalizedTweet, SessionValidationResult
from app.fetching.twikit_source import TwikitDataSource
from app.llm.schemas import DecisionResult
from app.runtime.enums import (
    AccountLifecycleStatus,
    FetchErrorCode,
    PauseReason,
    ProxyHealth,
    SourceType,
)
from app.runtime.models import AILogRecord
from app.runtime.settings import Settings


def test_proxy_is_injected_into_twikit_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, str | None] = {}

    class DummyClient:
        def __init__(self, language: str, proxy: str | None = None) -> None:
            captured["language"] = language
            captured["proxy"] = proxy

        def load_cookies(self, cookie_path: str) -> None:
            captured["cookie_path"] = cookie_path

    cookie_path = tmp_path / "acct.json"
    cookie_path.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "acct.yaml"
    config_path.write_text("id: acct\n", encoding="utf-8")

    account = AccountConfig.model_validate(
        {
            "id": "acct",
            "twitter_handle": "@acct",
            "cookie_file": str(cookie_path),
            "proxy": {"url": "socks5://127.0.0.1:9000"},
        }
    ).ensure_runtime_fields(source_file=config_path, resolved_cookie_file=cookie_path)

    monkeypatch.setattr(twikit_module, "Client", DummyClient)
    source = TwikitDataSource(Settings(_env_file=None, twikit_locale="en-US"))
    source._build_client(account)

    assert captured["language"] == "en-US"
    assert captured["proxy"] == "socks5://127.0.0.1:9000"
    assert captured["cookie_path"] == str(cookie_path)


def test_error_classification_maps_known_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "https://x.com")
    proxy_error = classify_exception(httpx.ProxyError("bad proxy", request=request))
    assert proxy_error.code == FetchErrorCode.PROXY_FAILED

    schema_error = classify_exception(ValueError("unexpected tweet payload"))
    assert schema_error.code == FetchErrorCode.SCHEMA_CHANGED

    class DummyTooManyRequests(Exception):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.rate_limit_reset = int((datetime.now(UTC) + timedelta(seconds=45)).timestamp())

    monkeypatch.setattr(errors_module, "TooManyRequests", DummyTooManyRequests)
    rate_limit_error = classify_exception(DummyTooManyRequests("rate limit hit"))
    assert rate_limit_error.code == FetchErrorCode.RATE_LIMITED
    assert rate_limit_error.retry_after_seconds is not None
    assert rate_limit_error.retry_after_seconds > 0


def test_twikit_normalization_marks_reply_and_retweet(tmp_path: Path) -> None:
    source = TwikitDataSource(Settings(_env_file=None))

    class DummyUser:
        screen_name = "openai"

    class DummyTweet:
        id = "1000"
        text = "RT @openai replying to things"
        lang = "en"
        user = DummyUser()
        created_at_datetime = datetime.now(UTC)
        in_reply_to_status_id = "998"
        retweeted_status_result = {"result": "yes"}
        _data = {
            "legacy": {
                "in_reply_to_status_id_str": "998",
                "retweeted_status_result": {"result": "yes"},
            }
        }

    normalized = source._normalize_tweet(DummyTweet())

    assert normalized.is_reply is True
    assert normalized.is_retweet is True


def test_twikit_normalization_prefers_full_text_from_payload() -> None:
    source = TwikitDataSource(Settings(_env_file=None))

    class DummyUser:
        screen_name = "openai"

    class DummyTweet:
        id = "1001"
        text = "Short preview..."
        lang = "en"
        user = DummyUser()
        created_at_datetime = datetime.now(UTC)
        _data = {
            "legacy": {
                "full_text": "This is the longer full text from the payload with more context."
            }
        }

    normalized = source._normalize_tweet(DummyTweet())

    assert normalized.text == "This is the longer full text from the payload with more context."


def test_twikit_normalization_captures_engagement_metrics() -> None:
    source = TwikitDataSource(Settings(_env_file=None))

    class DummyUser:
        screen_name = "openai"

    class DummyTweet:
        id = "1002"
        text = "Metrics payload"
        lang = "en"
        user = DummyUser()
        created_at_datetime = datetime.now(UTC)
        view_count = "120001"
        favorite_count = 780
        retweet_count = 65
        reply_count = 24
        _data = {"legacy": {}}

    normalized = source._normalize_tweet(DummyTweet())

    assert normalized.view_count == 120001
    assert normalized.like_count == 780
    assert normalized.retweet_count == 65
    assert normalized.reply_count == 24


@pytest.mark.asyncio
async def test_fetch_latest_first_restarts_from_head_and_deduplicates(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])

    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="1", text="hello"),
                NormalizedTweet(tweet_id="2", text="world"),
            ],
            next_cursor="older-1",
        ),
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="2", text="world"),
                NormalizedTweet(tweet_id="3", text="again"),
            ],
            next_cursor=None,
        ),
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="1", text="hello"),
                NormalizedTweet(tweet_id="4", text="fresh"),
            ],
            next_cursor=None,
        ),
    )

    first = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    second = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert first.status == "success"
    assert second.status == "success"
    assert await context.tweet_count("acct1") == 4

    cursor = await context.get_cursor("acct1", SourceType.TIMELINE, "home_following")
    assert cursor is not None
    assert cursor.cursor is None
    assert context.fake_source.fetch_requests[:3] == [
        ("acct1", SourceType.TIMELINE, "home_following", None, 20),
        ("acct1", SourceType.TIMELINE, "home_following", "older-1", 20),
        ("acct1", SourceType.TIMELINE, "home_following", None, 20),
    ]


@pytest.mark.asyncio
async def test_fetch_latest_first_stops_when_page_reaches_recent_window(make_test_context) -> None:
    now = datetime.now(UTC)
    context = await make_test_context(
        accounts=[{"id": "acct1"}],
        settings_overrides={
            "fetch_recent_window_hours": 6,
            "fetch_latest_first": True,
        },
    )

    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="11", text="recent", created_at=now - timedelta(hours=1)),
                NormalizedTweet(tweet_id="12", text="stale", created_at=now - timedelta(hours=8)),
            ],
            next_cursor="older-2",
        ),
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="13",
                    text="very old",
                    created_at=now - timedelta(hours=12),
                )
            ],
            next_cursor=None,
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    assert await context.tweet_count("acct1") == 1
    assert context.fake_source.fetch_requests == [
        ("acct1", SourceType.TIMELINE, "home_following", None, 20)
    ]


@pytest.mark.asyncio
async def test_fetch_recommended_timeline_uses_for_you_source(make_test_context) -> None:
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "timeline": False,
                "timeline_recommended": True,
                "follow_users_enabled": False,
                "search_keywords_enabled": False,
            }
        ]
    )

    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE_RECOMMENDED,
        "home_for_you",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="fy-1", text="Recommended post 1"),
                NormalizedTweet(tweet_id="fy-2", text="Recommended post 2"),
            ],
            next_cursor=None,
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    assert result.tweets_inserted == 2
    assert context.fake_source.fetch_requests == [
        ("acct1", SourceType.TIMELINE_RECOMMENDED, "home_for_you", None, 20)
    ]
    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    assert [tweet.tweet_id for tweet in tweets] == ["fy-2", "fy-1"]
    assert all(tweet.source_type == SourceType.TIMELINE_RECOMMENDED for tweet in tweets)
    cursor = await context.get_cursor("acct1", SourceType.TIMELINE_RECOMMENDED, "home_for_you")
    assert cursor is not None
    assert cursor.cursor is None


@pytest.mark.asyncio
async def test_fetch_popular_timeline_filters_to_thresholds(make_test_context) -> None:
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "timeline": False,
                "timeline_recommended": False,
                "timeline_popular": True,
                "follow_users_enabled": False,
                "search_keywords_enabled": False,
            }
        ],
        settings_overrides={
            "popular_tweet_min_views": 100000,
            "popular_tweet_min_likes": 500,
            "popular_tweet_min_retweets": 50,
            "popular_tweet_min_replies": 20,
        },
    )

    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE_POPULAR,
        "home_popular",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="hot-1",
                    text="viral post",
                    view_count=120000,
                    like_count=900,
                    retweet_count=80,
                    reply_count=40,
                ),
                NormalizedTweet(
                    tweet_id="hot-2",
                    text="not enough views",
                    view_count=90000,
                    like_count=900,
                    retweet_count=80,
                    reply_count=40,
                ),
                NormalizedTweet(
                    tweet_id="hot-3",
                    text="not enough likes",
                    view_count=150000,
                    like_count=300,
                    retweet_count=80,
                    reply_count=40,
                ),
            ],
            next_cursor=None,
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    assert result.tweets_inserted == 1
    assert context.fake_source.fetch_requests == [
        ("acct1", SourceType.TIMELINE_POPULAR, "home_popular", None, 20)
    ]
    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    assert [tweet.tweet_id for tweet in tweets] == ["hot-1"]
    assert tweets[0].view_count == 120000
    async with context.session_factory() as session:
        filter_log = (
            await session.execute(
                select(AILogRecord)
                .where(AILogRecord.account_id == "acct1", AILogRecord.log_type == "fetch_filter")
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
    assert filter_log.response_payload["popular_filtered"] == 2


@pytest.mark.asyncio
async def test_fetch_filters_replies_and_retweets_when_disabled(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1"}],
        settings_overrides={
            "fetch_include_replies": False,
            "fetch_include_retweets": False,
        },
    )

    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="21", text="plain post"),
                NormalizedTweet(tweet_id="22", text="reply post", is_reply=True),
                NormalizedTweet(tweet_id="23", text="rt post", is_retweet=True),
            ],
            next_cursor=None,
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    assert result.tweets_inserted == 1
    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    assert [tweet.tweet_id for tweet in tweets] == ["21"]
    async with context.session_factory() as session:
        filter_log = (
            await session.execute(
                select(AILogRecord)
                .where(AILogRecord.account_id == "acct1", AILogRecord.log_type == "fetch_filter")
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
    assert filter_log.response_payload == {
        "reply_filtered": 1,
        "retweet_filtered": 1,
        "popular_filtered": 0,
    }


@pytest.mark.asyncio
async def test_fetch_auto_scores_inserted_tweets_when_ai_enabled(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1", "execution_mode": "dry_run"}],
        settings_overrides={"ai_enabled": True},
    )

    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="24",
                    author_handle="@openai",
                    text="Auto scoring should begin after fetch.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    context.fake_llm.set_decision(
        "acct1",
        "Auto scoring should begin after fetch.",
        DecisionResult(
            relevance_score=6,
            like=False,
            reply_draft=None,
            reply_confidence=2,
            rationale="score only",
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    assert tweets[0].interaction_state == "scored_no_action"
    async with context.session_factory() as session:
        batch_log = (
            await session.execute(
                select(AILogRecord)
                .where(
                    AILogRecord.account_id == "acct1",
                    AILogRecord.log_type == "auto_score_batch",
                )
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
    assert batch_log.response_payload["scored_count"] == 1
    assert batch_log.response_payload["failed_count"] == 0


@pytest.mark.asyncio
async def test_fetch_records_auto_score_skip_when_ai_disabled(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[NormalizedTweet(tweet_id="25", text="AI is disabled for this fetch.")],
            next_cursor=None,
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    async with context.session_factory() as session:
        skip_log = (
            await session.execute(
                select(AILogRecord)
                .where(AILogRecord.account_id == "acct1", AILogRecord.log_type == "auto_score_skip")
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
    assert skip_log.status == "skipped"
    assert skip_log.response_payload == {
        "reason": "ai_disabled",
        "skipped_count": 1,
    }


@pytest.mark.asyncio
async def test_fetch_skips_auto_score_for_read_only_accounts(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1", "execution_mode": "read_only"}],
        settings_overrides={"ai_enabled": True},
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="25-ro",
                    author_handle="@openai",
                    text="Read only fetch should not call AI.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    assert context.fake_llm.decision_calls == 0
    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    assert tweets[0].interaction_state == "unscored"
    async with context.session_factory() as session:
        skip_log = (
            await session.execute(
                select(AILogRecord)
                .where(AILogRecord.account_id == "acct1", AILogRecord.log_type == "auto_score_skip")
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
        decision_logs = (
            await session.execute(
                select(AILogRecord).where(
                    AILogRecord.account_id == "acct1",
                    AILogRecord.log_type == "decision",
                )
            )
        ).scalars().all()
    assert skip_log.status == "skipped"
    assert skip_log.response_payload == {
        "reason": "read_only",
        "skipped_count": 1,
    }
    assert decision_logs == []


@pytest.mark.asyncio
async def test_single_account_failure_isolated_from_other_accounts(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1"}, {"id": "acct2"}],
        settings_overrides={"pause_after_failures": 1},
    )

    context.fake_source.set_failure(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        httpx.ProxyError("proxy dead", request=httpx.Request("GET", "https://x.com")),
    )
    context.fake_source.add_batch(
        "acct2",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(items=[NormalizedTweet(tweet_id="10", text="ok")], next_cursor=None),
    )

    failed = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    succeeded = await context.container.fetch_service.fetch_account("acct2", trigger="manual")

    assert failed.status == "failed"
    assert failed.error_code == FetchErrorCode.PROXY_FAILED
    assert succeeded.status == "success"
    assert await context.tweet_count("acct2") == 1

    failed_state = await context.get_state("acct1")
    healthy_state = await context.get_state("acct2")

    assert failed_state.lifecycle_status == AccountLifecycleStatus.PAUSED
    assert failed_state.pause_reason == PauseReason.PROXY_FAILED
    assert failed_state.proxy_health == ProxyHealth.UNHEALTHY
    assert healthy_state.lifecycle_status == AccountLifecycleStatus.ENABLED
    assert healthy_state.failure_streak == 0


@pytest.mark.asyncio
async def test_validate_session_marks_auth_or_proxy_failures(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}, {"id": "acct2"}])

    context.fake_source.set_validation(
        "acct1",
        SessionValidationResult(ok=False, detail="expired", error_code=FetchErrorCode.AUTH_EXPIRED),
    )
    context.fake_source.set_validation(
        "acct2",
        SessionValidationResult(
            ok=False,
            detail="proxy bad",
            error_code=FetchErrorCode.PROXY_FAILED,
        ),
    )

    await context.container.fetch_service.validate_session("acct1")
    await context.container.fetch_service.validate_session("acct2")

    auth_state = await context.get_state("acct1")
    proxy_state = await context.get_state("acct2")

    assert auth_state.pause_reason == PauseReason.AUTH_EXPIRED
    assert auth_state.lifecycle_status == AccountLifecycleStatus.PAUSED
    assert proxy_state.pause_reason == PauseReason.PROXY_FAILED
    assert proxy_state.proxy_health == ProxyHealth.UNHEALTHY


@pytest.mark.asyncio
async def test_validate_session_skips_admin_disabled_account(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    await context.container.account_service.disable_account("acct1")

    result = await context.container.fetch_service.validate_session("acct1")

    assert result.ok is False
    assert result.detail == "account is admin disabled"


@pytest.mark.asyncio
async def test_list_due_accounts_respects_quiet_hours(make_test_context) -> None:
    current_hour = datetime.now(UTC).hour
    next_hour = (current_hour + 1) % 24
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "fetch_schedule": {
                    "base_interval_minutes": 1,
                    "interval_jitter_minutes": 0,
                    "quiet_hours": [current_hour, next_hour],
                },
            }
        ],
        settings_overrides={"app_timezone": "UTC"},
    )

    due_accounts = await context.container.fetch_service.list_due_accounts()

    assert due_accounts == []


@pytest.mark.asyncio
async def test_fetch_success_sets_next_fetch_not_before_with_jitter(
    make_test_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "fetch_schedule": {
                    "base_interval_minutes": 10,
                    "interval_jitter_minutes": 3,
                    "quiet_hours": None,
                },
            }
        ]
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[NormalizedTweet(tweet_id="42", text="scheduled")],
            next_cursor=None,
        ),
    )
    monkeypatch.setattr("app.fetching.service.random.randint", lambda _start, _end: 2)

    result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    assert result.status == "success"
    state = await context.get_state("acct1")
    assert state.last_fetch_finished_at is not None
    assert state.next_fetch_not_before_at is not None
    delta = state.next_fetch_not_before_at - state.last_fetch_finished_at
    assert delta == timedelta(minutes=12)
