# virtual_account｜競賽虛擬帳戶

可重建、逐日續接的競賽虛擬帳本：初始化帳戶、建立每日決策前快照（`prepare_day`）、以官方當日成交均價結算前一份決策、套用已封存的決策成交。狀態以不可變 run 保存，`latest` 只指向已驗證狀態。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出公開介面 | `VirtualAccountError`、`VirtualAccountRepository`、`VirtualAccountService`、`DEFAULT_ACCOUNT_ID`、`active_account_id`、`write_active_account` |
| `active.py` | 讀寫 `config/active_account.json`（使用中的帳戶 ID）；檔案不存在時用 `ai-cup-2026` | `DEFAULT_ACCOUNT_ID`、`active_account_id`、`write_active_account` |
| `service.py` | 帳本保存／驗證（hash 鏈）與狀態轉移：初始化、`prepare_day`、`apply_decision` | `VirtualAccountError`、`parse_time`、`VirtualAccountRepository`（`save`、`verify`、`latest`、`nav_history(head_run_id=None)`：由指定狀態沿雜湊鏈往回讀 NAV 歷史，供回撤 `history_ref` 重算；預設從 latest）、`VirtualAccountService`（`ensure_initialized`、`initialize`、`prepare_day`、`rebase`、`apply_decision`、`read_snapshot`） |
| `daily.py` | 每日帳本推進：從資料庫取官方成交價建立執行市場，結算前一份決策，建立下一次決策前狀態 | `OfficialCloseMarket`（`execution_date`、`quotes`、`build`）、`DailyAccountRunner`（`settlement_date`、`settle`、`supersede_unstarted_decision`、`void_closed_session_decision`、`run`）、`pending_decision`、`restore_superseded_decision`、`add_business_days`、`average_price`、`find_decision_run`、`supersede_decision_runs` |

## 對外介面

`__init__.py`：`VirtualAccountError`、`VirtualAccountRepository`、`VirtualAccountService`。`DailyAccountRunner`、`OfficialCloseMarket` 需從 `etf_agent.virtual_account.daily` 匯入。

## 上下游

```mermaid
flowchart LR
    DB[(var/etf_agent.db<br/>官方價量)] --> D[daily.py<br/>OfficialCloseMarket]
    DEC[artifacts/decisions<br/>已封存決策] --> S[service.py<br/>apply_decision]
    D --> R[DailyAccountRunner<br/>settle / run]
    R --> S
    S --> L[ledger<br/>AccountLedger / ExecutionSimulator]
    S --> A[(artifacts/ledger<br/>hash 鏈帳本)]
```

- 上游（依 import）：`core`、`ledger`、`decision`（`DecisionInputValidator`、`DecisionRepository`、`DecisionResultValidator`）、`data`（`daily.py`）。
- 下游：`dashboard`（讀帳本）、`competition.dplan`（`verify_account_binding` 的帳本綁定）、`automation.preflight`、`cli/virtual_account.py`、`scripts/run_daily_pipeline.py`、`scripts/repair_pending_tpex_close.py`。
- 讀：`var/etf_agent.db` 的官方價量、決策封存。寫：帳本資料夾（預設 `artifacts/ledger/<account_id>/`，由 CLI 指定）。

## 對應的 CLI／scripts／測試

- CLI：`cli/virtual_account.py`（`init`、`ensure-init`、`prepare-day`、`attach-account`、`apply-decision`、`status`、`verify`，另有每日／結算類子命令）。
- scripts：`scripts/run_daily_pipeline.py`、`scripts/repair_pending_tpex_close.py`。
- 測試：`tests/account/test_virtual_account.py`、`tests/account/test_virtual_account_daily.py`。

## 規則與邊界

- 帳本是雜湊鏈，不分日期資料夾；已有帳戶不會重新注資，只驗證並沿用。
- 結算用官方 TWSE／TPEx 價量；缺官方價時 `run` 停止並顯示缺口，不改用 Yahoo 價格（見根 README「常見問題」）。
- 新決策取代同一份帳戶快照的舊決策時，舊 run 會被移到 `artifacts/archive/superseded_decisions`（`supersede_decision_runs`）；舊決策的目標時段已過交件期限（08:55，可能已上傳）或沒有目標時段時不取代，改把新決策移到封存區並讓該次流程失敗。
- 待結算決策的目標時段事後被日曆確認為休市（例如官方日期表沒列、之後才登錄的颱風假）且資料庫沒有該日任何行情時，`settle` 回傳 `session_closed`，`run` 把該決策移到決策庫旁的 `archive/voided_closed_sessions/`，並寫一份 `<run>.void.json` 作廢紀錄；現金與持股不動，不順延到下一個交易日成交（與主辦方作廢前一日委託一致），接著照常建立新 prepare。沒有日曆時維持等待。
- 有待成交決策時預設不建立新的 prepare；只有新 Snapshot 的 cutoff 早於該決策目標時段當天的交件期限（08:55）時，`DailyAccountRunner.run` 才把該決策移到同一封存區並建立新 prepare（`supersede_unstarted_decision`）。缺少目標時段、已過期限或收盤價晚到時維持 `blocked_by_pending_decision`。
- 取代後該次每日流程沒產生新 D-Plan 時，`restore_superseded_decision` 把被取代的決策搬回決策庫、帳本 latest 指回原 prepare（`VirtualAccountRepository.rollback_latest`），重跑建立的 prepare 移到 `ledger/<account_id>/rolled_back/`，全部只搬移不刪除；帳本已被其他執行推進或原決策不在封存區時拒絕。
- 決策跑完時已過目標時段的交件期限（08:55）：每日流程不匯出 D-Plan；有被取代的備案時照上面還原，沒有時以 `archive_decision_run` 把新決策移到封存區，帳本結算時視為該時段沒交件。
- 只模擬成交，不自動下單。
