"""評測的版本、錯誤型別與常數。"""

from __future__ import annotations


EVALUATION_SCHEMA_VERSION = "1.0"
COVERAGE_SCHEMA_VERSION = "1.0"
STOP_STAGES = ("analysts", "full")
# 比較的欄位：分析師逐檔 outlook、交易逐檔 intent／conviction、現金姿態與委託股票。
ANALYSTS = ("technical", "fundamental", "event")


class EvaluationError(ValueError):
    """評測輸入不完整或無法比較時停止，不產生假的分數。"""

# 便宜版回測（多日滾動回放）：只重算確定性程式，不呼叫 Agent。
REPLAY_SCHEMA_VERSION = "1.0"
# 日報酬樣本少於此數不給 Sharpe；交易日少於此數的年化只供參考，報告標示不可靠。
MIN_SHARPE_SAMPLES = 20
MIN_ANNUALIZE_SAMPLES = 60
TRADING_DAYS_PER_YEAR = 252
REPLAY_DAY_STATUSES = ("executed", "pending_execution", "gap")


class ReplayError(EvaluationError):
    """回放輸入不完整、封存驗證失敗、缺日、缺價或出現前視價格時停止，不補假值。"""
