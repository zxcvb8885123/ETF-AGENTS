# ledger｜成交模擬與帳務

以整張為單位模擬成交（含手續費與賣出證交稅），並以交易日現金帳處理賣出款交割與公司行動。純計算，不做 I/O。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出公開介面 | `AccountLedger`、`ExecutionSimulator`、`LedgerError` |
| `contracts.py` | 整張成交與帳本共用的小工具與錯誤型別 | `LedgerError`、`money_string`、`decimal_value`、`parse_time` |
| `execution.py` | 整張成交模擬，計算手續費與賣出稅 | `ExecutionSimulator`（`run`） |
| `account.py` | 以交易日記帳的現金帳，賣出款與公司行動分別交割 | `AccountLedger`（`buying_power`、`apply_fills`、`settle`、`apply_actions`、`snapshot`） |

## 對外介面

`AccountLedger`、`ExecutionSimulator`、`LedgerError`。

## 上下游

- 上游：只依賴 `etf_agent.core`。
- 下游：`virtual_account.service` 在 `apply_decision`、狀態重建時使用（以 import 檢查，唯一使用者）。
- 本套件不讀寫檔案；持久化由 `virtual_account` 負責。

## 對應的 CLI／scripts／測試

- CLI：透過 `cli/virtual_account.py` 間接使用。
- 測試：`tests/account/test_virtual_account_daily.py` 引用；帳戶行為另見 `tests/account/test_virtual_account.py`。

## 規則與邊界

- 數值使用有限 Decimal（`etf_agent.core`），金額以字串輸出，不用浮點數。
- 時間必須含時區，不合法時拋出 `LedgerError`。
- 帳本只記錄既有決策的模擬結果，不產生決策、不下單。
