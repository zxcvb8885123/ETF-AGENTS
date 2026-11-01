"""Agent 評測：資料抓齊率、固定評測集重跑（可續跑）、與參考決策的一致度和重跑穩定度、零 token 的多日 Policy 回放、
回測格子（run × symbol 兩種超額報酬）與一頁自包含 HTML 報告。"""

from .answers import answer_key, pool_scores, score_day, score_runs
from .compare import compare_maps, compare_outputs, load_outputs, pairwise_stability, process_stats, summarize
from .contracts import (
    DEFAULT_BENCHMARK_SYMBOL,
    EVALUATION_SCHEMA_VERSION,
    GRID_SCHEMA_VERSION,
    REPLAY_SCHEMA_VERSION,
    EvaluationError,
    GridError,
    ReplayError,
)
from .coverage import data_coverage, document_recall, price_coverage, universe_coverage
from .grid import GRID_ROOT, GridRepository, build_grid, read_grid, save_grid, verify_grid
from .html_report import render_eval_html, render_grid_html, render_replay_html
from .replay import REPLAY_ROOT, ReplayRepository, build_replay, read_replay, save_replay, select_runs
from .replay_report import compare_replays, render_comparison, render_replay_report
from .replay_tools import nav_metrics
from .replay_validator import verify_replay
from .service import build_suite, eligible_case, eval_policy_template, evaluate, render_report, run_suite, unfinished_runs

__all__ = [
    "DEFAULT_BENCHMARK_SYMBOL",
    "EVALUATION_SCHEMA_VERSION",
    "GRID_ROOT",
    "GRID_SCHEMA_VERSION",
    "REPLAY_ROOT",
    "REPLAY_SCHEMA_VERSION",
    "EvaluationError",
    "GridError",
    "GridRepository",
    "ReplayError",
    "ReplayRepository",
    "answer_key",
    "build_grid",
    "build_replay",
    "build_suite",
    "compare_replays",
    "compare_maps",
    "compare_outputs",
    "data_coverage",
    "document_recall",
    "eligible_case",
    "eval_policy_template",
    "evaluate",
    "load_outputs",
    "nav_metrics",
    "pairwise_stability",
    "pool_scores",
    "price_coverage",
    "process_stats",
    "read_grid",
    "read_replay",
    "render_comparison",
    "render_eval_html",
    "render_grid_html",
    "render_replay_html",
    "render_replay_report",
    "render_report",
    "run_suite",
    "save_grid",
    "save_replay",
    "score_day",
    "score_runs",
    "select_runs",
    "summarize",
    "unfinished_runs",
    "universe_coverage",
    "verify_grid",
    "verify_replay",
]
