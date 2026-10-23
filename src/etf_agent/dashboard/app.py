"""FastAPI 唯讀績效儀表板：`/` 為頁面，`/api/performance` 為帳戶 JSON，`/api/benchmarks` 為基準比較 JSON。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository

from .performance import PerformanceError, build_performance_summary

PAGE = Path(__file__).with_name("page.html")


def create_app(
    repository_root: Path,
    account_id: str,
    decision_repository: Optional[Path] = None,
    database: Optional[Path] = None,
    rules_path: Optional[Path] = None,
    benchmarks_path: Optional[Path] = None,
    root: Optional[Path] = None,
) -> FastAPI:
    """``database``、``rules_path``、``benchmarks_path`` 與 ``root`` 齊全時才提供基準比較，否則 `/api/benchmarks` 回 unavailable。"""
    repository = VirtualAccountRepository(repository_root, account_id)
    app = FastAPI(title="ETF Agent 績效儀表板", docs_url=None, redoc_url=None)

    @app.get("/api/performance")
    def performance() -> dict:
        try:
            return build_performance_summary(repository, decision_repository)
        except (PerformanceError, VirtualAccountError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/benchmarks")
    def benchmarks() -> dict:
        from etf_agent.benchmark import BenchmarkError, build_benchmark_comparison  # 延後匯入避免循環相依

        missing = [name for name, value in (("database", database), ("rules", rules_path), ("benchmarks", benchmarks_path), ("root", root)) if value is None]
        if missing:
            return {"status": "unavailable", "account_id": account_id, "reason": "儀表板未設定基準比較所需路徑：%s" % "、".join(missing)}
        try:
            return build_benchmark_comparison(repository, database, rules_path, benchmarks_path, root)
        except (BenchmarkError, VirtualAccountError, PerformanceError) as error:
            return {"status": "unavailable", "account_id": account_id, "reason": str(error)}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.read_text(encoding="utf-8")

    return app
