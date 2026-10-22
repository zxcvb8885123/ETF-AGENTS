# reporting｜Research Report V0

整合相同 Snapshot／cutoff 的已驗證研究 artifact，確定性建立同源 `ResearchReport` JSON 與 Markdown；Validator 以原始輸入重建整份報告並拒絕任何改寫。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出公開介面 | 見下方 |
| `contracts.py` | 狀態、禁用欄位、免責聲明與欄位解析 helper | `ResearchReportError` |
| `builder.py` | 由已驗證 artifact 確定性建立 `ResearchReport` | `ResearchReportBuilder` |
| `renderer.py` | 將 JSON 轉為同源 Markdown | `ResearchReportMarkdownRenderer` |
| `validator.py` | 以原始輸入重建整份報告並比對 | `ResearchReportValidator` |
| `service.py` | 對外應用服務 | `ResearchReportApplicationService` |

## 對外介面

`ResearchReportApplicationService`、`ResearchReportBuilder`、`ResearchReportError`、`ResearchReportMarkdownRenderer`、`ResearchReportValidator`。

## 上下游

- 上游：只依賴 `core`（以 import 檢查）；輸入 artifact 由呼叫者以檔案傳入。
- 下游：`cli/research_report.py`。
- 讀：相同 Snapshot／cutoff 的已驗證研究 artifact。寫：由 CLI 指定的 JSON／Markdown。
- 定位：獨立的研究報告；每日決策鏈的最終交付由 `decision_delivery` 負責。

## 對應的 CLI／scripts／測試

- CLI：`cli/research_report.py`。
- 測試：`tests/research/test_research_report.py`。

## 規則與邊界

- `MarketPerceptionResult` 與 `PerceptionDataBundle` 必須成對提供；缺少時產生明確降級報告。
- Builder 不增加市場結論。
- 報告不得包含配置、權重、股數、訂單，也不得聲稱自己是正式 D-Plan（禁用欄位在 `contracts.py`）。
- Validator 重建不一致即拒絕。
