"""隔夜市場背景：美股指數、台股代理、日韓指數、VIX 與台指期夜盤的確定性結果（1.0 只有美股與台股代理），只作現金姿態與報告背景。"""
from .contracts import OvernightMarketError
from .provider import OvernightProvider
from .service import (
    attach_overnight_market, build_overnight_market_input, capture_overnight_market, latest_captures, load_approvals,
)
from .tools import evaluate
from .validator import validate_pair

__all__ = ["OvernightMarketError", "OvernightProvider", "attach_overnight_market", "build_overnight_market_input",
           "capture_overnight_market", "evaluate", "latest_captures", "load_approvals", "validate_pair"]
