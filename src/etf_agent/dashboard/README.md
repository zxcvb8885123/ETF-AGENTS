# dashboard｜虛擬帳戶績效儀表板

唯讀彙整已驗證的虛擬帳本 run 鏈（本金、NAV、報酬率、持倉損益），並顯示帳戶對基準（0050 買入持有、150 檔等權買入持有）的比較，以 FastAPI 提供頁面與 JSON；不寫入帳本、不補假值。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出績效彙整 | `PerformanceError`、`build_performance_summary` |
| `performance.py` | 讀 `VirtualAccountRepository` 已封存狀態，彙整績效摘要；斷鏈或驗證失敗直接拋錯 | `PerformanceError`、`build_performance_summary` |
| `app.py` | FastAPI 應用：`/` 回傳 `page.html`，`/api/performance` 回傳帳戶 JSON，`/api/benchmarks` 回傳基準比較 JSON（由 `etf_agent.benchmark` 計算） | `create_app` |
| `page.html` | 儀表板靜態頁面（非 Python）：KPI、NAV 走勢（帳戶實線、基準虛線）、與基準比較表、買賣紀錄、持倉、每日紀錄 | — |

## 對外介面

`__init__.py`：`PerformanceError`、`build_performance_summary`。`create_app(repository_root, account_id, decision_repository=None, database=None, rules_path=None, benchmarks_path=None, root=None)` 需從 `etf_agent.dashboard.app` 匯入；後四個參數齊全時才提供基準比較，否則 `/api/benchmarks` 回 `{"status": "unavailable", "reason": …}`。

## 上下游

- 上游（依 import）：`virtual_account`（`VirtualAccountRepository`、`VirtualAccountError`）、`decision`（`performance.py`）、`benchmark`（`app.py` 延後匯入）。
- 下游：`cli/dashboard.py`（`./start.sh` 無參數時啟動）；`benchmark.service` 讀 `build_performance_summary` 取得帳戶 NAV 序列。
- 讀：虛擬帳本資料夾（預設 `artifacts/ledger/`）、選配的決策封存資料夾，以及基準比較用的 `var/etf_agent.db`（唯讀 `mode=ro`，不觸發遷移）、`config/decision_rules.json`、`config/benchmarks.json`、`data/official_universe.csv`；不寫入。
- `/api/performance` 遇到 `PerformanceError` 或 `VirtualAccountError` 回 HTTP 409 並附錯誤訊息；`/api/benchmarks` 遇到資料不足或路徑未設定時回 HTTP 200 的 `unavailable` 與原因，不影響帳戶摘要。

## 對應的 CLI／scripts／測試

- CLI：`cli/dashboard.py`；入口為根目錄 `start.sh`。同一份比較可由 `cli/benchmark.py compare` 在終端機輸出。
- 測試：`tests/automation/test_dashboard.py`、`tests/benchmark/test_benchmark_comparison.py`（`/api/benchmarks`），`tests/account/test_virtual_account_daily.py` 亦有引用。

## 規則與邊界

- 唯讀：不更新資料、不執行決策、不修改帳本。
- 數值必須是有限 Decimal，否則 `PerformanceError`。
- 帳本鏈驗證失敗時不顯示部分結果。
- 基準比較只用 as-of 時點已可得、已定案的官方日線；某基準資料不足時該列顯示 unavailable 與原因。
