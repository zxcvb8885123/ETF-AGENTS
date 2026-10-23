"""評測的版本、錯誤型別與常數。"""

from __future__ import annotations


EVALUATION_SCHEMA_VERSION = "1.0"
COVERAGE_SCHEMA_VERSION = "1.0"
STOP_STAGES = ("analysts", "full")
# 比較的欄位：分析師逐檔 outlook、交易逐檔 intent／conviction、現金姿態與委託股票。
ANALYSTS = ("technical", "fundamental", "event")


class EvaluationError(ValueError):
    """評測輸入不完整或無法比較時停止，不產生假的分數。"""
