"""補抓、保存與接入隔夜市場輸入；不呼叫模型，不自動核准來源。"""
import json
from datetime import datetime, timezone
from pathlib import Path

from etf_agent.core import canonical_sha256, parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from .contracts import PACK_VERSION, OvernightMarketError, seal
from .provider import OvernightProvider
from .tools import evaluate
from .validator import validate_pair

CAPTURE_SCHEMA = "overnight-market-capture-1.0"


def _store(output_root):
    return ImmutableRunStore(Path(output_root), schema_version=CAPTURE_SCHEMA, error=OvernightMarketError, dated=True)


def capture_overnight_market(output_root, provider=None, now=None):
    """抓取全部標的並保存不可變原始回應；個別標的失敗只記錄，不中止。"""
    provider = provider or OvernightProvider()
    now = now or datetime.now(timezone.utc)
    captures = provider.capture_all(now)
    run_id = "overnight-%s-%s" % (now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), canonical_sha256(captures)[:12])
    store = _store(output_root)
    path = store.save(run_id, {"captures": {"items": captures}})
    store.verify(run_id)
    return {"run_path": str(path), "captured": len(captures),
            "failures": [{"symbol": c["symbol"], "url": c["url"], "reason": c["error"]} for c in captures if c["status"] != "fetched"]}


def load_approvals(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OvernightMarketError("來源核准檔無法讀取：%s" % path) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise OvernightMarketError("來源核准檔缺少 sources 陣列")
    return payload["sources"]


def latest_captures(output_root, decision_cutoff):
    """截止前最後一次完整保存的抓取；全部 available_at 都必須不晚於截止時間。"""
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=OvernightMarketError)
    store, best = _store(output_root), None
    for manifest in sorted(Path(output_root).glob("*/overnight-*/manifest.json")):
        run_id = manifest.parent.name
        store.verify(run_id)
        items = json.loads((manifest.parent / "captures.json").read_text(encoding="utf-8"))["items"]
        latest = max(parse_aware_time(c["available_at"], "available_at", error=OvernightMarketError) for c in items)
        if latest <= cutoff and (best is None or latest > best[0]):
            best = (latest, items)
    return best[1] if best else None


def build_overnight_market_input(output_root, approvals_path, snapshot_id, decision_cutoff, latest_trade_date):
    """建立 {bundle, result} 成對輸入；截止前沒有任何抓取時回傳 None。"""
    captures = latest_captures(output_root, decision_cutoff)
    if captures is None:
        return None
    pack = seal({"schema_version": PACK_VERSION, "snapshot_id": snapshot_id, "decision_cutoff": decision_cutoff,
                 "latest_trade_date": latest_trade_date, "captures": captures,
                 "source_approvals": load_approvals(approvals_path)})
    return {"bundle": pack, "result": evaluate(pack)}


def attach_overnight_market(decision_input, pair):
    from etf_agent.decision.contracts import decision_bundle_sha256
    validate_pair(pair, decision_input["snapshot_id"], decision_input["decision_cutoff"],
                  decision_input.get("snapshot", {}).get("latest_trade_date"))
    output = dict(decision_input, overnight_market_input=pair)
    output["bundle_sha256"] = decision_bundle_sha256(output)
    return output
