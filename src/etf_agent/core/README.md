# core｜共用基礎工具

提供所有 Agent 共用、不依賴任何外部 Provider 的基礎函式：Canonical JSON 與內容雜湊、含時區的時間解析、有限 Decimal 解析、不可變 run 封存，以及依台北日期分資料夾的 run 佈局。Validator 在不同 Agent 之間重算雜湊與 cutoff 時都用同一份實作（見 [AGENTS.md](../../../AGENTS.md)「程式與 Skill 慣例」）。

## 檔案清單

| 檔案 | 職責 | 主要公開項目 |
| --- | --- | --- |
| `__init__.py` | 匯出最常用的雜湊、時間、數值函式 | 見下方「對外介面」 |
| `canonical.py` | Canonical JSON 編碼與內容雜湊 | `canonical_json`、`canonical_sha256`、`content_sha256` |
| `numeric.py` | 有限 Decimal 解析與穩定文字輸出 | `parse_decimal`、`decimal_string` |
| `timeutil.py` | 含時區時間解析；無時區的時間一律拒絕 | `parse_aware_time` |
| `artifact_store.py` | 不可變、雜湊封存的 run 目錄（manifest 驗證） | `ImmutableRunStore`（`build_manifest`、`save`、`verify`、`run_dir`） |
| `run_layout.py` | 日期分資料夾佈局 `<root>/<YYYY-MM-DD>/<run_id>`（Asia/Taipei） | `taipei_day`、`day_from_run_id`、`day_dirs`、`list_run_dirs`、`find_run_dir` |

## 對外介面

`__init__.py` 的 `__all__`：`canonical_json`、`canonical_sha256`、`content_sha256`、`decimal_string`、`parse_aware_time`、`parse_decimal`。

`ImmutableRunStore` 與 `run_layout` 的函式需從子模組匯入，例如 `from etf_agent.core.artifact_store import ImmutableRunStore`、`from etf_agent.core.run_layout import find_run_dir`。

## 上下游

- 不依賴本專案其他子套件（以 import 檢查為準），是依賴圖的最底層。
- 被 `automation`、`competition`、`data`、`decision`、`decision_delivery`、`fundamentals`、`ledger`、`perception`、`reporting`、`research`、`virtual_account` 引用。
- `ImmutableRunStore` 的繼承者：`decision.finalization`、`decision_delivery.repository`、`perception.news_service`、`perception.market_news.service`、`data.market_probe.service`。

## 對應的 CLI／scripts／測試

- CLI：沒有專屬入口；`cli/fundamental_repair.py`、`cli/market_news.py` 與 `scripts/collect_sector_classification.py`、`scripts/run_daily_pipeline.py` 會直接引用。
- 測試：`tests/core/test_core.py`；`tests/automation/test_run_layout.py` 另涵蓋 run 佈局。

## 規則與邊界

- 無時區的時間字串必須 fail closed（`parse_aware_time`）。
- Decimal 必須是有限值；NaN／Infinity 一律拒絕。
- 模組專屬錯誤型別以 `error=` 參數傳入，不在各模組另寫一份雜湊、時間或數值解析。
- 新的不可變 run 保存優先繼承 `ImmutableRunStore`。
- 同一個 run ID 出現在兩個日期資料夾會被拒絕（見 [執行時資料夾](../../../docs/runtime-artifacts.md)）。
