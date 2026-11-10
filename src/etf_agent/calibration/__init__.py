"""校準旁觀者：記錄已封存決策、價格到齊後對答案，審計決策價與成交均價的差距，並統計買進意圖的落地率。

只讀已封存的 Decision run 與官方價量；不被 decision、automation 或風控匯入，
不回寫任何決策 artifact。
"""

from .contracts import HORIZONS, MIN_DECISION_DAYS, CalibrationError
from .gap_audit import execution_gap_report
from .intent_landing import intent_landing_report
from .repository import CalibrationRepository
from .service import build_observation, evaluate, record_all, record_day
from .shadow_swap_report import shadow_swap_report

__all__ = [
    "HORIZONS",
    "MIN_DECISION_DAYS",
    "CalibrationError",
    "CalibrationRepository",
    "build_observation",
    "evaluate",
    "execution_gap_report",
    "intent_landing_report",
    "record_all",
    "record_day",
    "shadow_swap_report",
]
