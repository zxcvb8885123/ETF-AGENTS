"""補抓、保存與接入恐懼貪婪輸入；不呼叫模型，不自動核准來源。"""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_agent.core import canonical_sha256, parse_aware_time
from etf_agent.core.artifact_store import ImmutableRunStore
from .contracts import PACK_VERSION, FearGreedError, seal
from .provider import FearGreedProvider
from .tools import evaluate
from .validator import validate_pair

CAPTURE_SCHEMA = "market-fear-greed-capture-1.0"


def _store(output_root):
    return ImmutableRunStore(Path(output_root), schema_version=CAPTURE_SCHEMA, error=FearGreedError, dated=True)


def capture_fear_greed(output_root, provider=None, now=None):
    """抓取全部來源並保存不可變原始回應；個別來源失敗只記錄，不中止。"""
    provider = provider or FearGreedProvider()
    now = now or datetime.now(timezone.utc)
    captures = provider.capture_all(now.astimezone(ZoneInfo("Asia/Taipei")).date())
    run_id = "fear-greed-%s-%s" % (now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), canonical_sha256(captures)[:12])
    store = _store(output_root)
    path = store.save(run_id, {"captures": {"items": captures}})
    store.verify(run_id)
    return {"run_path": str(path), "captured": len(captures),
            "failures": [{"source": c["source"], "url": c["url"], "reason": c["error"]} for c in captures if c["status"] != "fetched"]}


def load_approvals(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FearGreedError("來源核准檔無法讀取：%s" % path) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise FearGreedError("來源核准檔缺少 sources 陣列")
    return payload["sources"]


def latest_captures(output_root, decision_cutoff):
    """截止前最後一次完整保存的抓取；全部 available_at 都必須不晚於截止時間。"""
    cutoff = parse_aware_time(decision_cutoff, "decision_cutoff", error=FearGreedError)
    store, best = _store(output_root), None
    for manifest in sorted(Path(output_root).glob("*/fear-greed-*/manifest.json")):
        run_id = manifest.parent.name
        store.verify(run_id)
        items = json.loads((manifest.parent / "captures.json").read_text(encoding="utf-8"))["items"]
        latest = max(parse_aware_time(c["available_at"], "available_at", error=FearGreedError) for c in items)
        if latest <= cutoff and (best is None or latest > best[0]):
            best = (latest, items)
    return best[1] if best else None


def build_fear_greed_input(output_root, approvals_path, snapshot_id, decision_cutoff):
    """建立 {bundle, result} 成對輸入；截止前沒有任何抓取時回傳 None。"""
    captures = latest_captures(output_root, decision_cutoff)
    if captures is None:
        return None
    pack = seal({"schema_version": PACK_VERSION, "snapshot_id": snapshot_id, "decision_cutoff": decision_cutoff,
                 "captures": captures, "source_approvals": load_approvals(approvals_path)})
    return {"bundle": pack, "result": evaluate(pack)}


def attach_fear_greed(decision_input, pair):
    from etf_agent.decision.contracts import decision_bundle_sha256
    validate_pair(pair, decision_input["snapshot_id"], decision_input["decision_cutoff"])
    output = dict(decision_input, market_fear_greed_input=pair)
    output["bundle_sha256"] = decision_bundle_sha256(output)
    return output
