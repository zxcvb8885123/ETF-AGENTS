# data/：靜態輸入檔

放人工維護或由腳本定期更新的靜態輸入；容器內唯讀掛載（`./data:/app/data:ro`）。動態行情與抓取結果在 `var/`、`artifacts/`。

## 檔案清單

| 檔案 | 內容 | 規則 | 主要讀取者 |
| --- | --- | --- | --- |
| `official_universe.csv` | 競賽官方 150 檔交易池（150 筆資料列） | 交易池唯一來源；筆數等於 `config/competition_rules.json` 的 `universe_size`；為空時各收集器拒絕執行 | `src/etf_agent/data/universe.py`、`scripts/check_setup.py`、`cli/fundamental_repair.py`、各 `scripts/collect_*` |
| `active_etf_list.csv` | 主辦方公布的主動型 ETF 名單（30 筆資料列） | 只檢查是否載入 | `scripts/check_setup.py` |
| `active_etf_holdings.csv` | 30 檔主動型 ETF 各自前 15 大持股（Active Share 基準，450 筆資料列） | MoneyDJ 2026-09-30 月底持股，2026-10-25 一次性抓取、不每日更新；來源核准見 `config/active_share_sources.json` | `src/etf_agent/decision/active_share.py`（每日決策輸入）、`scripts/check_setup.py` |
| `sector_classification.json` | 150 檔產業代碼分類（含來源、抓取時間與雜湊） | `status=completed`、`missing_symbols` 為空；由 `scripts/collect_sector_classification.py` 產生 | `cli/portfolio_decision.py build-policy`（`--sector`）、`scripts/run_daily_pipeline.py`、`preflight` |

這些檔案是輸入契約，改動後確認 `tests/` 內相關測試。

## 欄位

### `official_universe.csv`

| 欄位 | 說明 |
| --- | --- |
| `symbol` | 股票代號（檔內為純數字，如 `2330`；系統內部依市場附後綴：上市 `.TW`、上櫃 `.TWO`） |
| `name` | 公司名稱 |
| `market` | `TWSE` 或 `TPEX` |
| `source_date` | 清單來源日期（現為 `2026-09-14`，對應 `game_docs/` 的 150 檔清單 PDF） |

### `active_etf_list.csv`

| 欄位 | 說明 |
| --- | --- |
| `etf_code` | ETF 代號 |
| `name` | ETF 名稱 |
| `source_date` | 來源日期 |
| `source` | 來源檔（指向 `game_docs/玉山挑戰賽_主動型ETF列表_v1.pdf`） |

### `active_etf_holdings.csv`

表頭為 `etf_code,rank,stock_name,symbol,weight,source_date,available_at,source`，用途是競賽 Active Share 比較基準（比賽辦法（十三）：投資組合前 15 大 vs. 各檔主動型 ETF 前 15 大，不得小於 15%，連續 2 個交易日低於 15% 主辦方有權取消資格）。

| 欄位 | 說明 |
| --- | --- |
| `etf_code` | 主動型 ETF 代號，須涵蓋 `config/decision_rules.json` 的 `required_benchmark_ids` |
| `rank` | 該 ETF 內依權重由大到小的排名，自 1 起連續，最多 15 |
| `stock_name` | 來源上的股票名稱（已去除結尾 `*`） |
| `symbol` | 對照 `official_universe.csv` 的代號，格式與決策鏈一致（上市 `.TW`、上櫃 `.TWO`）；交易池外持股（含海外股）留空，計算時以 `OTHER:` 加名稱為鍵、必然不與投資組合重疊。代號不在交易池時 `DecisionInputValidator` 拒絕 |
| `weight` | 占該 ETF 淨值比例（小數） |
| `source_date` | 持股資料日 |
| `available_at` | 實際抓取時間；決策截止早於此時間時拒絕使用 |
| `source` | 來源代碼，須在 `config/active_share_sources.json` 核准 |

稽核與限制見 `docs/source_audit/2026-10-25_active_etf_holdings.md`。

### `sector_classification.json`

| 欄位 | 說明 |
| --- | --- |
| `schema_version`、`version`、`status` | 版本與完成狀態（`completed`） |
| `available_at` | 可用時間；晚於決策 `decision_cutoff` 時 `build_decision_policy` 拒絕 |
| `sources[]` | TWSE／TPEx 公司基本資料來源：`source_id`、`url`、`fetched_at`、`content_date`、`raw_sha256`、`row_count` |
| `sector_by_symbol` | 代號到產業代碼（形如 `industry:01`）的對照 |
| `missing_symbols` | 未覆蓋的交易池股票；非空時政策建立會失敗 |

### 產業分類的更新

| 項目 | 說明 |
| --- | --- |
| 腳本預設輸出 | `scripts/collect_sector_classification.py` 的 `--output` 預設為本檔 |
| `start.sh` 每日流程 | 以 `--output` 寫到 `artifacts/data/sector_classification_latest.json`，不覆蓋本檔 |
| 採用新版 | 人工核對候選檔後取代本檔 |
