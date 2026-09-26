"""Portfolio decision contracts, momentum tools, and intent validation."""

from .contracts import (
    DECISION_SCHEMA_VERSION,
    DecisionContext,
    DecisionInputValidator,
    DecisionToolError,
    canonical_sha256,
    decision_bundle_sha256,
    decision_rules_sha256,
    artifact_content_sha256,
)
from .input_builder import DEFAULT_LOOKBACK_BARS, DecisionInputBuilder
from .momentum import MomentumEngine, MomentumResultValidator
from .role_brief import build_role_brief
from .analysts import (
    ANALYSTS,
    AnalystReportValidator,
    build_analyst_brief,
    compute_fundamental_metrics,
    high_materiality_events,
    seal_report,
    sentiment_unavailable_report,
)
from .stance import (
    STANCE_ROLES,
    ResearchDebateBundleValidator,
    StancePacketValidator,
    build_research_debate,
    build_stance_brief,
    seal_stance_packet,
)
from .trader import (
    TRADE_INTENTS,
    TradeDecisionValidator,
    allocation_intents,
    apply_trade_decision,
    seal_trade_decision,
    trade_decision_envelope,
)
from .sizing import SIZING_METHOD, SizingPlanValidator, apply_sizing_plan
from .policy_builder import build_decision_policy
from .trade_intent import (
    BuyIntentPacketValidator,
    SellIntentPacketValidator,
    TradeDebateValidator,
    TradeIntentResultValidator,
    build_role_input_artifact,
)
from .allocation import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    ProposalValidator,
    decision_policy_sha256,
)
from .risk import (
    CompetitionGuardV2,
    GuardValidator,
    RiskReviewValidator,
    ScenarioEngine,
    ScenarioValidator,
    revision_effects,
)
from .finalization import (
    DecisionFinalizer,
    DecisionRepository,
    DecisionResultValidator,
)
from .revision import RevisionHistoryBuilder, RevisionHistoryValidator
from .service import PortfolioDecisionApplicationService

__all__ = [
    "DECISION_SCHEMA_VERSION",
    "DecisionContext",
    "DecisionInputValidator",
    "DecisionToolError",
    "canonical_sha256",
    "decision_bundle_sha256",
    "decision_rules_sha256",
    "artifact_content_sha256",
    "DEFAULT_LOOKBACK_BARS",
    "DecisionInputBuilder",
    "MomentumEngine",
    "MomentumResultValidator",
    "BuyIntentPacketValidator",
    "SellIntentPacketValidator",
    "TradeDebateValidator",
    "TradeIntentResultValidator",
    "build_role_input_artifact",
    "build_role_brief",
    "ANALYSTS",
    "AnalystReportValidator",
    "build_analyst_brief",
    "compute_fundamental_metrics",
    "high_materiality_events",
    "seal_report",
    "sentiment_unavailable_report",
    "STANCE_ROLES",
    "ResearchDebateBundleValidator",
    "StancePacketValidator",
    "build_research_debate",
    "build_stance_brief",
    "seal_stance_packet",
    "TRADE_INTENTS",
    "TradeDecisionValidator",
    "allocation_intents",
    "apply_trade_decision",
    "seal_trade_decision",
    "trade_decision_envelope",
    "SIZING_METHOD",
    "SizingPlanValidator",
    "apply_sizing_plan",
    "build_decision_policy",
    "AllocationOrderEngine",
    "DecisionPolicyValidator",
    "ProposalValidator",
    "decision_policy_sha256",
    "ScenarioEngine",
    "ScenarioValidator",
    "CompetitionGuardV2",
    "GuardValidator",
    "RiskReviewValidator",
    "revision_effects",
    "DecisionFinalizer",
    "DecisionResultValidator",
    "DecisionRepository",
    "RevisionHistoryBuilder",
    "RevisionHistoryValidator",
    "PortfolioDecisionApplicationService",
]
