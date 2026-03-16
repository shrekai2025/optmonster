from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.accounts.schemas import AccountConfig
from app.llm.service import LLMService
from app.runtime.enums import ActionStatus, ActionType
from app.runtime.models import ActionRequest, FetchedTweet
from app.runtime.settings import Settings


@dataclass(slots=True)
class GuideSample:
    tweet_text: str
    ai_draft: str
    final_draft: str


class WritingGuideService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm_service: LLMService,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.llm_service = llm_service
        self.settings = settings

    def resolve_path(self, account: AccountConfig) -> Path:
        configured = account.writing_guide_file or Path(f"{account.id}.md")
        return self.settings.resolve_path(configured)

    async def read_text(self, account: AccountConfig) -> str | None:
        path = self.resolve_path(account)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    async def apply_learning(self, account: AccountConfig, action_id: int) -> Path:
        samples = await self._load_recent_samples(account.id)
        if not samples:
            path = self.resolve_path(account)
            self._write_markdown(path, account, None, [])
            return path

        latest = samples[0]
        recommendation = None
        if self.settings.ai_enabled:
            recommendation = await self.llm_service.summarize_learning(
                account=account,
                tweet_text=latest.tweet_text,
                ai_draft=latest.ai_draft,
                final_draft=latest.final_draft,
            )
        path = self.resolve_path(account)
        self._write_markdown(path, account, recommendation, samples)
        return path

    async def _load_recent_samples(self, account_id: str) -> list[GuideSample]:
        async with self.session_factory() as session:
            query = (
                select(ActionRequest, FetchedTweet.text)
                .outerjoin(FetchedTweet, FetchedTweet.id == ActionRequest.fetched_tweet_id)
                .where(
                    ActionRequest.account_id == account_id,
                    ActionRequest.action_type == ActionType.REPLY,
                    ActionRequest.status == ActionStatus.SUCCEEDED,
                    ActionRequest.edited_draft.is_not(None),
                    ActionRequest.final_draft.is_not(None),
                    ActionRequest.ai_draft.is_not(None),
                )
                .order_by(desc(ActionRequest.executed_at), desc(ActionRequest.id))
                .limit(20)
            )
            rows = (await session.execute(query)).all()
        return [
            GuideSample(
                tweet_text=tweet_text or "",
                ai_draft=action.ai_draft or "",
                final_draft=action.final_draft or "",
            )
            for action, tweet_text in rows
            if action.ai_draft and action.final_draft
        ]

    def _write_markdown(
        self,
        path: Path,
        account: AccountConfig,
        recommendation,
        samples: list[GuideSample],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        forbidden = ", ".join(account.persona.forbidden_topics) or "None"
        voice = (
            recommendation.voice
            if recommendation
            else f"{account.persona.name}: {account.persona.tone}"
        )
        dos = recommendation.dos if recommendation else [account.persona.reply_style]
        donts = recommendation.donts if recommendation else [f"Forbidden topics: {forbidden}"]
        lines = [
            "# Voice",
            voice,
            "",
            "# Do",
            *[f"- {item}" for item in dos],
            "",
            "# Don't",
            *[f"- {item}" for item in donts],
            "",
            "# Recent Approved Edits",
        ]
        if not samples:
            lines.append("- No edited replies yet.")
        else:
            for sample in samples:
                lines.extend(
                    [
                        f"- Original tweet: {sample.tweet_text[:180]}",
                        f"  AI draft: {sample.ai_draft}",
                        f"  Final draft: {sample.final_draft}",
                    ]
                )
        path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
