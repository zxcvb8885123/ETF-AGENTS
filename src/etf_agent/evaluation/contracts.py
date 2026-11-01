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


# 回測格子：每筆決策一列（run × symbol），按評級分組統計兩種超額報酬（交易池平均／基準 ETF），零 token。
GRID_SCHEMA_VERSION = "1.0"
DEFAULT_BENCHMARK_SYMBOL = "0050.TW"
# 本輪只收已封存、manifest 驗證通過的 Decision run；「往更早歷史重建」尚未實作，保留此欄位供日後標示。
GRID_PROVENANCE_CLASSES = ("archived", "reconstructed")
GRID_BENCHMARK_STATUSES = ("available", "unavailable", "ex_rights")


class GridError(EvaluationError):
    """格子輸入不合法、封存驗證失敗、as_of 缺時區或重算不一致時停止，不補假值。"""
