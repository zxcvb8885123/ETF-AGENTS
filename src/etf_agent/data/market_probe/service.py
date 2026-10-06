"""補抓後保存不可變原始回應及重建事實。"""

from datetime import datetime, timezone
from etf_agent.core import canonical_sha256
from etf_agent.core.artifact_store import ImmutableRunStore
from .contracts import SOURCES
from .provider import OfficialMarketProbeProvider
from .tools import market_context_facts


def capture_market_context(trade_date, output_root, provider=None):
    provider = provider or OfficialMarketProbeProvider()
    captures = [provider.capture(source, trade_date) for source in SOURCES]
    cutoff = datetime.now(timezone.utc).isoformat()
    facts = market_context_facts(captures, cutoff)
    run_id = "market-"+canonical_sha256({"captures": captures, "facts": facts})[:20]
    store = ImmutableRunStore(output_root, schema_version="market-probe-run-1.0")
    path = store.save(run_id, {"captures": {"items": captures}, "facts": facts})
    store.verify(run_id)
    return {"run_dir": str(path), "decision_cutoff": cutoff, "facts": len(facts["facts"]), "failures": facts["failures"], "formal_sentiment_available": False}
