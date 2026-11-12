# 資料來源清單

這份文件回答三件事：每種資料從哪裡來、每天由誰抓、能不能進正式研究與決策。

兩個原則：

- 資料抓到了，不代表可以用。要看「授權核准」與「來源是否通過驗證」。
- 每筆資料都記錄來源、實際抓取時間與原始回應，事後可以重算、稽核。

## 一覽

| 用途 | 來源 | 抓取指令 | 能否進正式研究 |
| --- | --- | --- | --- |
| [150 檔研究日線](#150-檔研究日線) | Yahoo Finance | `scripts/collect_history.py` | 只供技術研究，不供帳本成交價 |
| [月營收、重大訊息](#月營收重大訊息) | 證交所／櫃買中心 MOPS 開放資料 | `cli/data_agent.py collect` | 可，作事件與基本面事實 |
| [官方財報](#官方財報) | MOPS 損益表、資產負債表 | `scripts/collect_financial_statements.py` | 可 |
| [公司基本資料](#公司基本資料) | 證交所／櫃買中心公司資料 | `scripts/collect_sector_classification.py` | 候選；正式讀已驗證的檔案 |
| [產業比較資料](#產業比較資料) | 證交所／櫃買中心各產業 EPS 統計（t187ap14） | `cli/industry_peer_collect.py` | 否（C1 只收集與稽核） |
| [估值資料（V1）](#估值資料v1) | 證交所 `BWIBBU_ALL`（當日）、FinMind `TaiwanStockPER`（歷史） | `cli/valuation_collect.py collect` | 否（V1 只收集與稽核，不進決策與簡報；僅上市股） |
| [多季基本面候選](#多季基本面候選) | FinMind 三張財報 | `scripts/collect_supplemental_sources.py finmind` | 部分：現金流經核對後使用；其餘僅補抓去年同期時使用 |
| [個股新聞](#個股新聞) | FinMind 個股新聞 | `scripts/collect_supplemental_sources.py finmind-news` | 可，只進事件分析師的公司事件 |
| [正式帳本價量](#正式帳本價量) | 證交所／櫃買中心官方價量 | `scripts/collect_latest_prices.py` 等 | 可，帳本結算依據 |
| [基準標的](#基準標的) | 證交所個股日成交資訊（0050） | `./start.sh benchmark collect` | 績效比較；2026-11-12 起另作風險 Agent 的背景事實（benchmark_facts） |
| [交易限制與日曆](#交易限制與日曆) | 政府開放 CSV 七份、證交所開休市表 | `scripts/run_daily_pipeline.py` | 可，風控必要輸入 |
| [交易池](#交易池) | 主辦方名單 | `data/universe.py` | 可，一次性版本化輸入 |
| [隔夜市場背景](overnight_market.md) | Yahoo Finance 的美股與亞股指數；期交所官網即時行情的台指期夜盤 | `cli/overnight_market.py capture` | 使用者已核准；只作現金姿態與報告背景 |
| [台股恐懼貪婪指標](market_fear_greed.md) | 期交所 VIX、FinMind 融資、證交所加權指數 | `cli/market_fear_greed.py capture` | 可，作全市場情緒（新聞不可用時） |
| [全市場新聞](market_news_integration.md) | 中央社、自由時報 RSS 與歷史原頁 | `cli/market_news.py capture` | 使用者已核准；作全市場情緒 |

## 150 檔研究日線

- 來源：Yahoo Finance，含開高低收量、原始收盤與調整收盤。`./start.sh data` 與每日流程都會抓，並檢查 150 檔同日都有資料。
- 用途：技術研究。Snapshot 與歷史 K 線同日優先用 Yahoo，該股該日缺資料時改用同日官方價。
- 不供正式帳本的成交均價。決策與帳本的用價一律是官方原價，不受 Yahoo 影響。

### 增量重抓

每次重抓最近 7 天。遇到下列情形，Yahoo 的價格基準已改變，該檔整段重抓：

| 情形 | 判斷方式 |
| --- | --- |
| 現金股利 | 還原係數（調整收盤 ÷ 收盤）出現「較早日期大於較晚日期」 |
| 配股、分割、減資 | 重疊區間同日收盤價的相對差超過 0.01%（這類事件 Yahoo 會把事件前的收盤與調整收盤同比例改寫，只能比對同日收盤） |
| 停抓超過 7 天 | 重疊區間看不到被改寫的舊列，直接整段重抓 |

已知限制：若 Yahoo 延遲超過 7 天才改寫，每日比對仍可能看不到。

### 定案時間與版本

- Yahoo 盤中就會回傳當天那根日線，但收盤價與成交量尚未定案。規則：台北 15:00 之後才算定案（收盤是 13:30）。15:00 以前抓到的當日日線不寫入資料庫；讀取端也只採用「定案後才抓到」的 Yahoo 列，沒有就改用同日官方價，官方價也沒有就不納入該日。（程式：`src/etf_agent/data/price_finality.py`）
- 價格表只追加、不覆寫。重抓內容相同就不新增；內容改變才新增一個版本。查詢一律取「抓取時間不晚於決策截止時間」的最新版本，所以事後用舊截止時間重建，拿到的是當時的版本，不會被後來的重抓蓋掉。
- 舊資料庫用 `cli/data_agent.py migrate --database var/etf_agent.db` 遷移（可重複執行，初始化時也會自動跑一次）。遷移前已被覆寫的歷史版本無法還原。

### 缺資料時的補法

Yahoo 缺某天時，用前後 Yahoo 的還原係數換算：

- 係數 = Yahoo 調整價 ÷ 同日官方原價；不用 Yahoo 收盤價，因為它可能被事後改寫。
- 同日沒有官方原價的 Yahoo 列不參考；缺口因此算不出係數時，排除該日。
- 前後係數不同時，用證交所月行情漲跌欄的 `X` 標記定位除權息日：除權息日不晚於缺口用後端係數，晚於缺口用前端係數。標記缺漏或區間內有多個除權息日，就排除該日，不拿原價混入。

### Snapshot 的價格欄位

| 欄位 | 用途 |
| --- | --- |
| 決策用價（`execution_close_price` 等三欄） | 同日官方原價，配置、風控、備援 D-Plan、虛擬帳戶都用它 |
| 分析用價（`analysis_close_price`） | 調整價，只供研究。重放過去日期時，含該日之後的除權息調整 |

同日沒有官方價時決策用價為空，決策退回分析用價。舊封存沒有這幾欄，處理相同。

## 月營收、重大訊息

- 來源：證交所／櫃買中心 MOPS 開放資料（月營收 `t187ap05`、重大訊息 `t187ap04`）。
- 每日由 `cli/data_agent.py collect` 抓取，依公告時間與實際取得時間保存，供事件與基本面事實使用。

## 官方財報

- 來源：MOPS 各業別損益表與資產負債表（`t187ap06`／`t187ap07`）。
- 每日由 `scripts/collect_financial_statements.py --latest-due` 抓最近已到期的季度。正式基本面只用已驗證的官方資料。
- 實測覆蓋：2026 Q2 為 298／300；`3718.TWO` 兩張表期間未驗證。

## 公司基本資料

- 來源：證交所／櫃買中心公司資料（含產業分類）。每日由 `scripts/collect_sector_classification.py` 保存候選。
- 正式決策仍讀已驗證的 `data/sector_classification.json`，候選不會直接生效。

## 產業比較資料

- 來源：證交所 `t187ap14_L`（上市公司各產業EPS統計資訊）與櫃買中心 `mopsfin_t187ap14_O`（上櫃公司各產業EPS統計資訊）；2026-11-10 使用者視同登記為政府開放資料來源，核對紀錄見[來源核對](source_audit/2026-11-10_industry_peer_sources.md)，登記在 `config/industry_peer_sources.json`。
- 內容：每家已公布本期的公司一列，含產業別名稱、營業收入、營業利益、稅後淨利、基本每股盈餘（千元）。2026-11-10 上市 296 家、上櫃 173 家，不是全市場。
- 保存：共用資料庫 `industry_peer_stats`（每日列、內容變動才新版）；交易池逐檔與官方損益表核對，結果與代碼↔名稱表（`artifacts/data/industry_name_map_latest.json`）在 `artifacts/data/industry_peer_latest.json`。
- 狀態：**只收集與稽核，不進 bundle、`team_inputs`、簡報或任何 Agent 輸入**，沒有計算產業中位數（C2 另案）。`INDUSTRY_PEER=0` 關閉每日收集。

## 多季基本面候選

- 來源：FinMind 的損益表、資產負債表、現金流量表。`./start.sh data` 與每日流程逐股更新；財報只在公布區間內變動，所以平常只補最近兩季，由 `scripts/finmind_schedule.py` 決定當天要不要抓、從哪天起抓。
- 實測覆蓋：三張表各 149／150；`3718.TWO` 為空。
- 目前實際用到的部分：
  - 現金流量表：與官方累計稅前淨利相符才轉成 Snapshot 文件（`scripts/collect_cash_flow.py`），進基本面分析。
  - 單季損益：用來補去年同期累計，需手動執行[基本面缺口補抓](fundamental_data_repair.md)。
- 限制：FinMind 的 `date` 是財報期間，不是發布時間；其餘候選資料尚未接入 Snapshot 或歷史回測。

## 估值資料（V1）

### 是什麼

上市股每日本益比（PER）、股價淨值比（PBR）、殖利率。設計見 [優質股計畫第 2.6 節](quality_dip_plan.md)；來源核對紀錄見 [2026-11-10 估值來源核對](source_audit/2026-11-10_valuation_sources.md)。

### 怎麼用

- 官方當日資料（證交所 `BWIBBU_ALL`，無歷史）是核對基準；FinMind `TaiwanStockPER` 提供歷史。同一資料日的三個欄位以 Decimal、兩位小數逐檔比對，**完全相符才接受**；不符、任一邊缺、資料日對不上、欄位名變更一律拒絕該檔，不取平均、不擇一、不填假值。
- `available_at` 一律是實際抓取時間，不回填。FinMind 資料日之前的歷史標 `unverified_history`（無法逐日核對）；資料日當天標 `verified_against_official`。
- PER／PBR 為空、0 或負：記 `null` 與原因碼（`NOT_PROVIDED`、`NONPOSITIVE`），不當成 0，也不當成便宜。
- 每次抓取存成不可變 run（`artifacts/valuation_collection/<日期>/valuation-<UTC 時間>/`），含官方與 FinMind 原始回應、逐檔紀錄、歷史、稽核摘要；更正另存新 run。`cli/valuation_collect.py validate <run_id>` 以原始回應整份重建並比對。
- **授權登記**：`license_status=approved`、範圍 `valuation_v1_collection_and_audit_only`、核准日 2026-11-10、核准人使用者。只限 V1 收集與稽核，不進決策、簡報或報告。

### 限制

- 只收上市股（交易池 100 檔）。上櫃端點（櫃買中心 `tpex_mainboard_peratio_analysis`）是否為正式端點、口徑與授權尚未確認，上櫃股估值一律不可用。
- 官方端點沒有歷史，所以 FinMind 在開始收集之前的歷史無法逐日核對。
- PER 的 EPS 基準與殖利率的股利基準尚待確認，確認前不得進簡報。
- 預設只補資料日前 14 天；第一次建歷史請加 `--history-start 2019-01-02` 之類的明確起日。
- 失敗（官方端點、金鑰、格式）退出碼 1，部分股票被拒絕退出碼 2。尚未排進每日流程；排程時必須是盡力而為，失敗不得讓 D-Plan 失敗。

## 個股新聞

### 是什麼

FinMind 個股新聞，含股票代號、標題、原媒體、連結與來源時間字串。它只幫「事件分析師」判斷個別公司有沒有重大事件，**不進全市場情緒，也不進回測**。

### 一天怎麼跑

1. 平日 20:40 排程執行 `scripts/run_finmind_news.sh`（即 `finmind-news`），與 20:00 財報排程錯開，避免同一小時合計約 600 次請求耗盡免費額度。
2. 21:00 決策建立 Snapshot 前，檢查當天逐股成功擷取是否少於交易池，不足就補抓一次（上限 15 分鐘）。補抓失敗只記錄，不中止決策，事件分析師照舊只看重大訊息。

### 保存與使用規則

| 項目 | 規則 |
| --- | --- |
| 授權 | 收集時一律先存「未核准」候選；核准狀態另存。核准指令 `cli/company_news.py review ... --status approved\|unverified\|restricted`，會抓 FinMind 規範頁原文與 SHA-256 存證，只對存證之後的決策截止時間生效，之後的 `restricted` 會取代前面的核准 |
| 核准現況 | 使用者已核准（2026-10-25、2026-10-27 各一筆，資料庫可查）：只用標題、內部研究、不轉載，這是使用者自行判斷，不是媒體書面同意。依據見[來源核對](source_audit/2026-10-25_finmind_stock_news.md) |
| 時間 | 來源時間沒有時區，所以發布時間留空，可得時間是第一次抓到的時間 |
| 媒體 | 只收正規新聞媒體（依連結網域白名單）；CMoney 貼文、聚財網論壇、富聯網、LINE TODAY、Yahoo股市、券商等不收 |
| 視窗 | 只取決策截止前最後一批抓取（該批抓取時間往前 12 小時，且不早於截止前 3 天）；排程中斷就輸出空清單，不拿舊聞補。空回應與全部重複的回應也算一批 |

事件報告怎麼使用，見[事件研究整合](integrated_event_analysis.md#事件報告-23已核准個股新聞)。

### 已知限制

- 授權：FinMind 服務使用權不等於原媒體的再利用授權，核准是使用者自行承擔的判斷。
- 完整性：假設已核准試算 10-20～10-23，每日約 80～100 則、約 40 檔；10-23 那批資料庫只有 82 筆、39 檔（白名單內 17 筆），看起來不完整。
- 實測覆蓋（候選階段，不代表新聞完整）：查詢 150 檔、116 檔有線索、34 檔空結果、共 1,004 筆。

## 正式帳本價量

帳本用官方價量結算：主辦方要求以交易所前日收盤價推導委託股數，以當日成交金額 ÷ 成交股數結算。

### 來源

| 範圍 | 來源 |
| --- | --- |
| 當日全市場（上市） | 證交所 `STOCK_DAY_ALL`；延遲時補 `exchangeReport/STOCK_DAY` |
| 當日全市場（上櫃） | 櫃買中心 `tpex_mainboard_quotes` |
| 歷史月行情（上市） | 證交所 `STOCK_DAY` |
| 歷史月行情（上櫃） | 櫃買中心 `afterTrading/tradingStock`（網址設定在 `config/data_sources.json`）；成交張數、仟元換算為股、元 |
| 待結算的歷史上櫃缺口 | 櫃買中心 `afterTrading/dailyQuotes`，指定日期，原始股數／元 |

### 誰在抓

- `scripts/collect_latest_prices.py`、`scripts/collect_official_history.py`：只有 `./start.sh daily` 執行。
- `scripts/repair_pending_tpex_close.py`：補歷史上櫃的帳本缺口。

### 怎麼避免被來源封鎖

個股月行情逐檔、逐月請求，所有執行緒共用同一個節流器：

- 每次請求至少間隔 2 秒（`config/data_sources.json` 可調）。
- 遇到 HTTP 308／403／429／503 視為限流，全體依 30、120、300 秒退避。
- 退避用盡仍被限流就停止後續請求，缺口留到下次補抓。

### 除權息標記

- 上市：直接用漲跌欄的 `X`。
- 上櫃：漲跌欄沒有 `X`，改以「參考價（收盤 − 漲跌）不等於前一交易日收盤」推導。
- 月內第一列沒有前一日可比，標記維持未知，不猜。

### 缺資料時

缺官方價量就等待，**不以 Yahoo 代替**。歷史補抓保留原始回應、實際抓取時間與來源。

## 基準標的

### 是什麼

讓虛擬帳戶績效能和兩條基準比較：「0050 買入持有」與「150 檔交易池等權買入持有」（儀表板「與基準比較」、`./start.sh benchmark compare`）。

口徑見 [benchmark/README.md](../src/etf_agent/benchmark/README.md)：同本金、開帳後第一個可成交日以官方成交均價整張買進、同一套費稅、以官方未還原收盤價估值、不計股利。

### 規則

- 來源：`config/benchmarks.json` 列出基準與授權狀態。0050 用與正式帳本相同的證交所個股日成交資訊端點，授權沿用「正式帳本價量」的核准；等權基準直接用交易池既有官方價量。
- 不用 Yahoo：授權只供研究，且沒有成交金額，算不出與帳本同口徑的均價。基準若設成 Yahoo 或非核准狀態，一律顯示 unavailable。
- 補抓：2026-10-23 前資料庫只有 0050 一筆全市場抓取殘留，須執行 `./start.sh benchmark collect` 補抓；`./start.sh daily` 會在帳本結算前補抓。
- 不污染交易池：基準以「不屬於交易池」的標記保存，交易池驗證、分析用價格與當日價格查詢都只看交易池成員，不會因此多一檔。
- 時點：比較只採用「抓取時間與收盤都不晚於 as-of」的官方日線版本；補抓的歷史列以實際抓取時間為可得時間，不回填。
- 用途擴大（2026-11-12 使用者核准）：0050 行情除績效比較外，可作風險 Agent 的背景事實（`position_sizing.benchmark_facts`，已實作、尚未開啟；紀錄用、不可引用、不作交易標的），來源與端點不變。
- 0050 成分權重（benchmark_facts 的 active_weights 用）：人工抄錄元大投信官網持股權重頁，存 `data/benchmark_holdings.csv`，來源核准在 `config/benchmark_holdings_sources.json`，稽核紀錄見 [2026-11-12_benchmark_holdings.md](source_audit/2026-11-12_benchmark_holdings.md)。該頁使用條款**待確認**，屬使用者自行承擔的判斷；權重會隨股價每日漂移，須由人每週一、四更新，過期超過 `max_holdings_age_days` 即 `unavailable`。
- 限制：等權基準每檔預算為本金 ÷ 150，整張買不起的股票留現金並列在 `unaffordable_symbols`；基準不計現金股利與配股。

## 交易限制與日曆

- 來源：已核准的政府開放 CSV 七份、證交所開休市日期表。
- 由 `scripts/run_daily_pipeline.py` 保存並驗證，是正式風控與 T+2 交割的必要輸入。缺漏或過期就停止（fail-closed）。

## 交易池

- 來源：主辦方名單，整理成 `data/official_universe.csv`，由 `data/universe.py` 驗證為 150 檔。
- 一次性的版本化輸入，不是每日網路擷取。

## 執行與憑證

- `./start.sh data` 只抓研究日線與公司資料，不執行決策或帳本。
- FinMind Token 從環境變數、本機 `.env`（權限 600、被 Git 忽略）或 macOS 鑰匙圈載入；直接執行 CLI 時須自行提供 `FINMIND_TOKEN`。
- 個股新聞與財報排程分開，原因見上。

## 已移除的來源

台糖 RSS 與 150 檔新聞需求無關，已從每日擷取移除；既有原始回應保留供稽核。

## 尚未接入

- FinMind 多季財報候選尚未進 Snapshot 或歷史回測（只有現金流與手動補抓的去年同期累計會被使用）。
- 個股新聞沒有發布時間時區，也沒有接入回測。

## 來源依據

- [FinMind 基本面](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)
- [FinMind 個股新聞欄位與單日限制](https://finmind.github.io/en/tutor/TaiwanMarket/Others/)
- [FinMind 資料授權說明](https://finmind.github.io/en/Disclaimer/)
- [主辦方 D-Plan 指南](../game_docs/D-Plan_撰寫指南_v4.3.md)
