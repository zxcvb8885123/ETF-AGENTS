# runtime｜管線紀錄基礎

提供不綁定任何 Provider 的小型積木，用來建立帶內容雜湊的管線 run 紀錄；只記錄執行身分，不做任何市場判斷。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出管線紀錄函式 | `PipelineRuntimeError`、`build_pipeline_run`、`content_sha256` |
| `pipeline.py` | 驗證必填字串欄位、stages／errors 型別後，加上 `content_sha256` | `PipelineRuntimeError`、`build_pipeline_run` |

## 對外介面

`PipelineRuntimeError`、`build_pipeline_run`、`content_sha256`（後者轉匯出自 `etf_agent.core`）。

## 上下游

- 上游：只依賴 `etf_agent.core`。
- 下游：無。`src/`、`cli/`、`scripts/`、`tests/` 沒有任何模組 import `etf_agent.runtime`，也沒有測試。
- 每日流程的 `pipeline.json` 由 `automation.daily_pipeline` 自行建立，不使用此模組。

## 對應的 CLI／scripts／測試

- CLI／scripts：無。
- 測試：無。

## 規則與邊界

- `schema_version`、`pipeline_run_id`、`execution_mode`、`status`、`started_at`、`ended_at` 必須是非空字串，否則拋出 `PipelineRuntimeError`。
- `stages` 必須是物件陣列、`errors` 必須是非空字串陣列。
- 此模組是預留的邊界，尚未接入每日流程，不是現行 `pipeline.json` 的產生者。
