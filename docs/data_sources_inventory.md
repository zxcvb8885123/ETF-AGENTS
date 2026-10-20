# 資料來源清單

更新：2026-09-30。只列目前與 150 檔研究、公司基本面、個股新聞，以及正式競賽帳本和風控直接相關的來源。資料已抓到不代表已進入正式研究；各欄的用途與限制以實際程式路徑為準。

| 用途 | 來源與內容 | 擷取與狀態 | 使用界線 |
| --- | --- | --- | --- |
| 150 檔研究日線 | Yahoo Finance：OHLCV、原始收盤與調整收盤 | `scripts/collect_history.py` → `data/yfinance_history.py`；`./start.sh data` 每日抓取並檢查同日 150 檔；增量重抓最近 7 天，有兩種情形視為 Yahoo 基準改變並整檔重抓：已存序列的還原係數（Adj Close ÷ Close）出現較早日期大於較晚日期（現金股利）；或重抓前後重疊區間的同日 Close 相對差超過 0.01%（配股、分割、減資時 Yahoo 會把事件前的 Close 與 Adj Close 同比例改寫，Adj÷Close 不變，只有比對同日 Close 才看得出）。已知限制：Yahoo 的 Close 在這類事件前不是交易所原價，若停抓超過 7 天再補抓，事件前的舊列看不出來，須人工整檔重抓 | 用於技術研究；Snapshot 與歷史 K 線同日優先 Yahoo，該股該日缺 Yahoo 時改用同日官方價：最新一日原價即還原價；歷史缺口以前後 Yahoo 的還原係數換算，係數為 Yahoo adj ÷ 同日官方原價（不用 Yahoo Close，因其可能被事後改寫），同日沒有官方原價的 Yahoo 列不作參考，缺口因而無法確定係數時排除；前後係數不同時，以 TWSE 個股月行情漲跌欄的 `X`（`daily_prices.ex_rights_marker`）定位除權息日，除權息日不晚於缺口用後端係數、否則用前端係數；標記缺漏（NULL）或區間內多個除權息日時排除該日，不以原價混入（`source` 保留來源）；不供正式帳本成交均價；Snapshot `latest_prices` 另有 `execution_close_price`／`execution_price_source`／`execution_source_evidence_id`：同日官方原價（Yahoo 以外、依來源優先序），是配置、風控、D-Plan 與虛擬帳戶的決策用價；`analysis_close_price` 是調整價，只供研究，重放過去日期時含該日之後的除權息調整。同日沒有官方價時 `execution_*` 為空，決策退回 `analysis_close_price`（舊封存沒有此欄位，亦同） |
| 月營收、重大訊息 | TWSE／TPEx MOPS 開放資料 `t187ap05`、`t187ap04` | `cli/data_agent.py collect` → `data/corporate.py`；每日擷取 | 依公告與實際取得時間保存，供事件與基本面事實 |
| 官方財報 | MOPS `t187ap06`／`t187ap07` 各業別損益表、資產負債表 | `scripts/collect_financial_statements.py --latest-due` → `data/financial.py`；每日擷取 | 正式基本面仍使用已驗證官方資料；2026 Q2 覆蓋 298／300，`3718.TWO` 兩表期間未驗證 |
| 公司基本資料 | TWSE／TPEx `t187ap03`：公司與產業分類 | `scripts/collect_sector_classification.py`；每日保存候選 | 正式決策仍讀已驗證的 `data/sector_classification.json` |
| 多季基本面候選 | FinMind `TaiwanStockFinancialStatements`、`TaiwanStockBalanceSheet`、`TaiwanStockCashFlowsStatement` | `scripts/collect_supplemental_sources.py finmind`；`./start.sh data` 每日逐股更新 | 2026-09-30 實抓三表各 149／150，`3718.TWO` 空結果；`date` 是財報期間，不是發布時間；尚未接 Snapshot 或歷史回測 |
| 150 檔個股新聞線索 | FinMind `TaiwanStockNews`：股票代號、標題、原媒體、連結、來源時間字串 | `scripts/collect_supplemental_sources.py finmind-news`；平日 22:00 與財報錯開 | 只存 `license_status=unverified` 的候選；來源時間無時區，`published_at` 留空、`available_at` 為抓取時間；不得進正式情緒、事件判斷或回測 |
| 正式帳本價量 | TWSE `STOCK_DAY_ALL`、TPEx `tpex_mainboard_quotes`；上市全市場端點延遲時補 TWSE `exchangeReport/STOCK_DAY`；歷史月行情上市用 TWSE `STOCK_DAY`、上櫃用 TPEx `afterTrading/tradingStock`（`config/data_sources.json` 的 `tpex_url`，來源 `TPEX_TRADING_STOCK`，成交張數、仟元換算為股、元） | `scripts/collect_latest_prices.py`、`scripts/collect_official_history.py`；僅 `./start.sh daily`。個股月行情逐檔逐月請求，所有執行緒共用節流：每次請求至少間隔 `min_request_interval_seconds`（預設 2 秒）；遇 HTTP 308／403／429／503 視為限流，全體依 `throttle_backoff_seconds`（30、120、300 秒）退避，用盡仍被限流即熔斷並停止後續請求，缺口留待下次補抓。TPEx 漲跌欄沒有 X 標記，`ex_rights_marker` 以參考價（收盤 − 漲跌）不等於前一交易日收盤推導；月內第一列沒有前一日可比，標記保持 NULL（未知），不猜測 | 主辦方要求交易所前日收盤價推導委託股數、當日成交金額 ÷ 股數結算；缺官方價量時等待，不以 Yahoo 代替 |
| 交易限制與日曆 | 已核准的政府開放 CSV × 7、TWSE 開休市日期表 | `scripts/run_daily_pipeline.py` 的來源保存與驗證 | 正式風控和 T+2 交割的必要輸入；缺漏或過期時 fail-closed |
| 交易池 | 主辦方名單整理成 `data/official_universe.csv` | `data/universe.py` 驗證 150 檔 | 一次性版本化輸入，不是每日網路擷取 |

`./start.sh data` 只抓研究日線與公司資料，不執行決策或帳本；FinMind Token 從執行環境或本機 `.env` 載入。`.env` 權限為 600 且被 Git 忽略。個股新聞排程與財報排程分開，避免兩者同一小時合計約 600 次請求耗盡免費額度。直接執行 CLI 時須自行提供 `FINMIND_TOKEN` 環境變數。

台糖 RSS 與 150 檔新聞需求無關，已從每日擷取移除；既有原始回應與候選資料保留供封存稽核。FinMind 新聞來源含多家媒體，FinMind 的服務使用權不等於原媒體內容再利用授權，且新聞 `date` 沒有時區；在逐篇授權與發布時間證據核准前，正式研究維持 `unavailable`。金融判斷仍須區分官方事實、新聞線索、情緒與交易決策。

首次實抓 2026-09-29：FinMind 個股新聞對 150 檔各查一次，116 檔有線索、34 檔空結果，保存 1,004 筆；這是候選覆蓋，不代表新聞內容完整或可用於正式研究。

來源依據：[FinMind 基本面](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)、[FinMind 個股新聞欄位與單日限制](https://finmind.github.io/en/tutor/TaiwanMarket/Others/)、[FinMind 資料授權說明](https://finmind.github.io/en/Disclaimer/)、[主辦方 D-Plan 指南](../game_docs/D-Plan_撰寫指南_v4.3.md)。
