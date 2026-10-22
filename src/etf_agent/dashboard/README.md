# dashboard｜虛擬帳戶績效儀表板

唯讀彙整已驗證的虛擬帳本 run 鏈（本金、NAV、報酬率、持倉損益），以 FastAPI 提供頁面與 JSON；不寫入帳本、不補假值。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出績效彙整 | `PerformanceError`、`build_performance_summary` |
| `performance.py` | 讀 `VirtualAccountRepository` 已封存狀態，彙整績效摘要；斷鏈或驗證失敗直接拋錯 | `PerformanceError`、`build_performance_summary` |
| `app.py` | FastAPI 應用：`/` 回傳 `page.html`，`/api/performance` 回傳 JSON | `create_app` |
| `page.html` | 儀表板靜態頁面（非 Python） | — |

## 對外介面

`__init__.py`：`PerformanceError`、`build_performance_summary`。`create_app` 需從 `etf_agent.dashboard.app` 匯入。

## 上下游

- 上游（依 import）：`virtual_account`（`VirtualAccountRepository`、`VirtualAccountError`）、`decision`（`performance.py`）。
- 下游：`cli/dashboard.py`（`./start.sh` 無參數時啟動）。
- 讀：虛擬帳本資料夾（預設 `artifacts/ledger/`）與選配的決策封存資料夾；不寫入。
- `/api/performance` 遇到 `PerformanceError` 或 `VirtualAccountError` 回 HTTP 409 並附錯誤訊息。

## 對應的 CLI／scripts／測試

- CLI：`cli/dashboard.py`；入口為根目錄 `start.sh`。
- 測試：`tests/automation/test_dashboard.py`，`tests/account/test_virtual_account_daily.py` 亦有引用。

## 規則與邊界

- 唯讀：不更新資料、不執行決策、不修改帳本。
- 數值必須是有限 Decimal，否則 `PerformanceError`。
- 帳本鏈驗證失敗時不顯示部分結果。
