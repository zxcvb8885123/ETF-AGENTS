# game_docs/：主辦方文件

主辦方（AI CUP 2026／玉山挑戰賽）提供的規格、範例與名單原檔，唯讀參考。

## 檔案清單

| 檔案 | 類型 | 用途 | 對應的專案內容 |
| --- | --- | --- | --- |
| `D-Plan_撰寫指南_v4.3.md` | 指南 | 每日決策文件 D-Plan 的結構與「下層必須引用上層」寫作原則 | `src/etf_agent/competition/`、`cli/dplan.py` 產生 D-Plan v4.3 候選 |
| `D-Plan.schema_v4.3.json` | JSON Schema | D-Plan 欄位格式的權威定義 | 同上；本地結構檢查（`cli/dplan.py validate`）的對照依據 |
| `D-Plan_TEAM_042_2026-10-28.json` | 範例 | 指南所稱的正例 | 參考格式用 |
| `D-Plan_TEAM_043_2026-10-28.json` | 範例 | 指南所稱的反例 | 參考格式用 |
| `DataPack_規格書_v4.3.md` | 規格書 | 每日可下載的 DataPack（最近一次結算後的部位與合規狀態）欄位與單位 | 人工對照參考（程式目前未讀取） |
| `玉山挑戰賽_投資組合及交易標的_150檔清單_v1.pdf` | 名單 | 官方 150 檔交易池 | `data/official_universe.csv`；`config/competition_rules.json` 的 `universe_source` 記錄其檔名與 sha256 |
| `玉山挑戰賽_主動型ETF列表_v1.pdf` | 名單 | 主動型 ETF 清單 | `data/active_etf_list.csv` |
| `玉山挑戰賽_ETF投資策略說明文件_參賽隊伍繳交格式_v1 .docx` | 格式文件 | 隊伍 ETF 投資策略說明的繳交格式（檔名在 `_v1` 後有一個空格） | 人工填寫策略說明時對照（尚未由程式讀取） |
| `AI CUP 2026玉山人工智慧公開挑戰賽_比賽辦法_1005更新.pdf` | 競賽辦法 | 正式規則（2026-10-05 版）：持股檔數、權重、現金、Active Share（前 15 大比前 15 大、低於 15% 連續 2 個交易日可取消資格）、警告與罰則 | `config/competition_rules.json`；Active Share 由 `config/decision_rules.json` 與 `data/active_etf_holdings.csv` 接入 Guard |
| `2026 Agent 基金經理人競賽後台操作說明.pdf` | 操作說明 | 競賽後台的繳交、DataPack 下載與合規狀態頁 | 人工操作參考 |
| `玉山挑戰賽_ETF投資策略說明文件_範例文件_v1.pdf` | 範例 | ETF 投資策略說明文件的主辦方範例 | 人工撰寫策略說明時對照 |
| `玉山挑戰賽_Google Token Credit領取方式_v1.pdf` | 說明 | Google Cloud Token Credit 領取流程 | 與程式無關 |

## 規則

| 規則 | 說明 |
| --- | --- |
| 唯讀 | 不修改原檔 |
| 執行時不讀取 | `src/`、`cli/`、`scripts/` 沒有讀取本資料夾的程式；僅 `data/active_etf_list.csv` 的 `source` 欄位以文字引用其中一份 PDF |
| 機器可讀版本 | 名單與規則已轉成 `data/`、`config/` |
| 檔名 | 含中文與空格，引用時完整複製 |
| 主辦方更新文件時 | 同步檢查 `config/competition_rules.json`（含交易池 sha256）、`data/official_universe.csv` 與 `docs/` |
| 內容細節 | 本 README 不複製文件內容，以原檔為準 |
