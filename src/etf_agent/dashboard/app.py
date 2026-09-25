"""FastAPI 唯讀績效儀表板：`/` 為頁面，`/api/performance` 為 JSON。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository

from .performance import PerformanceError, build_performance_summary

PAGE = Path(__file__).with_name("page.html")


def create_app(repository_root: Path, account_id: str) -> FastAPI:
    repository = VirtualAccountRepository(repository_root, account_id)
    app = FastAPI(title="ETF Agent 績效儀表板", docs_url=None, redoc_url=None)

    @app.get("/api/performance")
    def performance() -> dict:
        try:
            return build_performance_summary(repository)
        except (PerformanceError, VirtualAccountError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.read_text(encoding="utf-8")

    return app
