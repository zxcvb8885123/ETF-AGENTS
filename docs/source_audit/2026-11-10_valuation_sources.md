# 估值來源核對（2026-11-10）

範圍：[優質股計畫](../quality_dip_plan.md)第 2.6 節估值 V1，只收集與稽核，不進決策與簡報。使用者於 2026-11-10 同意核准下列兩個資料集用於 V1，並下令開工；登記內容為 `license_status=approved`、範圍 `valuation_v1_collection_and_audit_only`、核准日 2026-11-10、核准人使用者。這是使用者的決定，不是已逐條核對的條文。

| 來源代碼 | 資料集 | 端點 | 內容 |
| --- | --- | --- | --- |
| `TWSE_BWIBBU_ALL` | 上市個股本益比、殖利率及股價淨值比 | `https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL` | `Date`（民國 7 位，如 `1151109`）、`Code`、`Name`、`PEratio`、`PBratio`、`DividendYield`；只有當日 |
| `FINMIND_PER` | `TaiwanStockPER` | `https://api.finmindtrade.com/api/v4/data` | `date`、`stock_id`、`PER`、`PBR`、`dividend_yield`；有歷史 |

## 實測（2026-11-10，唯讀，引自計畫第 2.6 節）

- 證交所回傳 1087 檔；交易池上市 100 檔全部找得到，其中 2 檔 `PEratio` 是空字串。
- FinMind 2308 自 2019-01-02 起 1905 筆；2026-11-09 為 34.83／9.47／1.0，與證交所同日 34.83／9.47／1.00 一致。

## 核對規則（實作於 `src/etf_agent/data/valuation/`）

- 民國日期轉西元並驗證日曆；資料日晚於抓取日、同一回應資料日不一致、欄位名變更、代號重複：整份拒絕、不保存。
- 逐檔、逐欄位（PER、PBR、殖利率）Decimal 兩位小數比對，完全相符才收；PER／PBR 為空、0、負值記 `null` 與原因碼。
- 拒絕原因碼：`OFFICIAL_MISSING_SYMBOL`、`FINMIND_MISSING_DATE`、`FINMIND_UNAVAILABLE`、`FINMIND_BAD_ROW`、`OFFICIAL_BAD_ROW`、`VALUE_MISMATCH`。
- `available_at` 為實際抓取時間，不回填；FinMind 歷史資料日之前的列標 `unverified_history`。

## 尚未確認

- 櫃買中心估值端點（`tpex_mainboard_peratio_analysis`）：是否為正式端點、欄位口徑、授權條款、3718.TWO 缺漏原因；確認前上櫃股估值一律不可用。
- 兩個來源的授權條款未逐條讀取（使用者決定核准）。
- PER 的 EPS 基準（近四季或其他）與殖利率的股利基準。
- 證交所是否有可指定日期的官方查詢，可用來回補核對 FinMind 歷史。
- 連續核對相符的天數門檻（V2 進入條件，由人指定）。
