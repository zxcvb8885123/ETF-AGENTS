"""官方公司資料（重大訊息、月營收、財報）的相容轉出層。

實作已依職責拆分；既有 ``etf_agent.data.corporate`` import 路徑維持可用：

- ``corporate_contracts``：資料契約（dataclass）與常數。
- ``corporate_parsing``：民國日期、欄位與財報事實的確定性解析。
- ``corporate_provider``：官方 OpenAPI Provider 與 Factory。
- ``corporate_collector``：收集流程與 SQLite 保存。
"""

from .corporate_collector import CorporateDataCollector
from .corporate_contracts import (
    FINANCIAL_MAPPING_VERSION,
    TAIPEI_TIMEZONE,
    CorporateCollectionResult,
    CorporateRecord,
    CorporateResponse,
    FinancialStatement,
    FinancialStatementFact,
    MonthlyRevenue,
    SourceDocument,
)
from .corporate_parsing import _is_empty_financial_placeholder
from .corporate_provider import OfficialCorporateProvider, OfficialCorporateProviderFactory

__all__ = [
    "FINANCIAL_MAPPING_VERSION",
    "TAIPEI_TIMEZONE",
    "CorporateCollectionResult",
    "CorporateDataCollector",
    "CorporateRecord",
    "CorporateResponse",
    "FinancialStatement",
    "FinancialStatementFact",
    "MonthlyRevenue",
    "OfficialCorporateProvider",
    "OfficialCorporateProviderFactory",
    "SourceDocument",
]
