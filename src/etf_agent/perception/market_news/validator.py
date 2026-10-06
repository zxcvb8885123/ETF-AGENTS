"""以原始 RSS 與全量標籤重建成對研究輸入。"""
from etf_agent.core import parse_aware_time
from .contracts import MarketNewsError,check_fields
from .tools import evaluate

def validate_pair(pair,snapshot_id,decision_cutoff):
    check_fields(pair,{'bundle','result'},'全市場新聞輸入')
    pack=pair['bundle']
    if not isinstance(pack,dict): raise MarketNewsError('全市場新聞資料包必須是物件')
    if pack.get('snapshot_id') != snapshot_id or parse_aware_time(pack.get('decision_cutoff'),'decision_cutoff',error=MarketNewsError) != parse_aware_time(decision_cutoff,'decision_cutoff',error=MarketNewsError):
        raise MarketNewsError('全市場新聞 Snapshot 或截止時間不一致')
    result=evaluate(pack)
    if result != pair['result']: raise MarketNewsError('全市場新聞結果與原始資料重建不一致')
    return result
