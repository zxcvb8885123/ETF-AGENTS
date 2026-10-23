# 資料來源清單

## 用途與範圍

列出與 150 檔研究、公司基本面、個股新聞，以及正式競賽帳本與風控直接相關的來源。

- 資料已抓到，不代表已進入正式研究。
- 用途與限制以實際程式路徑為準。

## 一覽

| 用途 | 來源 | 擷取入口 | 能否進正式研究 |
| --- | --- | --- | --- |
| [150 檔研究日線](#150-檔研究日線) | Yahoo Finance | `scripts/collect_history.py` | 技術研究用；不供帳本成交價 |
| [月營收、重大訊息](#月營收重大訊息) | TWSE／TPEx MOPS 開放資料 | `cli/data_agent.py collect` | 可，作事件與基本面事實 |
| [官方財報](#官方財報) | MOPS 損益表、資產負債表 | `scripts/collect_financial_statements.py` | 可 |
| [公司基本資料](#公司基本資料) | TWSE／TPEx `t187ap03` | `scripts/collect_sector_classification.py` | 候選；正式讀已驗證的檔案 |
| [多季基本面候選](#多季基本面候選) | FinMind 三張財報 | `scripts/collect_supplemental_sources.py finmind` | 否，尚未接 Snapshot |
| [個股新聞線索](#個股新聞線索) | FinMind `TaiwanStockNews` | `scripts/collect_supplemental_sources.py finmind-news` | **否**，授權未驗證 |
| [正式帳本價量](#正式帳本價量) | TWSE／TPEx 官方價量 | `scripts/collect_latest_prices.py` 等 | 可，帳本結算依據 |
| [基準標的](#基準標的) | TWSE 官方個股日成交資訊（0050） | `cli/benchmark.py collect` | 只供績效比較，不進交易池、不進決策 |
| [交易限制與日曆](#交易限制與日曆) | 政府開放 CSV × 7、TWSE 開休市表 | `scripts/run_daily_pipeline.py` | 可，風控必要輸入 |
| [交易池](#交易池) | 主辦方名單 | `data/universe.py` | 可，一次性版本化輸入 |
| [台股恐懼貪婪指標](market_fear_greed.md) | 期交所 VIX 月檔、期交所 Put/Call 開放資料、FinMind `TaiwanStockTotalMarginPurchaseShortSale` | `cli/market_fear_greed.py capture` | 可，作全市場情緒；核准見[來源核對](source_audit/2026-10-23_market_fear_greed_sources.md)（VIX 為使用者決定） |

## 150 檔研究日線

- **來源**：Yahoo Finance，含 OHLCV、原始收盤與調整收盤。
- **入口**：`scripts/collect_history.py` → `data/yfinance_history.py`。`./start.sh data` 每日抓取，並檢查同日 150 檔都有資料。

### 增量重抓

每次重抓最近 7 天。符合下列任一情形，視為 Yahoo 的基準改變，整檔重抓：

1. **現金股利**：已存序列的還原係數（Adj Close ÷ Close）出現「較早日期大於較晚日期」。
2. **配股、分割、減資**：重抓前後重疊區間，同日 Close 的相對差超過 0.01%。
   - 這類事件發生時，Yahoo 會把事件前的 Close 與 Adj Close 同比例改寫，所以 Adj÷Close 不變，只有比對同日 Close 才看得出來。

3. **停抓超過 7 天**：重疊區間已看不到停抓期間被改寫的舊列，該檔自動整段重抓（2026-10-23 起）。

**已知限制**：Yahoo 的 Close 在這類事件前，不是交易所原價；若 Yahoo 延遲超過 7 天才改寫，每日重疊比對仍可能看不到。決策與帳本用價一律是官方原價，不受影響。

### 定案與版本（2026-10-23 起）

- **定案時間**：Yahoo 盤中就會回傳當日那根日線，收盤價與成交量尚未定案。`data/price_finality.py` 統一定義台北 15:00（`PRICE_FINALIZED_TIME`）為非官方日線的定案時間，13:30（`MARKET_CLOSE_TIME`）為收盤。
  - 收集端：`YFinanceHistoryCollector` 抓取時間早於該日 15:00 的日線不寫入資料庫，只記警告；`scripts/collect_history.py` 預設 `--end` 是最近已定案日期（15:00 前為前一天）。
  - 讀取端：`SnapshotRepository` 與事件研究的價格特徵只採用 `fetched_at ≥ 該 trade_date 15:00` 的 Yahoo 列；該日沒有定案版本時改用同日官方價，官方價也沒有就不納入該日。官方來源只在收盤後公布，不受此限制。
- **版本**：`daily_prices` 是 append-only 版本表（主鍵 `symbol, trade_date, source, fetched_at`）。重抓時內容與最新版本相同就不新增列（原始回應仍照存）；內容改變才新增版本，且 `fetched_at` 必須遞增、含時區、不得是未來時間。唯一例外：Yahoo 最新版本在定案前抓、本次已在定案後抓到相同內容，仍新增一版記錄定案。
  - 所有查詢取「`fetched_at ≤ cutoff` 的最新版本」，事後用舊 cutoff 重建 Snapshot 或歷史 K 線會拿到當時的版本，不再被後來的重抓覆蓋。
  - 既有資料庫由 `cli/data_agent.py migrate --database var/etf_agent.db` 遷移（任何 `initialize()` 也會自動執行一次，可重複執行）；遷移前的每列成為第一個版本，遷移前已被覆寫掉的歷史版本無法還原。

### 使用方式

- 用於技術研究。
- Snapshot 與歷史 K 線，同日優先用 Yahoo。該股該日缺 Yahoo 時，改用同日官方價。
  - 最新一日：原價即還原價。
  - 歷史缺口：以前後 Yahoo 的還原係數換算。
    - 係數 = Yahoo adj ÷ 同日官方原價。不用 Yahoo Close，因為它可能被事後改寫。
    - 同日沒有官方原價的 Yahoo 列，不作參考。缺口因此無法確定係數時，排除該日。
    - 前後係數不同時，用 TWSE 個股月行情漲跌欄的 `X`（`daily_prices.ex_rights_marker`）定位除權息日。
      - 除權息日不晚於缺口：用後端係數。
      - 除權息日晚於缺口：用前端係數。
      - 標記缺漏（NULL），或區間內有多個除權息日：排除該日，不以原價混入。
  - `source` 欄位保留來源。
- **不供正式帳本的成交均價。**

### Snapshot 的價格欄位

| 欄位 | 用途 |
| --- | --- |
| `execution_close_price`、`execution_price_source`、`execution_source_evidence_id` | 同日官方原價（Yahoo 以外，依來源優先序）。配置、風控、D-Plan 與虛擬帳戶的決策用價 |
| `analysis_close_price` | 調整價，只供研究。重放過去日期時，含該日之後的除權息調整 |

同日沒有官方價時，`execution_*` 為空，決策退回 `analysis_close_price`。舊封存沒有這些欄位，也是同樣處理。

## 月營收、重大訊息

- **來源**：TWSE／TPEx MOPS 開放資料 `t187ap05`（月營收）、`t187ap04`（重大訊息）。
- **入口**：`cli/data_agent.py collect` → `data/corporate.py`，每日擷取。
- **使用**：依公告時間與實際取得時間保存，供事件與基本面事實使用。

## 官方財報

- **來源**：MOPS `t187ap06`／`t187ap07`，各業別的損益表與資產負債表。
- **入口**：`scripts/collect_financial_statements.py --latest-due` → `data/financial.py`，每日擷取。
- **使用**：正式基本面使用已驗證的官方資料。
- **實測覆蓋**：2026 Q2 為 298／300；`3718.TWO` 兩張表期間未驗證。

## 公司基本資料

- **來源**：TWSE／TPEx `t187ap03`，含公司與產業分類。
- **入口**：`scripts/collect_sector_classification.py`，每日保存候選。
- **使用**：正式決策仍讀已驗證的 `data/sector_classification.json`。

## 多季基本面候選

- **來源**：FinMind `TaiwanStockFinancialStatements`、`TaiwanStockBalanceSheet`、`TaiwanStockCashFlowsStatement`。
- **入口**：`scripts/collect_supplemental_sources.py finmind`。`./start.sh data` 每日逐股更新。
- **實測覆蓋**：三張表各 149／150；`3718.TWO` 為空結果。
- **限制**：
  - `date` 是財報期間，不是發布時間。
  - 尚未接入 Snapshot 或歷史回測。

## 個股新聞線索

- **來源**：FinMind `TaiwanStockNews`，含股票代號、標題、原媒體、連結、來源時間字串。
- **入口**：`scripts/collect_supplemental_sources.py finmind-news`。平日 22:00 執行，與財報排程錯開。
- **保存方式**：只存 `license_status=unverified` 的候選。
  - 來源時間沒有時區，所以 `published_at` 留空。
  - `available_at` 為抓取時間。
- **使用界線**：不得進入正式情緒、事件判斷或回測。

**為什麼不能用**

| 原因 | 說明 |
| --- | --- |
| 授權 | 新聞含多家媒體；FinMind 服務使用權不等於原媒體再利用授權 |
| 時間 | 新聞 `date` 沒有時區 |
| 狀態 | 逐篇授權與發布時間證據核准前，正式研究維持 `unavailable` |

**實測覆蓋**：

| 項目 | 數量 |
| --- | --- |
| 查詢股票 | 150 檔各一次 |
| 有線索 / 空結果 | 116 / 34 檔 |
| 保存筆數 | 1,004 |

僅為候選覆蓋，不代表新聞完整，也不能用於正式研究。

## 正式帳本價量

### 來源

| 範圍 | 來源 |
| --- | --- |
| 當日全市場（上市） | TWSE `STOCK_DAY_ALL`。全市場端點延遲時，補 `exchangeReport/STOCK_DAY` |
| 當日全市場（上櫃） | TPEx `tpex_mainboard_quotes` |
| 歷史月行情（上市） | TWSE `STOCK_DAY` |
| 歷史月行情（上櫃） | TPEx `afterTrading/tradingStock`（`config/data_sources.json` 的 `tpex_url`，來源代碼 `TPEX_TRADING_STOCK`）。成交張數、仟元換算為股、元 |
| 待結算的歷史上櫃缺口 | TPEx `afterTrading/dailyQuotes`，指定日期，原始股數／元 |

### 入口

- `scripts/collect_latest_prices.py`、`scripts/collect_official_history.py`：僅 `./start.sh daily` 執行。
- `scripts/repair_pending_tpex_close.py`：歷史上櫃的帳本缺口。

### 節流與熔斷

個股月行情逐檔、逐月請求，所有執行緒共用同一個節流器：

- 每次請求至少間隔 `min_request_interval_seconds`（預設 2 秒）。
- 遇到 HTTP 308／403／429／503，視為限流。全體依 `throttle_backoff_seconds`（30、120、300 秒）退避。
- 退避用盡仍被限流，即熔斷並停止後續請求。缺口留待下次補抓。

### 除權息標記

- TWSE：漲跌欄的 `X` 標記。
- TPEx：漲跌欄**沒有** X 標記。`ex_rights_marker` 以參考價（收盤 − 漲跌）不等於前一交易日收盤來推導。
- 月內第一列沒有前一日可比，標記保持 `NULL`（未知），不猜測。

### 使用界線

- 主辦方要求：以交易所前日收盤價推導委託股數，以當日成交金額 ÷ 股數結算。
- 歷史補抓保留原始回應、實際抓取時間與來源。
- 缺官方價量時等待，**不以 Yahoo 代替**。

## 基準標的

- **用途**：讓虛擬帳戶績效與「0050 買入持有」、「150 檔交易池等權買入持有」比較（儀表板「與基準比較」、`cli/benchmark.py compare`）。口徑見 [benchmark/README.md](../src/etf_agent/benchmark/README.md)：同本金、開帳後第一個可成交交易日以官方成交均價整張買進、同一套費稅（`config/decision_rules.json`）、官方未還原收盤價估值、不計股利。
- **來源**：`config/benchmarks.json` 列出基準與 `license_status`。0050 用與正式帳本價量相同的證交所個股日成交資訊端點（`TWSE_STOCK_DAY`，`config/data_sources.json` 的 `historical_daily.twse_url`），授權沿用「正式帳本價量」的核准；150 檔等權直接用交易池既有官方價量。2026-10-23 前資料庫只有 0050 的一筆 `TWSE_STOCK_DAY_ALL` 列（早期全市場抓取殘留），須以 `cli/benchmark.py collect` 補抓。
- **不用 Yahoo**：授權只供研究，且沒有成交金額，算不出與帳本同口徑的成交均價；設定成 `YAHOO_FINANCE` 來源或非 `approved` 的基準一律顯示 unavailable。
- **不污染交易池**：收集時以 `HistoricalPriceCollector(in_competition_universe=False)` 保存（`YFinanceHistoryCollector` 有同樣開關），`instruments.in_competition_universe` 維持 0；`SnapshotRepository`、交易池驗證、`analysis_daily_prices` 與 `prices_current` 都只看交易池成員，不會因此多一檔。帳本結算的 `OfficialCloseMarket` 只要求所需標的存在，多出的基準列不影響。
- **時點**：比較只採用 `fetched_at ≤ as_of` 且收盤 ≤ as_of 的官方日線版本；`collect` 補抓的歷史列以實際抓取時間為可得時間，不回填到更早的 as-of。
- **限制**：等權基準每檔預算為本金 ÷ 150，股價高於預算（整張買不起）的股票留現金並列在 `unaffordable_symbols`；基準不計現金股利與配股（帳本亦只在提供公司行動時入帳）。

## 交易限制與日曆

- **來源**：已核准的政府開放 CSV × 7、TWSE 開休市日期表。
- **入口**：`scripts/run_daily_pipeline.py` 的來源保存與驗證。
- **使用**：正式風控與 T+2 交割的必要輸入。缺漏或過期時 fail-closed。

## 交易池

- **來源**：主辦方名單，整理成 `data/official_universe.csv`。
- **入口**：`data/universe.py` 驗證 150 檔。
- **性質**：一次性的版本化輸入，不是每日網路擷取。

## 執行與憑證

- `./start.sh data` 只抓研究日線與公司資料，不執行決策或帳本。
- FinMind Token 從執行環境或本機 `.env` 載入。`.env` 權限為 600，且被 Git 忽略。
- 直接執行 CLI 時，須自行提供 `FINMIND_TOKEN` 環境變數。
- 個股新聞排程與財報排程分開，避免兩者同一小時合計約 600 次請求，耗盡免費額度。

## 已移除的來源

台糖 RSS 與 150 檔新聞需求無關，已從每日擷取移除。既有的原始回應與候選資料保留，供封存稽核。

## 原則

金融判斷須區分：官方事實、新聞線索、情緒、交易決策。

## 尚未實作

- 多季基本面候選（FinMind 三張財報）尚未接入 Snapshot 或歷史回測。
- 個股新聞線索缺逐篇授權與發布時間證據，不進入正式研究。

## 來源依據

- [FinMind 基本面](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)
- [FinMind 個股新聞欄位與單日限制](https://finmind.github.io/en/tutor/TaiwanMarket/Others/)
- [FinMind 資料授權說明](https://finmind.github.io/en/Disclaimer/)
- [主辦方 D-Plan 指南](../game_docs/D-Plan_撰寫指南_v4.3.md)
