"""虛擬帳戶績效對基準（0050 買入持有、交易池等權買入持有）的確定性比較。"""

from .contracts import BenchmarkError, FeeSchedule, OfficialPriceRow
from .repository import BenchmarkPriceRepository
from .service import METHOD, build_benchmark_comparison
from .tools import buy_and_hold, cumulative_return, max_drawdown, relative_drawdown, valuation, whole_lot_buy

__all__ = [
    "METHOD",
    "BenchmarkError",
    "BenchmarkPriceRepository",
    "FeeSchedule",
    "OfficialPriceRow",
    "build_benchmark_comparison",
    "buy_and_hold",
    "cumulative_return",
    "max_drawdown",
    "relative_drawdown",
    "valuation",
    "whole_lot_buy",
]
