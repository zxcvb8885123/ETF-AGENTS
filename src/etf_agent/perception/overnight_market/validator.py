"""以原始回應與核准紀錄重建成對輸入，拒絕任何改寫。"""
from etf_agent.core import parse_aware_time
from .contracts import OvernightMarketError, check_fields
from .tools import evaluate


def validate_pair(pair, snapshot_id, decision_cutoff, latest_trade_date=None):
    check_fields(pair, {"bundle", "result"}, "隔夜市場輸入")
    pack = pair["bundle"]
    if not isinstance(pack, dict):
        raise OvernightMarketError("隔夜市場資料包必須是物件")
    if pack.get("snapshot_id") != snapshot_id or parse_aware_time(
            pack.get("decision_cutoff"), "decision_cutoff", error=OvernightMarketError) != parse_aware_time(
            decision_cutoff, "decision_cutoff", error=OvernightMarketError):
        raise OvernightMarketError("隔夜市場 Snapshot 或截止時間不一致")
    if latest_trade_date is not None and pack.get("latest_trade_date") != latest_trade_date:
        raise OvernightMarketError("隔夜市場資料包的台股最近交易日與 Snapshot 不一致")
    result = evaluate(pack)
    if result != pair["result"]:
        raise OvernightMarketError("隔夜市場結果與原始資料重建不一致")
    return result
