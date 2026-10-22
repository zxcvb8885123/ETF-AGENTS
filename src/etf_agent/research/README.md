# research｜Event Research Agent（獨立研究與舊封存）

Event Research Agent 的契約、確定性工具與驗證：Fact／Bull／Bear／Adjudicator 四個子 Agent 的辯論與 `ResearchResult` 驗證，以及 cutoff 前可得的歷史價格特徵。這是獨立研究與舊封存使用的流程，**不是目前每日決策鏈**（每日鏈的事件由分析團隊的事件分析師處理，見 [docs/architecture.md](../../../docs/architecture.md)）。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出公開介面 | 見下方 |
| `contracts.py` | 列舉、錯誤型別與欄位解析 helper | `ResearchToolError` |
| `tools.py` | 對 Snapshot 的確定性、時間點一致工具 | `SnapshotResearchTools` |
| `repository.py` | 以 cutoff 前可得的歷史行情計算事件研究用價格特徵 | `HistoricalPriceFeatureRepository` |
| `validator.py` | `ResearchResult` 與 Fact／Bull／Bear／Adjudicator 辯論的完整驗證 | `ResearchResultValidator`、`ResearchDebateValidator` |
| `service.py` | 對外應用服務 | `EventResearchApplicationService` |

## 對外介面

`EventResearchApplicationService`、`HistoricalPriceFeatureRepository`、`ResearchDebateValidator`、`ResearchResultValidator`、`ResearchToolError`、`SnapshotResearchTools`。

## 上下游

- 上游（依 import）：`core`、`data`（`repository.py`、`tools.py`）。
- 下游：`decision`（`contracts.py`、`finalization.py` 驗證舊的 `ResearchResult` 與重建）、`automation.daily_pipeline`（`ResearchResultValidator`）、`cli/event_research.py`。
- 讀：`ResearchSnapshot` 與資料庫的歷史行情。寫：由 CLI 指定的研究結果。

## 對應的 CLI／scripts／測試

- CLI：`cli/event_research.py`。
- 測試：`tests/research/test_event_research_agent.py`。

## 規則與邊界

- Fact 只整理可驗證事實；Bull 與 Bear 只讀同一 FactPacket，彼此不得先讀對方輸出；Adjudicator 不得新增事實。
- `ResearchResult` 必須通過 debate 與 result validator 才能交給下游。
- MoM、YoY 與歷史趨勢不是市場共識，也不能單獨形成方向。
- 工具以 `decision_cutoff` 之前可得的資料為界（`tools.py` 引用 `available_at`）；價格特徵只用 cutoff 前的歷史行情。
