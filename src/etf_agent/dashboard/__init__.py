"""虛擬帳戶績效儀表板：唯讀彙整帳本並以 FastAPI 呈現。"""

from .performance import PerformanceError, build_performance_summary

__all__ = ["PerformanceError", "build_performance_summary"]
