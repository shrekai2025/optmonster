from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.fetching.schemas import FetchBatchResult, NormalizedTweet
from app.llm.schemas import DecisionResult
from app.main import create_app
from app.runtime.enums import AccountLifecycleStatus, PauseReason, SourceType
from app.runtime.models import AccountFollowerSnapshot, AILogRecord
from app.runtime.settings import Settings


@pytest.mark.asyncio
async def test_reload_configs_marks_removed_accounts_and_masks_proxy(make_test_context) -> None:
    context = await make_test_context(
        accounts=[
            {"id": "acct1"},
            {
                "id": "acct2",
                "proxy": {"url": "http://user:pass@127.0.0.1:8080"},
            },
        ]
    )

    accounts = await context.container.account_service.list_accounts(
        fetch_limit_default=context.settings.fetch_limit_default
    )
    assert {item.id for item in accounts} == {"acct1", "acct2"}

    proxied = next(item for item in accounts if item.id == "acct2")
    assert proxied.proxy_enabled is True
    assert proxied.proxy_url_masked == "http://***@127.0.0.1:8080"

    (context.config_dir / "acct1.yaml").unlink()
    summary = await context.container.account_service.reload_configs()

    assert summary.loaded_accounts == 1
    assert summary.removed_accounts == 1

    removed_state = await context.get_state("acct1")
    assert removed_state.lifecycle_status == AccountLifecycleStatus.PAUSED
    assert removed_state.pause_reason == PauseReason.CONFIG_REMOVED


@pytest.mark.asyncio
async def test_admin_routes_list_accounts_and_enqueue_fetch(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        list_response = await client.get("/admin/accounts")
        assert list_response.status_code == 200
        assert list_response.json()[0]["id"] == "acct1"

        enqueue_response = await client.post("/admin/accounts/acct1/fetch-now")
        assert enqueue_response.status_code == 200
        assert enqueue_response.json() == {
            "account_id": "acct1",
            "enqueued": True,
            "detail": None,
        }


@pytest.mark.asyncio
async def test_admin_can_disable_and_enable_account(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        disable_response = await client.post("/admin/accounts/acct1/disable")
        assert disable_response.status_code == 200
        assert disable_response.json() == {
            "account_id": "acct1",
            "lifecycle_status": "paused",
            "pause_reason": "admin_disabled",
        }

        enqueue_response = await client.post("/admin/accounts/acct1/fetch-now")
        assert enqueue_response.status_code == 200
        assert enqueue_response.json() == {
            "account_id": "acct1",
            "enqueued": False,
            "detail": "account_paused:admin_disabled",
        }

        disabled_state = await context.get_state("acct1")
        assert disabled_state.lifecycle_status == AccountLifecycleStatus.PAUSED
        assert disabled_state.pause_reason == PauseReason.ADMIN_DISABLED

        enable_response = await client.post("/admin/accounts/acct1/enable")
        assert enable_response.status_code == 200
        assert enable_response.json() == {
            "account_id": "acct1",
            "lifecycle_status": "enabled",
            "pause_reason": "none",
        }

        enabled_state = await context.get_state("acct1")
        assert enabled_state.lifecycle_status == AccountLifecycleStatus.ENABLED
        assert enabled_state.pause_reason == PauseReason.NONE


@pytest.mark.asyncio
async def test_reload_configs_preserves_admin_disabled_accounts(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    await context.container.account_service.disable_account("acct1")

    summary = await context.container.account_service.reload_configs()

    assert summary.loaded_accounts == 1
    state = await context.get_state("acct1")
    assert state.lifecycle_status == AccountLifecycleStatus.PAUSED
    assert state.pause_reason == PauseReason.ADMIN_DISABLED


@pytest.mark.asyncio
async def test_dashboard_and_tweets_routes_return_console_data(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="100",
                    author_handle="@openai",
                    text="hello dashboard",
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
        dashboard_response = await client.get("/admin/dashboard")
        assert dashboard_response.status_code == 200
        dashboard = dashboard_response.json()
        assert dashboard["summary"]["total_accounts"] == 1
        assert dashboard["summary"]["total_tweets"] == 1
        assert dashboard["runtime_settings"]["llm_provider"] == "mock"
        assert dashboard["accounts"][0]["tweet_count"] == 1
        assert len(dashboard["accounts"][0]["budgets"]) == 3
        assert dashboard["recent_operations"][0]["status"] == "success"

        tweets_response = await client.get("/admin/tweets", params={"account_id": "acct1"})
        assert tweets_response.status_code == 200
        tweets = tweets_response.json()
        assert tweets[0]["id"] > 0
        assert tweets[0]["tweet_id"] == "100"
        assert tweets[0]["tweet_url"] == "https://x.com/i/status/100"
        assert tweets[0]["interaction_state"] == "unscored"


@pytest.mark.asyncio
async def test_tweet_detail_prefers_full_text_from_raw_payload(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="150",
                    author_handle="@openai",
                    text="Short preview...",
                    created_at=datetime.now(UTC),
                    raw_payload={
                        "legacy": {
                            "full_text": "This is the full tweet content restored from raw payload."
                        }
                    },
                )
            ],
            next_cursor=None,
        ),
    )
    fetch_result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    assert fetch_result.status == "success"

    tweet = (await context.container.account_service.list_tweets(account_id="acct1"))[0]
    detail = await context.container.account_service.get_tweet_detail(tweet.id)
    assert detail.text == "This is the full tweet content restored from raw payload."


@pytest.mark.asyncio
async def test_account_config_includes_recent_behavior_logs(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[NormalizedTweet(tweet_id="151", text="hello logs")],
            next_cursor=None,
        ),
    )
    fetch_result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    assert fetch_result.status == "success"

    config_doc = await context.container.account_service.get_account_config("acct1")
    assert config_doc.recent_operations
    assert config_doc.recent_operations[0].operation_type == "fetch"
    assert config_doc.recent_operations[0].status == "success"


@pytest.mark.asyncio
async def test_tweet_cleanup_route_removes_stale_and_filtered_tweets(
    make_test_context,
) -> None:
    now = datetime.now(UTC)
    context = await make_test_context(
        accounts=[{"id": "acct1"}],
        settings_overrides={
            "fetch_recent_window_hours": 24,
            "fetch_include_replies": True,
            "fetch_include_retweets": True,
        },
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(tweet_id="401", text="keep me", created_at=now),
                NormalizedTweet(
                    tweet_id="402",
                    text="too old",
                    created_at=now - timedelta(hours=5),
                ),
                NormalizedTweet(
                    tweet_id="403",
                    text="reply",
                    created_at=now,
                    is_reply=True,
                    raw_payload={"legacy": {"in_reply_to_status_id_str": "400"}},
                ),
                NormalizedTweet(
                    tweet_id="404",
                    text="RT @someone rt something",
                    created_at=now,
                    is_retweet=True,
                    raw_payload={"legacy": {"retweeted_status_result": {"result": "ok"}}},
                ),
            ],
            next_cursor=None,
        ),
    )
    fetch_result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    assert fetch_result.status == "success"

    context.settings.fetch_recent_window_hours = 2
    context.settings.fetch_include_replies = False
    context.settings.fetch_include_retweets = False

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        cleanup_response = await client.post(
            "/admin/tweets/cleanup",
            json={"account_id": "acct1"},
        )
        assert cleanup_response.status_code == 200
        payload = cleanup_response.json()
        assert payload["deleted_tweets"] == 3
        assert payload["deleted_outside_window_tweets"] == 1
        assert payload["deleted_filtered_reply_tweets"] == 1
        assert payload["deleted_filtered_retweet_tweets"] == 1

        tweets_response = await client.get("/admin/tweets", params={"account_id": "acct1"})
        assert tweets_response.status_code == 200
        tweets = tweets_response.json()
        assert [tweet["tweet_id"] for tweet in tweets] == ["401"]


@pytest.mark.asyncio
async def test_tweet_backfill_ai_route_scores_recent_unscored_tweets(
    make_test_context,
) -> None:
    now = datetime.now(UTC)
    context = await make_test_context(
        accounts=[{"id": "acct1", "execution_mode": "dry_run"}],
        settings_overrides={"ai_enabled": False},
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="451",
                    author_handle="@openai",
                    text="Backfill this tweet.",
                    created_at=now,
                )
            ],
            next_cursor=None,
        ),
    )
    fetch_result = await context.container.fetch_service.fetch_account("acct1", trigger="manual")
    assert fetch_result.status == "success"

    context.settings.ai_enabled = True
    context.fake_llm.set_decision(
        "acct1",
        "Backfill this tweet.",
        DecisionResult(
            relevance_score=6,
            like=False,
            reply_draft=None,
            reply_confidence=2,
            rationale="backfilled",
        ),
    )

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        backfill_response = await client.post(
            "/admin/tweets/backfill-ai",
            json={"account_id": "acct1"},
        )
        assert backfill_response.status_code == 200
        payload = backfill_response.json()
        assert payload["candidate_tweets"] == 1
        assert payload["scored_tweets"] == 1
        assert payload["failed_tweets"] == 0

        tweets_response = await client.get("/admin/tweets", params={"account_id": "acct1"})
        assert tweets_response.status_code == 200
        tweets = tweets_response.json()
        assert tweets[0]["interaction_state"] == "scored_no_action"

    async with context.session_factory() as session:
        backfill_log = (
            await session.execute(
                select(AILogRecord)
                .where(
                    AILogRecord.account_id == "acct1",
                    AILogRecord.log_type == "auto_score_batch",
                )
                .order_by(AILogRecord.id.desc())
            )
        ).scalar_one()
    assert backfill_log.request_payload["trigger_source"] == "auto_backfill_scoring"


@pytest.mark.asyncio
async def test_console_page_is_served(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/console")
        assert response.status_code == 200
        assert "OptMonster Console" in response.text
        tweet_console = await client.get("/console/tweets")
        assert tweet_console.status_code == 200
        assert "OptMonster Tweet Workspace" in tweet_console.text
        reply_console = await client.get("/console/replies")
        assert reply_console.status_code == 200
        assert "OptMonster Reply Workspace" in reply_console.text
        ai_console = await client.get("/console/ai")
        assert ai_console.status_code == 200
        assert "OptMonster AI Settings" in ai_console.text
        ai_logs_console = await client.get("/console/ai/logs")
        assert ai_logs_console.status_code == 200
        assert "OptMonster AI Logs" in ai_logs_console.text
        account_console = await client.get("/console/accounts/acct1")
        assert account_console.status_code == 200
        assert "OptMonster Account Detail" in account_console.text


@pytest.mark.asyncio
async def test_account_config_route_updates_yaml_and_runtime(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1", "search_keywords": ["old keyword"]}]
    )

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        config_response = await client.get("/admin/accounts/acct1/config")
        assert config_response.status_code == 200
        payload = config_response.json()["account"]
        payload["persona"]["name"] = "Editor"
        payload["targets"]["follow_users_enabled"] = False
        payload["targets"]["search_keywords_enabled"] = False
        payload["targets"]["search_keywords"] = [{"query": "new keyword", "count": 12}]
        payload["fetch_schedule"] = {
            "base_interval_minutes": 19,
            "interval_jitter_minutes": 4,
            "quiet_hours": [1, 7],
        }
        payload["behavior_budget"]["daily_replies_max"] = 4

        update_response = await client.put("/admin/accounts/acct1/config", json=payload)
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["account"]["account"]["persona"]["name"] == "Editor"
        assert updated["account"]["account"]["targets"]["follow_users_enabled"] is False
        assert updated["account"]["account"]["targets"]["search_keywords_enabled"] is False
        assert updated["account"]["account"]["fetch_schedule"]["base_interval_minutes"] == 19
        assert updated["account"]["account"]["fetch_schedule"]["interval_jitter_minutes"] == 4
        assert updated["account"]["account"]["behavior_budget"]["daily_replies_max"] == 4
        assert (
            updated["account"]["account"]["targets"]["search_keywords"][0]["query"]
            == "new keyword"
        )

    yaml_payload = (context.config_dir / "acct1.yaml").read_text(encoding="utf-8")
    assert "Editor" in yaml_payload
    assert "new keyword" in yaml_payload
    assert "follow_users_enabled: false" in yaml_payload
    assert "search_keywords_enabled: false" in yaml_payload
    assert "base_interval_minutes: 19" in yaml_payload
    assert "- 1" in yaml_payload
    accounts = await context.container.account_service.list_accounts(
        fetch_limit_default=context.settings.fetch_limit_default
    )
    assert len(accounts[0].fetch_sources) == 1
    assert accounts[0].fetch_sources[0].source_type == SourceType.TIMELINE
    assert accounts[0].fetch_sources[0].source_key == "home_following"


@pytest.mark.asyncio
async def test_account_delete_route_removes_account_files_and_dashboard_entry(
    make_test_context,
) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    guide_path = context.settings.resolve_path("config/writing_guides/acct1.md")
    guide_path.parent.mkdir(parents=True, exist_ok=True)
    guide_path.write_text("guide", encoding="utf-8")

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        delete_response = await client.delete("/admin/accounts/acct1")
        assert delete_response.status_code == 200
        deleted = delete_response.json()
        assert deleted["deleted_config_file"] is True
        assert deleted["deleted_cookie_file"] is True
        assert deleted["deleted_writing_guide_file"] is True

        accounts_response = await client.get("/admin/accounts")
        assert accounts_response.status_code == 200
        assert accounts_response.json() == []

        dashboard_response = await client.get("/admin/dashboard")
        assert dashboard_response.status_code == 200
        assert dashboard_response.json()["summary"]["total_accounts"] == 0

    assert not (context.config_dir / "acct1.yaml").exists()
    assert not (context.cookie_dir / "acct1.json").exists()
    assert not guide_path.exists()


@pytest.mark.asyncio
async def test_dashboard_includes_follower_history_and_delta(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    now = datetime.now(UTC)
    context.fake_source.set_profile("acct1", follower_count=120, twitter_handle="@acct1")

    async with context.session_factory() as session:
        session.add(
            AccountFollowerSnapshot(
                account_id="acct1",
                snapshot_date=(now - timedelta(days=1)).date(),
                follower_count=100,
                captured_at=now - timedelta(days=1),
            )
        )
        await session.commit()

    await context.container.fetch_service.validate_session("acct1")

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/admin/dashboard")
        assert response.status_code == 200
        account = response.json()["accounts"][0]
        assert account["follower_count"] == 120
        assert account["follower_delta"] == 20
        assert len(account["follower_history"]) == 2
        assert account["follower_history"][-1]["follower_count"] == 120


@pytest.mark.asyncio
async def test_tweet_detail_exposes_author_coverage_and_follow_target_route(
    make_test_context,
) -> None:
    context = await make_test_context(
        accounts=[
            {"id": "acct1", "follow_users": ["@builder"]},
            {"id": "acct2", "execution_mode": "dry_run"},
        ],
        settings_overrides={"action_interval_jitter_seconds": 0},
    )
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="301",
                    author_handle="@builder",
                    text="Builders need cleaner product feedback loops.",
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
        tweets_response = await client.get("/admin/tweets")
        tweet_id = tweets_response.json()[0]["id"]

        detail_response = await client.get(f"/admin/tweets/{tweet_id}")
        assert detail_response.status_code == 200
        coverage = {item["account_id"]: item for item in detail_response.json()["author_coverage"]}
        assert coverage["acct1"]["follows_author"] is True
        assert coverage["acct2"]["follows_author"] is False

        follow_response = await client.post(f"/admin/tweets/{tweet_id}/follow-targets/acct2")
        assert follow_response.status_code == 200
        assert follow_response.json()["added_to_follow_scope"] is True

        updated_detail = await client.get(f"/admin/tweets/{tweet_id}")
        updated_coverage = {
            item["account_id"]: item for item in updated_detail.json()["author_coverage"]
        }
        assert updated_coverage["acct2"]["follows_author"] is True

    yaml_payload = (context.config_dir / "acct2.yaml").read_text(encoding="utf-8")
    assert "@builder" in yaml_payload


@pytest.mark.asyncio
async def test_cookie_import_routes_create_new_account_from_netscape_cookie_file(
    make_test_context,
    tmp_path: Path,
) -> None:
    import_dir = tmp_path / "imports"
    import_dir.mkdir(parents=True, exist_ok=True)
    source_file = import_dir / "MavaeAI.txt"
    source_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                ".x.com\tTRUE\t/\tTRUE\t1807924863\tauth_token\tsecret-auth-token",
                ".x.com\tTRUE\t/\tTRUE\t1807924863\tct0\tsecret-ct0",
                ".x.com\tTRUE\t/\tTRUE\t1807924863\tkdt\tsecret-kdt",
            ]
        ),
        encoding="utf-8",
    )

    context = await make_test_context(
        accounts=[],
        settings_overrides={"cookie_import_dir": import_dir},
    )

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        candidates_response = await client.get("/admin/cookie-import/candidates")
        assert candidates_response.status_code == 200
        candidates = candidates_response.json()
        assert candidates[0]["source_file"] == "MavaeAI.txt"
        assert candidates[0]["has_auth_token"] is True
        assert candidates[0]["has_ct0"] is True

        import_response = await client.post(
            "/admin/cookie-import/accounts",
            json={
                "source_file": "MavaeAI.txt",
                "extra_yaml": (
                    "targets:\n"
                    "  search_keywords:\n"
                    "    - query: ai infra\n"
                    "      count: 12\n"
                ),
            },
        )
        assert import_response.status_code == 201
        imported = import_response.json()
        assert imported["account"]["account"]["id"] == "mavaeai"
        assert imported["validation_ok"] is True

        accounts_response = await client.get("/admin/accounts")
        assert accounts_response.status_code == 200
        assert accounts_response.json()[0]["id"] == "mavaeai"

    cookie_payload = json.loads(
        (context.cookie_dir / "mavaeai.json").read_text(encoding="utf-8")
    )
    assert cookie_payload["auth_token"] == "secret-auth-token"
    yaml_payload = (context.config_dir / "mavaeai.yaml").read_text(encoding="utf-8")
    assert "ai infra" in yaml_payload


@pytest.mark.asyncio
async def test_runtime_settings_route_updates_env_file_and_dashboard(
    make_test_context,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "APP_ENV=dev",
                "AI_ENABLED=false",
                "FETCH_RECENT_WINDOW_HOURS=24",
                "FETCH_LATEST_FIRST=true",
                "FETCH_INCLUDE_REPLIES=true",
                "FETCH_INCLUDE_RETWEETS=true",
                "LLM_PROVIDER=mock",
                "LLM_BASE_URL=",
                "LLM_API_KEY=",
                "LLM_MODEL_ID=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    context = await make_test_context(
        accounts=[{"id": "acct1"}],
        settings_overrides={"app_env_file": env_file},
    )

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        update_response = await client.put(
            "/admin/runtime-settings",
            json={
                "ai_enabled": True,
                "fetch_recent_window_hours": 18,
                "fetch_latest_first": True,
                "fetch_include_replies": False,
                "fetch_include_retweets": False,
                "llm_provider": "openai_compatible",
                "llm_base_url": "https://llm.example/v1",
                "llm_model_id": "vendor/model-1",
                "llm_api_key": "sk-secret-1234",
                "replace_api_key": True,
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["runtime_settings"]["current_env_file"] == str(env_file)
        assert updated["runtime_settings"]["ai_enabled"] is True
        assert updated["runtime_settings"]["fetch_recent_window_hours"] == 18
        assert updated["runtime_settings"]["fetch_latest_first"] is True
        assert updated["runtime_settings"]["fetch_include_replies"] is False
        assert updated["runtime_settings"]["fetch_include_retweets"] is False
        assert updated["runtime_settings"]["llm_provider"] == "openai_compatible"
        assert updated["runtime_settings"]["llm_model_id"] == "vendor/model-1"
        assert updated["runtime_settings"]["llm_api_key_masked"] == "sk-s...1234"

        dashboard_response = await client.get("/admin/dashboard")
        assert dashboard_response.status_code == 200
        dashboard = dashboard_response.json()
        assert dashboard["runtime_settings"]["current_env_file"] == str(env_file)
        assert dashboard["runtime_settings"]["ai_enabled"] is True
        assert dashboard["runtime_settings"]["fetch_recent_window_hours"] == 18
        assert dashboard["runtime_settings"]["fetch_latest_first"] is True
        assert dashboard["runtime_settings"]["fetch_include_replies"] is False
        assert dashboard["runtime_settings"]["fetch_include_retweets"] is False
        assert dashboard["runtime_settings"]["llm_provider"] == "openai_compatible"
        assert dashboard["runtime_settings"]["llm_model_id"] == "vendor/model-1"


@pytest.mark.asyncio
async def test_runtime_prompt_test_route_returns_llm_output(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])

    async def fake_test_prompt(prompt: str):
        return {
            "provider": "mock",
            "model_id": None,
            "content": f"echo:{prompt}",
        }

    context.container.llm_service.test_prompt = fake_test_prompt

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/admin/runtime-settings/test",
            json={"prompt": "hello ai"},
        )

    assert response.status_code == 200
    assert response.json()["content"] == "echo:hello ai"


@pytest.mark.asyncio
async def test_ai_logs_routes_return_prompt_and_decision_logs(make_test_context) -> None:
    context = await make_test_context(accounts=[{"id": "acct1"}])
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="901",
                    author_handle="@openai",
                    text="AI logs should show this scoring input.",
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
        test_response = await client.post(
            "/admin/runtime-settings/test",
            json={"prompt": "hello ai logs"},
        )
        assert test_response.status_code == 200

        tweets_response = await client.get("/admin/tweets", params={"account_id": "acct1"})
        tweet_record_id = tweets_response.json()[0]["id"]
        generate_response = await client.post(
            f"/admin/tweets/{tweet_record_id}/reply/generate",
            json={"account_id": "acct1", "trigger_source": "console"},
        )
        assert generate_response.status_code == 200

        logs_response = await client.get("/admin/ai-logs")
        assert logs_response.status_code == 200
        logs = logs_response.json()
        log_types = {item["log_type"] for item in logs}
        assert "prompt_test" in log_types
        assert "decision" in log_types

        detail_response = await client.get(f"/admin/ai-logs/{logs[0]['id']}")
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert "request_payload" in detail
        assert "response_payload" in detail


@pytest.mark.asyncio
async def test_ai_log_summary_route_returns_runtime_stats(make_test_context) -> None:
    context = await make_test_context(
        accounts=[{"id": "acct1", "execution_mode": "dry_run"}],
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
                NormalizedTweet(
                    tweet_id="910",
                    author_handle="@openai",
                    text="skip while ai disabled",
                    created_at=datetime.now(UTC),
                )
            ],
            next_cursor=None,
        ),
    )
    await context.container.fetch_service.fetch_account("acct1", trigger="manual")

    context.settings.ai_enabled = True
    context.fake_source.add_batch(
        "acct1",
        SourceType.TIMELINE,
        "home_following",
        FetchBatchResult(
            items=[
                NormalizedTweet(
                    tweet_id="911",
                    author_handle="@openai",
                    text="score this one",
                    created_at=datetime.now(UTC),
                ),
                NormalizedTweet(
                    tweet_id="912",
                    author_handle="@openai",
                    text="reply should be filtered",
                    created_at=datetime.now(UTC),
                    is_reply=True,
                ),
                NormalizedTweet(
                    tweet_id="913",
                    author_handle="@openai",
                    text="retweet should be filtered",
                    created_at=datetime.now(UTC),
                    is_retweet=True,
                ),
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
        summary_response = await client.get(
            "/admin/ai-logs/summary",
            params={"account_id": "acct1"},
        )

    assert summary_response.status_code == 200
    summary = summary_response.json()
    assert summary["auto_scored_tweets"] == 2
    assert summary["auto_score_failed_tweets"] == 0
    assert summary["auto_score_skipped_ai_disabled"] == 1
    assert summary["filtered_replies_count"] == 1
    assert summary["filtered_retweets_count"] == 1
    assert summary["latest_auto_score_batch_scored"] == 1


@pytest.mark.asyncio
async def test_runtime_prompts_route_updates_prompt_templates(
    make_test_context,
    tmp_path: Path,
) -> None:
    prompt_file = tmp_path / "prompts.yaml"
    context = await make_test_context(
        accounts=[{"id": "acct1"}],
        settings_overrides={"ai_prompt_config_file": prompt_file},
    )

    async def container_factory(_: Settings):
        return context.container

    app = create_app(context.settings, container_factory=container_factory)
    app.state.container = context.container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        get_response = await client.get("/admin/runtime-prompts")
        assert get_response.status_code == 200

        update_response = await client.put(
            "/admin/runtime-prompts",
            json={
                "decision_system_template": "SYS {persona_name} {json_contract}",
                "decision_user_template": "USER {author_handle} {tweet_text}",
                "learning_system_template": "LEARN {persona_name} {json_contract}",
                "learning_user_template": "DIFF {tweet_text} {ai_draft} {final_draft}",
            },
        )
        assert update_response.status_code == 200
        updated = update_response.json()
        assert updated["prompts"]["config_file"] == str(prompt_file)
        assert updated["prompts"]["decision_user_template"] == "USER {author_handle} {tweet_text}"

    prompt_text = prompt_file.read_text(encoding="utf-8")
    assert "decision_user_template: USER {author_handle} {tweet_text}" in prompt_text
