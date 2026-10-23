"""Agent 評測：資料抓齊率、固定評測集重跑、與參考決策的一致度和重跑穩定度，以及零 token 的多日 Policy 回放。"""

from .answers import answer_key, pool_scores, score_day, score_runs
from .compare import compare_maps, compare_outputs, load_outputs, pairwise_stability, process_stats, summarize
from .contracts import EVALUATION_SCHEMA_VERSION, REPLAY_SCHEMA_VERSION, EvaluationError, ReplayError
from .coverage import data_coverage, document_recall, price_coverage, universe_coverage
from .replay import REPLAY_ROOT, ReplayRepository, build_replay, read_replay, save_replay, select_runs
from .replay_report import compare_replays, render_comparison, render_replay_report
from .replay_tools import nav_metrics
from .replay_validator import verify_replay
from .service import build_suite, eligible_case, eval_policy_template, evaluate, render_report, run_suite

__all__ = [
    "EVALUATION_SCHEMA_VERSION",
    "REPLAY_ROOT",
    "REPLAY_SCHEMA_VERSION",
    "EvaluationError",
    "ReplayError",
    "ReplayRepository",
    "answer_key",
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
    "read_replay",
    "render_comparison",
    "render_replay_report",
    "render_report",
    "run_suite",
    "save_replay",
    "score_day",
    "score_runs",
    "select_runs",
    "summarize",
    "universe_coverage",
    "verify_replay",
]
