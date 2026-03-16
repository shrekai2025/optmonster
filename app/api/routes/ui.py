from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

router = APIRouter(tags=["ui"])
UI_DIR = Path(__file__).resolve().parents[2] / "ui"
ADMIN_PAGE = UI_DIR / "admin.html"
TWEETS_PAGE = UI_DIR / "tweets.html"
AI_PAGE = UI_DIR / "ai.html"
AI_LOGS_PAGE = UI_DIR / "ai_logs.html"
ACCOUNT_PAGE = UI_DIR / "account.html"
REPLIES_PAGE = UI_DIR / "replies.html"


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/console", status_code=307)


@router.get("/console", include_in_schema=False)
async def console() -> FileResponse:
    return FileResponse(ADMIN_PAGE)


@router.get("/console/tweets", include_in_schema=False)
async def console_tweets() -> FileResponse:
    return FileResponse(TWEETS_PAGE)


@router.get("/console/ai", include_in_schema=False)
async def console_ai() -> FileResponse:
    return FileResponse(AI_PAGE)


@router.get("/console/ai/logs", include_in_schema=False)
async def console_ai_logs() -> FileResponse:
    return FileResponse(AI_LOGS_PAGE)


@router.get("/console/accounts/{account_id}", include_in_schema=False)
async def console_account_detail(account_id: str) -> FileResponse:
    return FileResponse(ACCOUNT_PAGE)


@router.get("/console/replies", include_in_schema=False)
async def console_replies() -> FileResponse:
    return FileResponse(REPLIES_PAGE)


@router.get("/ui/{filename:path}", include_in_schema=False)
async def ui_static(filename: str) -> FileResponse:
    """Serve static assets (CSS, JS, images) from the ui/ directory."""
    file_path = UI_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail=f"Static file not found: {filename}")
    return FileResponse(file_path)
