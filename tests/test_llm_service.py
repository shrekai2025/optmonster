from __future__ import annotations

from pathlib import Path

import pytest

import app.llm.service as llm_module
from app.accounts.schemas import AccountConfig
from app.llm.schemas import PromptTemplateConfig
from app.llm.service import LLMService
from app.runtime.database import create_engine, create_schema, create_session_factory
from app.runtime.settings import Settings, get_settings


def _build_account(tmp_path: Path) -> AccountConfig:
    cookie_path = tmp_path / "acct.json"
    cookie_path.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "acct.yaml"
    config_path.write_text("id: acct\n", encoding="utf-8")
    return AccountConfig.model_validate(
        {
            "id": "acct",
            "twitter_handle": "@acct",
            "cookie_file": str(cookie_path),
            "persona": {
                "name": "Acct",
                "role": "AI infrastructure operator",
                "tone": "Clear",
                "language": "English",
                "forbidden_topics": ["politics"],
                "reply_style": "Offer one practical angle.",
            },
        }
    ).ensure_runtime_fields(source_file=config_path, resolved_cookie_file=cookie_path)


@pytest.mark.asyncio
async def test_openai_compatible_llm_uses_base_url_key_and_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"relevance_score":8,"like":true,'
                                '"reply_draft":"Solid point.",'
                                '"reply_confidence":7,"rationale":"structured"}'
                            )
                        }
                    }
                ]
            }

    class DummyClient:
        def __init__(self, *, timeout: int) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> DummyClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str],
        ) -> DummyResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return DummyResponse()

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", DummyClient)
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example/v1",
        llm_api_key="sk-secret-1234",
        llm_model_id="vendor/model-1",
    )
    service = LLMService(settings)
    account = _build_account(tmp_path)

    result = await service.generate_decision(
        account=account,
        tweet_text="AI infra teams need better evals.",
        author_handle="@openai",
        writing_guide="Keep replies grounded.",
    )

    assert result.reply_draft == "Solid point."
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["headers"] == {
        "Authorization": "Bearer sk-secret-1234",
        "Content-Type": "application/json",
    }
    assert captured["json"]["model"] == "vendor/model-1"


@pytest.mark.asyncio
async def test_openai_compatible_llm_accepts_plain_text_key_value_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "Relevance score: 2\nLike: No\nReply: N/A",
                        }
                    }
                ]
            }

    class DummyClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> DummyClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str],
        ) -> DummyResponse:
            return DummyResponse()

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", DummyClient)
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example/v1",
        llm_api_key="sk-secret-1234",
        llm_model_id="vendor/model-1",
    )
    service = LLMService(settings)
    account = _build_account(tmp_path)

    result = await service.generate_decision(
        account=account,
        tweet_text="Traffic update",
        author_handle="@mmda",
        writing_guide="Stay concise.",
    )

    assert result.relevance_score == 2
    assert result.like is False
    assert result.reply_draft is None
    assert result.reply_confidence == 0


@pytest.mark.asyncio
async def test_openai_compatible_llm_normalizes_legacy_decision_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": '{"score":9,"like":true,"reply":"Thanks for the update."}',
                        }
                    }
                ]
            }

    class DummyClient:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        async def __aenter__(self) -> DummyClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str],
        ) -> DummyResponse:
            return DummyResponse()

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", DummyClient)
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example/v1",
        llm_api_key="sk-secret-1234",
        llm_model_id="vendor/model-1",
    )
    service = LLMService(settings)
    account = _build_account(tmp_path)

    result = await service.generate_decision(
        account=account,
        tweet_text="Road closed",
        author_handle="@mmda",
        writing_guide="Stay concise.",
    )

    assert result.relevance_score == 9
    assert result.like is True
    assert result.reply_draft == "Thanks for the update."
    assert result.reply_confidence == 9


def test_settings_masks_llm_api_key() -> None:
    settings = Settings(_env_file=None, llm_api_key="sk-secret-1234")

    assert settings.llm_api_key_masked == "sk-s...1234"


def test_get_settings_prefers_runtime_env_file_over_stale_process_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "\n".join(
            [
                "APP_TIMEZONE=Asia/Singapore",
                "FETCH_RECENT_WINDOW_HOURS=36",
                "FETCH_LATEST_FIRST=true",
                "LLM_PROVIDER=openai_compatible",
                "LLM_BASE_URL=https://llm.example/v1",
                "LLM_API_KEY=sk-runtime-1234",
                "LLM_MODEL_ID=vendor/model-2",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("APP_ENV_FILE", str(env_file))
    monkeypatch.setenv("APP_TIMEZONE", "UTC")
    monkeypatch.setenv("FETCH_RECENT_WINDOW_HOURS", "0")
    monkeypatch.setenv("FETCH_LATEST_FIRST", "false")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL_ID", "")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.app_timezone == "Asia/Singapore"
    assert settings.fetch_recent_window_hours == 36
    assert settings.fetch_latest_first is True
    assert settings.llm_provider.value == "openai_compatible"
    assert settings.llm_base_url == "https://llm.example/v1"
    assert settings.llm_api_key == "sk-runtime-1234"
    assert settings.llm_model_id == "vendor/model-2"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_test_prompt_uses_current_provider_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": "hello from test prompt",
                        }
                    }
                ]
            }

    class DummyClient:
        def __init__(self, *, timeout: int) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> DummyClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            json: dict[str, object],
            headers: dict[str, str],
        ) -> DummyResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return DummyResponse()

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", DummyClient)
    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example/v1",
        llm_api_key="sk-secret-1234",
        llm_model_id="vendor/model-1",
    )
    service = LLMService(settings)

    result = await service.test_prompt("Say hello")

    assert result.provider == "openai_compatible"
    assert result.content == "hello from test prompt"
    assert captured["url"] == "https://llm.example/v1/chat/completions"
    assert captured["json"]["model"] == "vendor/model-1"


def test_prompt_templates_can_be_updated_and_persisted(tmp_path: Path) -> None:
    prompt_file = tmp_path / "prompts.yaml"
    settings = Settings(_env_file=None, ai_prompt_config_file=prompt_file)
    service = LLMService(settings)

    result = service.update_prompt_templates(
        PromptTemplateConfig(
            decision_system_template="SYS {persona_name} {json_contract}",
            decision_user_template="USER {author_handle} {tweet_text}",
            learning_system_template="LEARN {persona_name} {json_contract}",
            learning_user_template="DIFF {tweet_text} {ai_draft} {final_draft}",
        )
    )

    assert result.prompts.config_file == str(prompt_file)
    assert "decision_system_template: SYS {persona_name} {json_contract}" in prompt_file.read_text(
        encoding="utf-8"
    )
    rendered = service._decision_user_prompt(
        tweet_text="hello",
        author_handle="@openai",
    )
    assert rendered == "USER @openai hello"


@pytest.mark.asyncio
async def test_llm_service_persists_ai_logs_to_database(tmp_path: Path) -> None:
    db_path = tmp_path / "llm_logs.db"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        llm_provider="mock",
    )
    engine = create_engine(settings)
    await create_schema(engine)
    session_factory = create_session_factory(engine)
    service = LLMService(settings, session_factory=session_factory)
    account = _build_account(tmp_path)

    await service.generate_decision(
        account=account,
        tweet_text="AI logging should preserve this decision input.",
        author_handle="@openai",
        writing_guide="Stay concise.",
    )
    await service.test_prompt("hello prompt log")

    logs = await service.list_logs(limit=10)

    assert len(logs) == 2
    assert {item.log_type for item in logs} == {"decision", "prompt_test"}
    detail = await service.get_log(logs[0].id)
    assert detail.request_payload is not None
    assert detail.response_payload is not None

    await engine.dispose()
