# 資料來源清單

更新：2026-09-30。

只列和以下項目直接相關的來源：150 檔研究、公司基本面、個股新聞，以及正式競賽的帳本與風控。

**資料已抓到，不代表已進入正式研究。** 各來源的用途與限制，以實際程式路徑為準。

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
| [交易限制與日曆](#交易限制與日曆) | 政府開放 CSV × 7、TWSE 開休市表 | `scripts/run_daily_pipeline.py` | 可，風控必要輸入 |
| [交易池](#交易池) | 主辦方名單 | `data/universe.py` | 可，一次性版本化輸入 |

## 150 檔研究日線

- **來源**：Yahoo Finance，含 OHLCV、原始收盤與調整收盤。
- **入口**：`scripts/collect_history.py` → `data/yfinance_history.py`。`./start.sh data` 每日抓取，並檢查同日 150 檔都有資料。

### 增量重抓

每次重抓最近 7 天。符合下列任一情形，視為 Yahoo 的基準改變，整檔重抓：

1. **現金股利**：已存序列的還原係數（Adj Close ÷ Close）出現「較早日期大於較晚日期」。
2. **配股、分割、減資**：重抓前後重疊區間，同日 Close 的相對差超過 0.01%。
   - 這類事件發生時，Yahoo 會把事件前的 Close 與 Adj Close 同比例改寫，所以 Adj÷Close 不變，只有比對同日 Close 才看得出來。

**已知限制**：Yahoo 的 Close 在這類事件前，不是交易所原價。若停抓超過 7 天再補抓，事件前的舊列看不出來，須人工整檔重抓。

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
- **覆蓋**：2026 Q2 為 298／300。`3718.TWO` 的兩張表期間未驗證。

## 公司基本資料

- **來源**：TWSE／TPEx `t187ap03`，含公司與產業分類。
- **入口**：`scripts/collect_sector_classification.py`，每日保存候選。
- **使用**：正式決策仍讀已驗證的 `data/sector_classification.json`。

## 多季基本面候選

- **來源**：FinMind `TaiwanStockFinancialStatements`、`TaiwanStockBalanceSheet`、`TaiwanStockCashFlowsStatement`。
- **入口**：`scripts/collect_supplemental_sources.py finmind`。`./start.sh data` 每日逐股更新。
- **實抓（2026-09-30）**：三張表各 149／150。`3718.TWO` 為空結果。
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

- FinMind 新聞包含多家媒體。FinMind 的服務使用權，不等於原媒體內容的再利用授權。
- 新聞 `date` 沒有時區。
- 在逐篇的授權與發布時間證據核准前，正式研究維持 `unavailable`。

**首次實抓（2026-09-29）**：對 150 檔各查一次，116 檔有線索、34 檔空結果，共保存 1,004 筆。這只是候選覆蓋，不代表新聞內容完整，也不能用於正式研究。

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

金融判斷仍須區分：官方事實、新聞線索、情緒、交易決策。

## 來源依據

- [FinMind 基本面](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)
- [FinMind 個股新聞欄位與單日限制](https://finmind.github.io/en/tutor/TaiwanMarket/Others/)
- [FinMind 資料授權說明](https://finmind.github.io/en/Disclaimer/)
- [主辦方 D-Plan 指南](../game_docs/D-Plan_撰寫指南_v4.3.md)
