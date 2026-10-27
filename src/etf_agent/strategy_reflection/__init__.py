"""策略反思旁觀者：讀校準報告，提出旋鈕調整；通過驗證與影子回放後由程式原子套用。"""

from .brief import build_brief
from .contracts import COOLDOWN_DECISION_DAYS, KNOBS, ReflectionError
from .repository import ReflectionRepository
from .service import (
    agent_task, apply_reflection, default_replayer, from_agent_output, make_brief, observe_only, replay_gate, rollback, run_auto,
    save_reflection,
)
from .tools import apply_proposals, check_proposal, current_knobs, validate_template
from .validator import validate_reflection

__all__ = [
    "COOLDOWN_DECISION_DAYS", "KNOBS", "ReflectionError", "ReflectionRepository", "agent_task", "apply_proposals",
    "apply_reflection", "build_brief", "check_proposal", "current_knobs", "default_replayer", "from_agent_output",
    "make_brief", "observe_only", "replay_gate", "rollback", "run_auto", "save_reflection", "validate_reflection",
    "validate_template",
]
