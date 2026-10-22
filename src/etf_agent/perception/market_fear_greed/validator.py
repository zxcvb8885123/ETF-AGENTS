"""以原始回應與核准紀錄重建成對輸入，拒絕任何改寫。"""
from etf_agent.core import parse_aware_time
from .contracts import FearGreedError, check_fields
from .tools import evaluate


def validate_pair(pair, snapshot_id, decision_cutoff):
    check_fields(pair, {"bundle", "result"}, "恐懼貪婪輸入")
    pack = pair["bundle"]
    if not isinstance(pack, dict):
        raise FearGreedError("恐懼貪婪資料包必須是物件")
    if pack.get("snapshot_id") != snapshot_id or parse_aware_time(
            pack.get("decision_cutoff"), "decision_cutoff", error=FearGreedError) != parse_aware_time(
            decision_cutoff, "decision_cutoff", error=FearGreedError):
        raise FearGreedError("恐懼貪婪 Snapshot 或截止時間不一致")
    result = evaluate(pack)
    if result != pair["result"]:
        raise FearGreedError("恐懼貪婪結果與原始資料重建不一致")
    return result
