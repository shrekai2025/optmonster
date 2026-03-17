from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.actions.schemas import (
    ActionDecisionRequest,
    ActionModifyRequest,
    FollowActionCreateRequest,
    GenerateReplyRequest,
    LikeActionCreateRequest,
    ReplyApprovalCreateRequest,
)
from app.fetching.schemas import FetchBatchResult, NormalizedTweet
from app.llm.schemas import DecisionResult
from app.main import create_app
from app.runtime.enums import ActionErrorCode, ActionStatus, ActionType, ExecutionMode, SourceType
from app.runtime.models import AILogRecord
from app.runtime.settings import Settings


@pytest.mark.asyncio
async def test_reply_approval_flow_executes_in_dry_run(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1", "execution_mode": "dry_run"}],
        settings_overrides={"action_interval_jitter_seconds": 0},
    )

    created = await context.container.action_service.create_reply_request(
        ReplyApprovalCreateRequest(
            account_id="acct1",
            target_tweet_id="tweet-1",
            target_user_handle="@openai",
            content_draft="Interesting angle on open models.",
        )
    )
    assert created.status == ActionStatus.PENDING_APPROVAL

    approved = await context.container.action_service.approve_action(
        created.id,
        ActionDecisionRequest(),
    )
    assert approved.status == ActionStatus.APPROVED

    result = await context.container.action_service.process_action(created.id)
    assert result is not None
    assert result.status == ActionStatus.SUCCEEDED
    assert result.applied_execution_mode == ExecutionMode.DRY_RUN
    assert await context.action_count("acct1", status=ActionStatus.SUCCEEDED) == 1

    used = await context.container.runtime_coordinator.get_daily_action_count(
        "acct1",
        ActionType.REPLY,
        datetime.now(UTC),
    )
    assert used == 1


@pytest.mark.asyncio
async def test_read_only_like_request_is_blocked_at_execution(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "read_only"}])

    created = await context.container.action_service.create_like_request(
        LikeActionCreateRequest(
            account_id="acct1",
            target_tweet_id="tweet-1",
            target_user_handle="@openai",
        )
    )
    assert created.status == ActionStatus.APPROVED

    result = await context.container.action_service.process_action(created.id)
    assert result is not None
    assert result.status == ActionStatus.FAILED
    assert result.error_code == ActionErrorCode.EXECUTION_MODE_BLOCKED


@pytest.mark.asyncio
async def test_actions_respect_active_hours_and_admin_disable(make_test_context) -> None:
    current_hour = datetime.now(UTC).hour
    blocked_hour = (current_hour + 2) % 24
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "execution_mode": "dry_run",
                "behavior_budget": {
                    "daily_likes_max": 30,
                    "daily_replies_max": 8,
                    "daily_follows_max": 5,
                    "active_hours": [blocked_hour, blocked_hour],
                    "min_interval_minutes": 15,
                },
            },
            {"id": "acct2", "execution_mode": "dry_run"},
        ],
        settings_overrides={"action_interval_jitter_seconds": 0},
    )

    outside_hours = await context.container.action_service.create_like_request(
        LikeActionCreateRequest(account_id="acct1", target_tweet_id="tweet-1")
    )
    outside_result = await context.container.action_service.process_action(outside_hours.id)
    assert outside_result is not None
    assert outside_result.status == ActionStatus.FAILED
    assert outside_result.error_code == ActionErrorCode.OUTSIDE_ACTIVE_HOURS

    disabled = await context.container.action_service.create_like_request(
        LikeActionCreateRequest(account_id="acct2", target_tweet_id="tweet-2")
    )
    await context.container.account_service.disable_account("acct2")
    disabled_result = await context.container.action_service.process_action(disabled.id)
    assert disabled_result is not None
    assert disabled_result.status == ActionStatus.FAILED
    assert disabled_result.error_code == ActionErrorCode.ADMIN_DISABLED


@pytest.mark.asyncio
async def test_action_admin_routes_support_approval_and_history(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1", "execution_mode": "dry_run"}],
        settings_overrides={"action_interval_jitter_seconds": 0},
    )

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        create_response = await client.post(
            "/admin/actions/replies",
            json={
                "account_id": "acct1",
                "target_tweet_id": "tweet-1",
                "target_user_handle": "@openai",
                "content_draft": "Worth discussing further.",
            },
        )
        assert create_response.status_code == 201
        approval_id = create_response.json()["id"]

        approvals_response = await client.get("/admin/approvals")
        assert approvals_response.status_code == 200
        assert approvals_response.json()[0]["id"] == approval_id

        approve_response = await client.post(
            f"/admin/approvals/{approval_id}/approve",
            json={},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["status"] == "approved"

        await context.container.action_service.process_action(approval_id)

        actions_response = await client.get("/admin/actions", params={"status": "succeeded"})
        assert actions_response.status_code == 200
        assert actions_response.json()[0]["id"] == approval_id


@pytest.mark.asyncio
async def test_generate_modify_and_execute_live_reply_updates_writing_guide(
    make_test_context,
) -> None:
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "execution_mode": "live",
                "search_keywords": ["governance"],
                "behavior_budget": {
                    "daily_likes_max": 30,
                    "daily_replies_max": 8,
                    "daily_follows_max": 5,
                    "active_hours": [0, 23],
                    "min_interval_minutes": 0,
                },
            }
        ],
        settings_overrides={"action_interval_jitter_seconds": 0},
    )
    tweet_text = "Governance upgrades should reduce coordination overhead for rollups."
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="200",
                    author_handle="@builder",
                    text=tweet_text,
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    tweet = tweets[0]
    context.fake_llm.set_decision(
        "acct1",
        tweet_text,
        result=DecisionResult(
            relevance_score=9,
            like=True,
            reply_draft="AI draft reply.",
            reply_confidence=8,
            rationale="high relevance",
        ),
    )
    context.settings.ai_enabled = True

    generated = await context.container.action_service.generate_reply_for_tweet(
        tweet.id,
        GenerateReplyRequest(account_id="acct1"),
    )
    assert generated.like_action is not None
    assert generated.reply_action is not None
    assert generated.reply_action.status == ActionStatus.PENDING_APPROVAL

    like_result = await context.container.action_service.process_action(generated.like_action.id)
    assert like_result is not None
    assert like_result.status == ActionStatus.SUCCEEDED

    modified = await context.container.action_service.modify_action(
        generated.reply_action.id,
        ActionModifyRequest(final_draft="Human final reply."),
    )
    assert modified.status == ActionStatus.APPROVED

    reply_result = await context.container.action_service.process_action(generated.reply_action.id)
    assert reply_result is not None
    assert reply_result.status == ActionStatus.SUCCEEDED
    assert reply_result.learning_status == "applied"
    assert context.fake_executor.likes == [("acct1", "200")]
    assert context.fake_executor.replies == [("acct1", "200", "Human final reply.")]

    guide_text = (context.settings.resolve_path("config/writing_guides/acct1.md")).read_text(
        encoding="utf-8"
    )
    assert "Human final reply." in guide_text
    assert "AI draft reply." in guide_text


@pytest.mark.asyncio
async def test_follow_request_executes_live(make_test_context) -> None:
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "execution_mode": "live",
                "behavior_budget": {
                    "daily_likes_max": 30,
                    "daily_replies_max": 8,
                    "daily_follows_max": 5,
                    "active_hours": [0, 23],
                    "min_interval_minutes": 0,
                },
            }
        ],
        settings_overrides={"action_interval_jitter_seconds": 0},
    )

    created = await context.container.action_service.create_follow_request(
        FollowActionCreateRequest(
            account_id="acct1",
            target_user_handle="@openai",
        )
    )
    assert created.status == ActionStatus.APPROVED

    result = await context.container.action_service.process_action(created.id)
    assert result is not None
    assert result.status == ActionStatus.SUCCEEDED
    assert result.applied_execution_mode == ExecutionMode.LIVE
    assert context.fake_executor.follows == [("acct1", "@openai")]


@pytest.mark.asyncio
async def test_generate_reply_route_returns_actions_and_tweet_detail(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "dry_run"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="201",
                    author_handle="@openai",
                    text="Open models need sharper product loops.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    context.settings.ai_enabled = True

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tweets_response = await client.get("/admin/tweets")
        assert tweets_response.status_code == 200
        tweet_id = tweets_response.json()[0]["id"]

        generate_response = await client.post(
            f"/admin/tweets/{tweet_id}/reply/generate",
            json={"account_id": "acct1"},
        )
        assert generate_response.status_code == 200
        generated = generate_response.json()
        assert generated["decision"]["reply_draft"] is not None
        assert generated["reply_action"]["status"] == "pending_approval"

        detail_response = await client.get(f"/admin/tweets/{tweet_id}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["actions"][0]["action_type"] in {"reply", "like"}


@pytest.mark.asyncio
async def test_generate_reply_route_is_blocked_for_read_only_accounts(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "read_only"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="201-ro",
                    author_handle="@openai",
                    text="Read only accounts should not trigger AI review.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    context.settings.ai_enabled = True

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        tweets_response = await client.get("/admin/tweets")
        assert tweets_response.status_code == 200
        tweet_id = tweets_response.json()[0]["id"]

        generate_response = await client.post(
            f"/admin/tweets/{tweet_id}/reply/generate",
            json={"account_id": "acct1"},
        )
        assert generate_response.status_code == 400
        assert generate_response.json()["detail"] == "AI review is disabled for read_only accounts"

    assert context.fake_llm.decision_calls == 0


@pytest.mark.asyncio
async def test_reply_workspace_route_returns_pending_reply_items(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "dry_run"}])
    context.settings.ai_enabled = True
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="206",
                    author_handle="@openai",
                    text="A pending reply should appear in the reply workspace.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin/reply-workspace")
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        assert items[0]["account_id"] == "acct1"
        assert items[0]["tweet_text"] == "A pending reply should appear in the reply workspace."
        assert items[0]["ai_draft"] is not None


@pytest.mark.asyncio
async def test_generate_reply_reuses_existing_actions_for_same_tweet(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "dry_run"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="202",
                    author_handle="@openai",
                    text="Reasoning models need stronger eval loops.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    context.settings.ai_enabled = True
    tweet = (await context.container.account_service.list_tweets(account_id="acct1"))[0]

    first = await context.container.action_service.generate_reply_for_tweet(
        tweet.id,
        GenerateReplyRequest(account_id="acct1"),
    )
    second = await context.container.action_service.generate_reply_for_tweet(
        tweet.id,
        GenerateReplyRequest(account_id="acct1"),
    )

    assert first.like_action is not None and second.like_action is not None
    assert first.reply_action is not None and second.reply_action is not None
    assert first.like_action.id == second.like_action.id
    assert first.reply_action.id == second.reply_action.id


@pytest.mark.asyncio
async def test_duplicate_rows_from_for_you_and_hot_reuse_same_actions(make_test_context) -> None:
    context = await make_test_context(
        accounts=[
            {
                "id": "acct1",
                "execution_mode": "dry_run",
                "timeline": False,
                "timeline_recommended": True,
                "timeline_popular": True,
            }
        ],
    )
    shared_text = "The same tweet can arrive from both For You and Hot feeds."
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE_RECOMMENDED,
        "home_for_you",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="dup-301",
                    author_handle="@builder",
                    text=shared_text,
                    created_at=datetime.now(UTC),
                    view_count=180000,
                    like_count=900,
                    retweet_count=120,
                    reply_count=40,
                )
            ],
            next_cursor=None,
        ),
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE_POPULAR,
        "home_popular",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="dup-301",
                    author_handle="@builder",
                    text=shared_text,
                    created_at=datetime.now(UTC),
                    view_count=180000,
                    like_count=900,
                    retweet_count=120,
                    reply_count=40,
                )
            ],
            next_cursor=None,
        ),
    )
    fetch_result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    assert fetch_result.status == "success"

    tweets = await context.container.account_service.list_tweets(account_id="acct1")
    assert len(tweets) == 2
    assert {tweet.source_type for tweet in tweets} == {
        SourceType.TIMELINE_RECOMMENDED,
        SourceType.TIMELINE_POPULAR,
    }

    context.settings.ai_enabled = True
    context.fake_llm.set_decision(
        "acct1",
        shared_text,
        DecisionResult(
            relevance_score=8,
            like=True,
            reply_draft="One concrete reply.",
            reply_confidence=7,
            rationale="same tweet across feeds",
        ),
    )

    first = await context.container.action_service.generate_reply_for_tweet(
        tweets[0].id,
        GenerateReplyRequest(account_id="acct1"),
    )
    second = await context.container.action_service.generate_reply_for_tweet(
        tweets[1].id,
        GenerateReplyRequest(account_id="acct1"),
    )

    assert first.like_action is not None and second.like_action is not None
    assert first.reply_action is not None and second.reply_action is not None
    assert first.like_action.id == second.like_action.id
    assert first.reply_action.id == second.reply_action.id
    assert context.fake_llm.decision_calls == 1


@pytest.mark.asyncio
async def test_same_tweet_reuses_successful_decision_across_accounts(make_test_context) -> None:
    context = await make_test_context(
        accounts=[
            {"id": "acct1", "execution_mode": "dry_run"},
            {"id": "acct2", "execution_mode": "dry_run"},
        ],
    )
    shared_text = "Shared tweet should only be evaluated once."
    context.settings.ai_enabled = True
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="shared-201",
                    author_handle="@openai",
                    text=shared_text,
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    context.fake_source.add_batch(
        "acct2",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="shared-201",
                    author_handle="@openai",
                    text=shared_text,
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    context.fake_llm.set_decision(
        "acct1",
        shared_text,
        DecisionResult(
            relevance_score=6,
            like=False,
            reply_draft=None,
            reply_confidence=3,
            rationale="shared evaluation",
        ),
    )

    first = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    second = await context.container.fetch_service.fetch_account("acct2", trigger="manual")

    assert first.status == "success"
    assert second.status == "success"
    assert context.fake_llm.decision_calls == 1

    acct2_tweet = (await context.container.account_service.list_tweets(account_id="acct2"))[0]
    assert acct2_tweet.interaction_state == "scored_no_action"
    assert acct2_tweet.latest_decision is not None
    assert acct2_tweet.latest_decision.relevance_score == 6

    async with context.session_factory() as session:
        reused_log = (
            await session.execute(
                select(AILogRecord)
                .where(
                    AILogRecord.account_id == "acct2",
                    AILogRecord.fetched_tweet_id == acct2_tweet.id,
                    AILogRecord.log_type == "decision",
                )
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
    assert reused_log.response_payload["reused"] is True
    assert reused_log.response_payload["source_account_id"] == "acct1"


@pytest.mark.asyncio
async def test_scored_without_actions_is_visible_in_tweet_list(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "dry_run"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="203",
                    author_handle="@openai",
                    text="Ship evaluation loops before scaling agents.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    tweet = (await context.container.account_service.list_tweets(account_id="acct1"))[0]
    context.fake_llm.set_decision(
        "acct1",
        "Ship evaluation loops before scaling agents.",
        result=DecisionResult(
            relevance_score=7,
            like=False,
            reply_draft=None,
            reply_confidence=3,
            rationale="worth tracking but no action",
        ),
    )
    context.settings.ai_enabled = True

    await context.container.action_service.generate_reply_for_tweet(
        tweet.id,
        GenerateReplyRequest(account_id="acct1"),
    )

    refreshed = (await context.container.account_service.list_tweets(account_id="acct1"))[0]
    assert refreshed.interaction_state == "scored_no_action"
    assert refreshed.latest_decision is not None
    assert refreshed.latest_decision.relevance_score == 7


@pytest.mark.asyncio
async def test_manual_generate_is_blocked_when_ai_disabled(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1", "execution_mode": "dry_run"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="204",
                    author_handle="@openai",
                    text="AI should stay off until toggled on.",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    tweet = (await context.container.account_service.list_tweets(account_id="acct1"))[0]

    with pytest.raises(ValueError, match="AI is disabled"):
        await context.container.action_service.generate_reply_for_tweet(
            tweet.id,
            GenerateReplyRequest(account_id="acct1"),
        )
