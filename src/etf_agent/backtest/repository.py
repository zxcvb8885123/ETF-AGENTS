"""Immutable on-disk storage for reproducible backtest runs."""

from __future__ import annotations

from pathlib import Path

from etf_agent.core.artifact_store import ImmutableRunStore

from .contracts import BACKTEST_SCHEMA_VERSION, BacktestToolError


class BacktestRepository(ImmutableRunStore):
    def __init__(self, root: Path):
        super().__init__(root, schema_version=BACKTEST_SCHEMA_VERSION, error=BacktestToolError)
