from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import orjson
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.schemas import AccountConfig
from app.llm.schemas import (
    AILogDetailView,
    AILogListItem,
    AILogSummaryView,
    DecisionResult,
    GuideRecommendation,
    PromptTemplateConfig,
    PromptTemplateUpdateResult,
    PromptTemplateView,
    PromptTestResponse,
)
from app.runtime.enums import LLMProvider
from app.runtime.models import AILogRecord
from app.runtime.settings import Settings


class LLMService:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.prompt_templates = self._load_prompt_templates()

    async def generate_decision(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        author_handle: str | None,
        writing_guide: str | None,
        fetched_tweet_id: int | None = None,
    ) -> DecisionResult:
        provider = self.settings.llm_provider
        request_payload: dict[str, Any] = {
            "tweet_text": tweet_text,
            "author_handle": author_handle,
            "writing_guide": writing_guide,
        }
        if provider == LLMProvider.MOCK or not self.settings.llm_api_key:
            result = self._mock_decision(
                account=account,
                tweet_text=tweet_text,
                author_handle=author_handle,
            )
            await self._record_log(
                account_id=account.id,
                fetched_tweet_id=fetched_tweet_id,
                log_type="decision",
                status="success",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={"result": result.model_dump(mode="json")},
            )
            return result

        system_prompt = self._decision_system_prompt(account, writing_guide)
        user_prompt = self._decision_user_prompt(tweet_text=tweet_text, author_handle=author_handle)
        request_payload.update(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        response: dict[str, Any] | None = None
        content: Any = None
        try:
            if provider == LLMProvider.ANTHROPIC:
                payload = {
                    "model": self._require_model_id(),
                    "max_tokens": 600,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                request_payload["request_body"] = payload
                response = await self._post_anthropic(payload)
                content = response["content"][0]["text"]
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                request_payload["request_body"] = {"messages": messages}
                response = await self._post_openai_compatible(messages=messages)
                content = response["choices"][0]["message"]["content"]

            result = self._parse_decision_result(content)
            await self._record_log(
                account_id=account.id,
                fetched_tweet_id=fetched_tweet_id,
                log_type="decision",
                status="success",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={
                    "raw_response": response,
                    "raw_content": self._extract_text_response(content),
                    "result": result.model_dump(mode="json"),
                },
            )
            return result
        except Exception as exc:
            await self._record_log(
                account_id=account.id,
                fetched_tweet_id=fetched_tweet_id,
                log_type="decision",
                status="failed",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={
                    "raw_response": response,
                    "raw_content": (
                        self._extract_text_response(content)
                        if content is not None
                        else None
                    ),
                },
                error_message=str(exc),
            )
            raise

    async def summarize_learning(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        ai_draft: str,
        final_draft: str,
    ) -> GuideRecommendation:
        provider = self.settings.llm_provider
        request_payload: dict[str, Any] = {
            "tweet_text": tweet_text,
            "ai_draft": ai_draft,
            "final_draft": final_draft,
        }
        if provider == LLMProvider.MOCK or not self.settings.llm_api_key:
            result = self._mock_guide(account=account, ai_draft=ai_draft, final_draft=final_draft)
            await self._record_log(
                account_id=account.id,
                log_type="learning",
                status="success",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={"result": result.model_dump(mode="json")},
            )
            return result

        system_prompt = self._learning_system_prompt(account)
        user_prompt = self._learning_user_prompt(
            tweet_text=tweet_text,
            ai_draft=ai_draft,
            final_draft=final_draft,
        )
        request_payload.update(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
            }
        )
        response: dict[str, Any] | None = None
        content: Any = None
        try:
            if provider == LLMProvider.ANTHROPIC:
                payload = {
                    "model": self._require_model_id(),
                    "max_tokens": 500,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                }
                request_payload["request_body"] = payload
                response = await self._post_anthropic(payload)
                content = response["content"][0]["text"]
            else:
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                request_payload["request_body"] = {"messages": messages}
                response = await self._post_openai_compatible(messages=messages)
                content = response["choices"][0]["message"]["content"]

            result = self._parse_guide_recommendation(content)
            await self._record_log(
                account_id=account.id,
                log_type="learning",
                status="success",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={
                    "raw_response": response,
                    "raw_content": self._extract_text_response(content),
                    "result": result.model_dump(mode="json"),
                },
            )
            return result
        except Exception as exc:
            await self._record_log(
                account_id=account.id,
                log_type="learning",
                status="failed",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={
                    "raw_response": response,
                    "raw_content": (
                        self._extract_text_response(content)
                        if content is not None
                        else None
                    ),
                },
                error_message=str(exc),
            )
            raise

    def configured_provider_name(self) -> str:
        return self.settings.llm_provider.value

    def configured_model(self) -> str | None:
        return self.settings.llm_model_id

    def get_prompt_templates(self) -> PromptTemplateView:
        return PromptTemplateView(
            config_file=str(self._prompt_config_path()),
            **self.prompt_templates.model_dump(),
        )

    def update_prompt_templates(
        self,
        payload: PromptTemplateConfig,
    ) -> PromptTemplateUpdateResult:
        self._validate_prompt_templates(payload)
        path = self._prompt_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(payload.model_dump(mode="json"), sort_keys=False),
            encoding="utf-8",
        )
        self.prompt_templates = payload
        return PromptTemplateUpdateResult(
            prompts=PromptTemplateView(
                config_file=str(path),
                **payload.model_dump(),
            )
        )

    async def list_logs(
        self,
        *,
        account_id: str | None = None,
        log_type: str | None = None,
        limit: int = 100,
    ) -> list[AILogListItem]:
        if self.session_factory is None:
            return []
        query = select(AILogRecord).order_by(AILogRecord.created_at.desc(), AILogRecord.id.desc())
        if account_id:
            query = query.where(AILogRecord.account_id == account_id)
        if log_type:
            query = query.where(AILogRecord.log_type == log_type)
        async with self.session_factory() as session:
            rows = (await session.execute(query.limit(limit))).scalars().all()
        return [self._to_log_list_item(row) for row in rows]

    async def get_log(self, log_id: int) -> AILogDetailView:
        if self.session_factory is None:
            raise KeyError(log_id)
        async with self.session_factory() as session:
            row = await session.get(AILogRecord, log_id)
        if row is None:
            raise KeyError(log_id)
        item = self._to_log_list_item(row)
        return AILogDetailView(
            **item.model_dump(),
            request_payload=row.request_payload,
            response_payload=row.response_payload,
        )

    async def get_log_summary(
        self,
        *,
        account_id: str | None = None,
        window_hours: int = 24,
    ) -> AILogSummaryView:
        if self.session_factory is None:
            return AILogSummaryView(
                window_hours=window_hours,
                total_logs=0,
                decision_success_count=0,
                decision_failed_count=0,
                learning_count=0,
                prompt_test_count=0,
                auto_scored_tweets=0,
                auto_score_failed_tweets=0,
                auto_score_skipped_ai_disabled=0,
                filtered_replies_count=0,
                filtered_retweets_count=0,
            )

        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        query = (
            select(AILogRecord)
            .where(AILogRecord.created_at >= cutoff)
            .order_by(AILogRecord.created_at.desc(), AILogRecord.id.desc())
        )
        if account_id:
            query = query.where(AILogRecord.account_id == account_id)
        async with self.session_factory() as session:
            rows = (await session.execute(query)).scalars().all()

        decision_success_count = 0
        decision_failed_count = 0
        learning_count = 0
        prompt_test_count = 0
        auto_scored_tweets = 0
        auto_score_failed_tweets = 0
        auto_score_skipped_ai_disabled = 0
        filtered_replies_count = 0
        filtered_retweets_count = 0
        latest_auto_score_batch_at = None
        latest_auto_score_batch_scored = None
        latest_auto_score_batch_failed = None

        for row in rows:
            if row.log_type == "decision":
                if row.status == "success":
                    decision_success_count += 1
                else:
                    decision_failed_count += 1
            elif row.log_type == "learning":
                learning_count += 1
            elif row.log_type == "prompt_test":
                prompt_test_count += 1
            elif row.log_type == "auto_score_batch":
                payload = row.response_payload or {}
                auto_scored_tweets += int(payload.get("scored_count", 0) or 0)
                auto_score_failed_tweets += int(payload.get("failed_count", 0) or 0)
                if latest_auto_score_batch_at is None:
                    latest_auto_score_batch_at = row.created_at
                    latest_auto_score_batch_scored = int(payload.get("scored_count", 0) or 0)
                    latest_auto_score_batch_failed = int(payload.get("failed_count", 0) or 0)
            elif row.log_type == "auto_score_skip":
                payload = row.response_payload or {}
                if payload.get("reason") == "ai_disabled":
                    auto_score_skipped_ai_disabled += int(payload.get("skipped_count", 0) or 0)
            elif row.log_type == "fetch_filter":
                payload = row.response_payload or {}
                filtered_replies_count += int(payload.get("reply_filtered", 0) or 0)
                filtered_retweets_count += int(payload.get("retweet_filtered", 0) or 0)

        return AILogSummaryView(
            window_hours=window_hours,
            total_logs=len(rows),
            decision_success_count=decision_success_count,
            decision_failed_count=decision_failed_count,
            learning_count=learning_count,
            prompt_test_count=prompt_test_count,
            auto_scored_tweets=auto_scored_tweets,
            auto_score_failed_tweets=auto_score_failed_tweets,
            auto_score_skipped_ai_disabled=auto_score_skipped_ai_disabled,
            filtered_replies_count=filtered_replies_count,
            filtered_retweets_count=filtered_retweets_count,
            latest_auto_score_batch_at=latest_auto_score_batch_at,
            latest_auto_score_batch_scored=latest_auto_score_batch_scored,
            latest_auto_score_batch_failed=latest_auto_score_batch_failed,
        )

    async def test_prompt(self, prompt: str) -> PromptTestResponse:
        provider = self.settings.llm_provider
        request_payload = {"prompt": prompt}
        if provider == LLMProvider.MOCK or not self.settings.llm_api_key:
            content = (
                "mock provider active\n\n"
                f"received prompt:\n{prompt}\n\n"
                "Set LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL_ID / LLM_API_KEY to test a real model."
            )
            result = PromptTestResponse(
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                content=content,
            )
            await self._record_log(
                account_id=None,
                fetched_tweet_id=None,
                log_type="prompt_test",
                status="success",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={"result": result.model_dump(mode="json")},
            )
            return result
        try:
            if provider == LLMProvider.ANTHROPIC:
                payload = {
                    "model": self._require_model_id(),
                    "max_tokens": 800,
                    "system": "Answer the user prompt directly. Do not return JSON unless asked.",
                    "messages": [{"role": "user", "content": prompt}],
                }
                request_payload["request_body"] = payload
                response = await self._post_anthropic(payload)
                content = self._extract_text_response(response.get("content"))
            else:
                messages = [
                    {
                        "role": "system",
                        "content": (
                            "Answer the user prompt directly. "
                            "Do not return JSON unless asked."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ]
                request_payload["request_body"] = {"messages": messages}
                response = await self._post_openai_compatible_plain(messages=messages)
                content = self._extract_text_response(response["choices"][0]["message"]["content"])

            result = PromptTestResponse(
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                content=content,
            )
            await self._record_log(
                account_id=None,
                fetched_tweet_id=None,
                log_type="prompt_test",
                status="success",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload={
                    "raw_response": response,
                    "result": result.model_dump(mode="json"),
                },
            )
            return result
        except Exception as exc:
            await self._record_log(
                account_id=None,
                fetched_tweet_id=None,
                log_type="prompt_test",
                status="failed",
                provider=provider.value,
                model_id=self.settings.llm_model_id,
                request_payload=request_payload,
                response_payload=None,
                error_message=str(exc),
            )
            raise

    def _mock_decision(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        author_handle: str | None,
    ) -> DecisionResult:
        persona_terms = " ".join(
            [
                account.persona.role,
                account.persona.tone,
                account.persona.reply_style,
                " ".join(item.handle for item in account.targets.follow_users),
                " ".join(item.query for item in account.targets.search_keywords),
            ]
        ).lower()
        tweet_lower = tweet_text.lower()
        keywords = {
            token
            for token in persona_terms.replace("/", " ").replace(",", " ").split()
            if len(token) > 3
        }
        score = 4
        if any(keyword in tweet_lower for keyword in keywords):
            score = 8
        elif len(tweet_text) > 80:
            score = 6
        like = score >= 6
        subject = tweet_text.strip().split(".")[0].strip()
        if len(subject) > 72:
            subject = f"{subject[:69]}..."
        reply_draft = None
        reply_confidence = 0
        if score >= 7:
            opener = "Interesting point"
            if author_handle:
                opener = f"{author_handle}, interesting point"
            reply_draft = f"{opener}. Curious how this changes the practical tradeoffs?"
            reply_confidence = min(score, 9)
        return DecisionResult(
            relevance_score=score,
            like=like,
            reply_draft=reply_draft,
            reply_confidence=reply_confidence,
            rationale=f"mock decision generated from persona overlap for {subject or 'tweet'}",
        )

    def _mock_guide(
        self,
        *,
        account: AccountConfig,
        ai_draft: str,
        final_draft: str,
    ) -> GuideRecommendation:
        ai_words = set(ai_draft.lower().split())
        final_words = set(final_draft.lower().split())
        added_words = sorted(word for word in final_words - ai_words if len(word) > 3)[:3]
        removed_words = sorted(word for word in ai_words - final_words if len(word) > 3)[:3]
        dos = [f"Prefer wording closer to: {final_draft[:72]}"]
        if added_words:
            dos.append(f"Lean into concrete words like: {', '.join(added_words)}")
        donts = [f"Avoid the flatter phrasing from: {ai_draft[:72]}"]
        if removed_words:
            donts.append(f"Cut filler such as: {', '.join(removed_words)}")
        return GuideRecommendation(
            voice=f"{account.persona.name}: {account.persona.tone}",
            dos=dos,
            donts=donts,
        )

    async def _openai_compatible_decision(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        author_handle: str | None,
        writing_guide: str | None,
    ) -> DecisionResult:
        system_prompt = self._decision_system_prompt(account, writing_guide)
        user_prompt = self._decision_user_prompt(tweet_text=tweet_text, author_handle=author_handle)
        response = await self._post_openai_compatible(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        content = response["choices"][0]["message"]["content"]
        return DecisionResult.model_validate(self._extract_json_payload(content))

    async def _openai_compatible_learning(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        ai_draft: str,
        final_draft: str,
    ) -> GuideRecommendation:
        response = await self._post_openai_compatible(
            messages=[
                {"role": "system", "content": self._learning_system_prompt(account)},
                {
                    "role": "user",
                    "content": self._learning_user_prompt(
                        tweet_text=tweet_text,
                        ai_draft=ai_draft,
                        final_draft=final_draft,
                    ),
                },
            ]
        )
        content = response["choices"][0]["message"]["content"]
        return GuideRecommendation.model_validate(self._extract_json_payload(content))

    async def _anthropic_decision(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        author_handle: str | None,
        writing_guide: str | None,
    ) -> DecisionResult:
        payload = {
            "model": self._require_model_id(),
            "max_tokens": 600,
            "system": self._decision_system_prompt(account, writing_guide),
            "messages": [
                {
                    "role": "user",
                    "content": self._decision_user_prompt(
                        tweet_text=tweet_text,
                        author_handle=author_handle,
                    ),
                }
            ],
        }
        response = await self._post_anthropic(payload)
        content = response["content"][0]["text"]
        return DecisionResult.model_validate(self._extract_json_payload(content))

    async def _anthropic_learning(
        self,
        *,
        account: AccountConfig,
        tweet_text: str,
        ai_draft: str,
        final_draft: str,
    ) -> GuideRecommendation:
        payload = {
            "model": self._require_model_id(),
            "max_tokens": 500,
            "system": self._learning_system_prompt(account),
            "messages": [
                {
                    "role": "user",
                    "content": self._learning_user_prompt(
                        tweet_text=tweet_text,
                        ai_draft=ai_draft,
                        final_draft=final_draft,
                    ),
                }
            ],
        }
        response = await self._post_anthropic(payload)
        content = response["content"][0]["text"]
        return GuideRecommendation.model_validate(self._extract_json_payload(content))

    async def _post_openai_compatible(self, *, messages: list[dict[str, Any]]) -> dict[str, Any]:
        base_url = (self.settings.llm_base_url or "").rstrip("/")
        if not base_url:
            raise ValueError("LLM_BASE_URL is required for openai_compatible provider")
        payload = {
            "model": self._require_model_id(),
            "messages": messages,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.llm_request_timeout_seconds) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    async def _post_openai_compatible_plain(
        self,
        *,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        base_url = (self.settings.llm_base_url or "").rstrip("/")
        if not base_url:
            raise ValueError("LLM_BASE_URL is required for openai_compatible provider")
        payload = {
            "model": self._require_model_id(),
            "messages": messages,
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.llm_request_timeout_seconds) as client:
            response = await client.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            return response.json()

    async def _post_anthropic(self, payload: dict[str, Any]) -> dict[str, Any]:
        base_url = (self.settings.llm_base_url or "https://api.anthropic.com/v1/messages").rstrip("/")
        if not base_url.endswith("/messages"):
            base_url = f"{base_url}/messages"
        headers = {
            "x-api-key": self.settings.llm_api_key or "",
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.llm_request_timeout_seconds) as client:
            response = await client.post(base_url, json=payload, headers=headers)
            response.raise_for_status()
            return response.json()

    def _decision_system_prompt(self, account: AccountConfig, writing_guide: str | None) -> str:
        forbidden = ", ".join(account.persona.forbidden_topics) or "None"
        guide_text = writing_guide or "No extra guide yet."
        return self.prompt_templates.decision_system_template.format(
            persona_name=account.persona.name,
            persona_role=account.persona.role,
            persona_tone=account.persona.tone,
            persona_language=account.persona.language,
            reply_style=account.persona.reply_style,
            forbidden_topics=forbidden,
            writing_guide=guide_text,
            json_contract=(
                "Return exactly one JSON object only. No prose, no markdown, no code fences. "
                "Required keys: relevance_score (integer 0-10), like (boolean), "
                "reply_draft (string or null), reply_confidence (integer 0-10), "
                "rationale (string). If there should be no reply, set reply_draft to null "
                "and reply_confidence to 0."
            ),
        )

    def _decision_user_prompt(self, *, tweet_text: str, author_handle: str | None) -> str:
        return self.prompt_templates.decision_user_template.format(
            author_handle=author_handle or "unknown",
            tweet_text=tweet_text,
        )

    def _learning_system_prompt(self, account: AccountConfig) -> str:
        return self.prompt_templates.learning_system_template.format(
            persona_name=account.persona.name,
            persona_tone=account.persona.tone,
            reply_style=account.persona.reply_style,
            json_contract=(
                "Return exactly one JSON object only. No prose, no markdown, no code fences. "
                "Required keys: voice (string), dos (array of short strings), "
                "donts (array of short strings)."
            ),
        )

    def _learning_user_prompt(self, *, tweet_text: str, ai_draft: str, final_draft: str) -> str:
        return self.prompt_templates.learning_user_template.format(
            tweet_text=tweet_text,
            ai_draft=ai_draft,
            final_draft=final_draft,
        )

    def _extract_json_payload(self, content: Any) -> dict[str, Any]:
        text = self._extract_text_response(content)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
        return orjson.loads(text)

    def _parse_decision_result(self, content: Any) -> DecisionResult:
        raw_text = self._extract_text_response(content)
        payload = self._extract_json_with_fallback(raw_text)
        normalized = self._normalize_decision_payload(payload, raw_text=raw_text)
        return DecisionResult.model_validate(normalized)

    def _parse_guide_recommendation(self, content: Any) -> GuideRecommendation:
        raw_text = self._extract_text_response(content)
        payload = self._extract_json_with_fallback(raw_text)
        normalized = self._normalize_guide_payload(payload)
        return GuideRecommendation.model_validate(normalized)

    def _extract_json_with_fallback(self, raw_text: str) -> dict[str, Any]:
        try:
            return self._extract_json_payload(raw_text)
        except Exception:
            return self._extract_key_value_payload(raw_text)

    def _extract_key_value_payload(self, raw_text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for raw_line in raw_text.splitlines():
            line = raw_line.strip().strip("-*").strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            normalized_key = key.strip().lower().replace(" ", "_").replace("-", "_")
            cleaned_value = value.strip()
            if normalized_key in {"relevance_score", "score", "reply_confidence"}:
                parsed_int = self._parse_int_value(cleaned_value)
                if parsed_int is not None:
                    payload[normalized_key] = parsed_int
            elif normalized_key == "like":
                payload[normalized_key] = self._parse_bool_value(cleaned_value)
            elif normalized_key in {"reply", "reply_draft", "rationale", "reason", "voice"}:
                payload[normalized_key] = cleaned_value
            elif normalized_key in {"dos", "donts"}:
                payload[normalized_key] = self._parse_list_value(cleaned_value)
        if payload:
            return payload
        raise ValueError("LLM response did not contain recognizable JSON or key-value pairs")

    def _normalize_decision_payload(
        self,
        payload: dict[str, Any],
        *,
        raw_text: str,
    ) -> dict[str, Any]:
        normalized = dict(payload)
        if "score" in normalized and "relevance_score" not in normalized:
            normalized["relevance_score"] = normalized.pop("score")
        if "reply" in normalized and "reply_draft" not in normalized:
            normalized["reply_draft"] = normalized.pop("reply")
        if "reason" in normalized and "rationale" not in normalized:
            normalized["rationale"] = normalized.pop("reason")

        normalized["like"] = self._parse_bool_value(normalized.get("like"))
        normalized["relevance_score"] = self._clamp_score(normalized.get("relevance_score"))

        reply_draft = normalized.get("reply_draft")
        if isinstance(reply_draft, str):
            cleaned_reply = reply_draft.strip()
            if cleaned_reply.lower() in {"", "n/a", "none", "null", "no", "no reply"}:
                normalized["reply_draft"] = None
            else:
                normalized["reply_draft"] = cleaned_reply
        elif reply_draft is None:
            normalized["reply_draft"] = None

        reply_confidence = normalized.get("reply_confidence")
        if reply_confidence is None:
            normalized["reply_confidence"] = (
                normalized["relevance_score"] if normalized["reply_draft"] else 0
            )
        else:
            normalized["reply_confidence"] = self._clamp_score(reply_confidence)

        rationale = normalized.get("rationale")
        if rationale is None:
            normalized["rationale"] = raw_text[:280] if raw_text else None
        return normalized

    def _normalize_guide_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        if isinstance(normalized.get("dos"), str):
            normalized["dos"] = self._parse_list_value(normalized["dos"])
        if isinstance(normalized.get("donts"), str):
            normalized["donts"] = self._parse_list_value(normalized["donts"])
        return normalized

    def _parse_int_value(self, value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        match = re.search(r"-?\d+", str(value))
        if match is None:
            return None
        return int(match.group(0))

    def _parse_bool_value(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"true", "yes", "y", "1", "like", "liked"}

    def _parse_list_value(self, value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        text = str(value or "").strip()
        if not text:
            return []
        parts = re.split(r"[;\n]|(?:\s*[|]\s*)", text)
        items = [part.strip(" -•\t") for part in parts if part.strip(" -•\t")]
        if items:
            return items
        return [text]

    def _clamp_score(self, value: Any) -> int:
        parsed = self._parse_int_value(value)
        if parsed is None:
            return 0
        return max(0, min(10, parsed))

    def _extract_text_response(self, content: Any) -> str:
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if text:
                        parts.append(str(text))
                else:
                    parts.append(str(part))
            return "".join(parts).strip()
        return str(content).strip()

    async def _record_log(
        self,
        *,
        account_id: str | None,
        fetched_tweet_id: int | None,
        log_type: str,
        status: str,
        provider: str | None,
        model_id: str | None,
        request_payload: dict[str, Any] | None,
        response_payload: dict[str, Any] | None,
        error_message: str | None = None,
    ) -> None:
        if self.session_factory is None:
            return
        async with self.session_factory() as session:
            session.add(
                AILogRecord(
                    account_id=account_id,
                    fetched_tweet_id=fetched_tweet_id,
                    log_type=log_type,
                    status=status,
                    provider=provider,
                    model_id=model_id,
                    request_payload=request_payload,
                    response_payload=response_payload,
                    error_message=error_message,
                )
            )
            await session.commit()

    def _to_log_list_item(self, row: AILogRecord) -> AILogListItem:
        return AILogListItem(
            id=row.id,
            created_at=row.created_at,
            account_id=row.account_id,
            log_type=row.log_type,
            status=row.status,
            provider=row.provider,
            model_id=row.model_id,
            request_preview=self._preview_payload(row.request_payload),
            response_preview=self._preview_payload(row.response_payload),
            error_message=row.error_message,
        )

    def _preview_payload(self, payload: dict[str, Any] | None) -> str:
        if not payload:
            return "n/a"
        for key in ("tweet_text", "prompt", "user_prompt", "system_prompt"):
            value = payload.get(key)
            if value:
                return str(value)[:180]
        return orjson.dumps(payload).decode("utf-8")[:180]

    def _require_model_id(self) -> str:
        if not self.settings.llm_model_id:
            raise ValueError("LLM_MODEL_ID is required")
        return self.settings.llm_model_id

    def _prompt_config_path(self):
        return self.settings.resolve_path(self.settings.ai_prompt_config_file)

    def _load_prompt_templates(self) -> PromptTemplateConfig:
        path = self._prompt_config_path()
        if not path.exists():
            defaults = self._default_prompt_templates()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(defaults.model_dump(mode="json"), sort_keys=False),
                encoding="utf-8",
            )
            return defaults
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return PromptTemplateConfig.model_validate(payload)

    def _default_prompt_templates(self) -> PromptTemplateConfig:
        return PromptTemplateConfig(
            decision_system_template=(
                "You are helping manage an X account. {json_contract} "
                "Persona name: {persona_name}. Role: {persona_role}. "
                "Tone: {persona_tone}. Language: {persona_language}. "
                "Reply style: {reply_style}. Forbidden topics: {forbidden_topics}. "
                "Writing guide: {writing_guide}"
            ),
            decision_user_template=(
                "Author: {author_handle}\n"
                "Tweet:\n{tweet_text}\n\n"
                "Score relevance from 0-10. Like only if score >= 6. "
                "Create a reply only if score >= 8. Reply must read human and stay under 40 words."
            ),
            learning_system_template=(
                "You summarize how a human editor improved an AI reply. {json_contract} "
                "Keep dos and donts as short bullet-worthy strings. "
                "Persona name: {persona_name}. Tone: {persona_tone}. Reply style: {reply_style}."
            ),
            learning_user_template=(
                "Original tweet:\n{tweet_text}\n\n"
                "AI draft:\n{ai_draft}\n\n"
                "Human-approved final draft:\n{final_draft}\n\n"
                "Summarize the preferred voice and 2-4 dos/donts."
            ),
        )

    def _validate_prompt_templates(self, payload: PromptTemplateConfig) -> None:
        samples = {
            "persona_name": "Operator",
            "persona_role": "Thoughtful X account operator",
            "persona_tone": "Clear and warm",
            "persona_language": "English",
            "reply_style": "Offer one concrete thought or question in under 40 words.",
            "forbidden_topics": "Politics, personal attacks",
            "writing_guide": "No extra guide yet.",
            "json_contract": "Return JSON only with keys ...",
            "author_handle": "@openai",
            "tweet_text": "AI infra teams need better evals.",
            "ai_draft": "Interesting point. Curious how this changes tradeoffs?",
            "final_draft": "Good point. The evaluation loop is still the bottleneck.",
        }
        try:
            payload.decision_system_template.format(**samples)
            payload.decision_user_template.format(**samples)
            payload.learning_system_template.format(**samples)
            payload.learning_user_template.format(**samples)
        except KeyError as exc:
            raise ValueError(f"unknown prompt template variable: {exc.args[0]}") from exc
