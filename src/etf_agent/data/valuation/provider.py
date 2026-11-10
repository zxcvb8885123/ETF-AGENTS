"""估值資料來源：證交所 BWIBBU_ALL（當日、無歷史）與 FinMind TaiwanStockPER（有歷史）。"""

from __future__ import annotations

import urllib.error
import urllib.request
from datetime import date
from typing import Callable

from etf_agent.data.supplemental_sources import FinMindApiProvider

from .contracts import FINMIND_DATASET, OFFICIAL_ENDPOINT, ValuationError

# 通用客戶端名稱，不含任何使用者或專案識別。
USER_AGENT = "etf-agent-data-client/1.0"


class OfficialValuationProvider:
    def __init__(self, opener: Callable = urllib.request.urlopen, endpoint: str = OFFICIAL_ENDPOINT):
        self.opener = opener
        self.endpoint = endpoint

    def fetch(self) -> str:
        request = urllib.request.Request(self.endpoint, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with self.opener(request, timeout=30) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise ValuationError("證交所估值端點 HTTP %d" % error.code) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise ValuationError("證交所估值端點連線失敗（%s）" % type(error).__name__) from None


class FinMindValuationProvider(FinMindApiProvider):
    """沿用既有 FinMind 取用方式（金鑰只從 FINMIND_TOKEN 環境變數取得，不讀 .env 檔）。"""

    allowed_datasets = frozenset({FINMIND_DATASET})
    dataset_error = "FinMind 資料集未列入估值白名單"

    def fetch_per(self, code: str, start: date, end: date) -> str:
        return self.fetch(FINMIND_DATASET, code, start, end)
