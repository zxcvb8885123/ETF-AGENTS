# fundamentals｜基本面研究工具

唯讀 `ResearchSnapshot` 內已驗證的財報，建立 `FundamentalDataBundle`，由程式確定性計算並重算 `FundamentalMetrics`，再驗證 `FundamentalResearchResult`。財務比率由程式計算，不由 LLM 覆寫。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出公開介面 | 見下方 |
| `contracts.py` | 版本、列舉、錯誤型別與欄位解析 helper | `FundamentalToolError` |
| `tools.py` | 讀 Snapshot 已驗證財報，建立 `FundamentalDataBundle` | `FundamentalSnapshotTools` |
| `metrics.py` | 由資料包確定性計算與重算指標 | `FundamentalMetricsCalculator` |
| `validator.py` | 欄位白名單、引用與重算驗證 | `FundamentalResearchResultValidator` |
| `service.py` | 對外應用服務 | `FundamentalResearchApplicationService` |

## 對外介面

`FundamentalMetricsCalculator`、`FundamentalResearchApplicationService`、`FundamentalResearchResultValidator`、`FundamentalSnapshotTools`、`FundamentalToolError`。

## 上下游

```mermaid
flowchart LR
    SNAP[(ResearchSnapshot)] --> T[tools.py<br/>FundamentalDataBundle]
    T --> M[metrics.py<br/>FundamentalMetrics]
    M --> V[validator.py<br/>重算並驗證]
    V --> OUT[FundamentalResearchResult]
```

- 上游：只依賴 `core`。
- 下游：`decision.analysts`（`compute_fundamental_metrics`）與 `decision.event_financial_context` 引用此套件；`cli/fundamental_research.py`。
- 讀：已封存的 `ResearchSnapshot` JSON。寫：由 CLI 指定輸出。

## 對應的 CLI／scripts／測試

- CLI：`cli/fundamental_research.py`；資料補強另見 `cli/fundamental_repair.py`（屬 `data.fundamental_repair`）與 [docs/fundamental_data_repair.md](../../../docs/fundamental_data_repair.md)。
- 測試：`tests/research/test_fundamental_research_agent.py`；`tests/research/test_fundamental_language.py` 針對 `decision.fundamental_language`。

## 規則與邊界

- 只用 Snapshot 內的資料，並以 `available_at` 判斷當時可用性；資料不足、單位或期間衝突時停止或降級，不填補假值。
- 指標一律由程式計算，Validator 重算並拒絕改寫。
- MoM、YoY 與歷史趨勢不是市場共識，也不能單獨形成方向。
