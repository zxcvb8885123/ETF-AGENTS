# 資料來源盤點：現有來源、Agent 需求與缺口

> 本文第「資料庫現況」的筆數是 2026-09-29 修改前的盤點快照，不代表新接入來源已有真實資料。

## 2026-09-29 更新：來源分工與新增擷取器

| 用途 | 來源與現況 | 可用界線 |
| --- | --- | --- |
| 150 檔研究日線 | 每日資料階段由 `yfinance` 批次擷取至台北當日，並檢查截止日 150 檔覆蓋；已出現 Yahoo 資料的股票不再把官方原價接入研究序列。原始收盤與 `Adj Close` 分欄 | 截止日不完整時退出碼 2、每日流程停止；仍須核對拆股／除息與跨日調整口徑 |
| 競賽帳本成交與收盤 | 目前仍靠 TWSE／TPEx 官方行情；成交均價需要成交金額 ÷ 成交股數，Yahoo 紀錄沒有成交金額 | 資料專用排程不補官方個股月行情；`daily` 決策結算時才補，缺價量仍等待。改用 Yahoo 收盤成交須另改比賽口徑並標示差異 |
| 官方財報／公司資料 | 現有 MOPS、TWSE／TPEx 財報與公司資料保留，作原始發布與交叉核對證據 | 官方資料不因 FinMind 接入而移除 |
| FinMind 財報 | 每日資料階段在有 `FINMIND_TOKEN` 時逐股更新三大報表；原始回應與內容版本獨立保存 | `date` 是財報期間，缺原始發布時間時 `published_at=NULL`、`available_at=本次抓取時間`；未接入 Snapshot 與回測 |
| 新聞候選 | 每日資料階段擷取政府開放資料授權的台糖 RSS；保存來源／授權／發布與抓取時間、去重版本 | 台糖範圍有限，未映射 150 檔，也未接入事件研究／情緒。商業媒體及 TWSE RSS 未核實機器擷取授權，不啟用爬蟲 |

操作：`FINMIND_TOKEN` 在執行環境設定；執行 `.venv/bin/python scripts/collect_supplemental_sources.py finmind --dataset TaiwanStockFinancialStatements --stock-id 2330 --start 2025-01-01 --end 2026-09-29`，三大報表可改 `--dataset` 並以 `--universe data/official_universe.csv` 逐股回補。新聞執行 `.venv/bin/python scripts/collect_supplemental_sources.py news`。150 檔逐股、三種報表約 450 次請求，需留意 FinMind 額度；中斷可重跑，同內容去重。CLI 以退出碼 2 標示空結果。這些命令只保存候選資料，不代表正式研究或回測已可使用。不要將 Token 放進命令列、設定檔、日誌或 Git；已在對話中揭露的 Token 建議到 FinMind 重新產生。

來源依據：[FinMind 基本面資料集](https://finmind.github.io/tutor/TaiwanMarket/Fundamental/)、[FinMind 認證與額度](https://finmind.github.io/quickstart/)、[台糖新聞開放資料授權](https://data.gov.tw/dataset/41593)。

每日自動擷取：已安裝 `com.etf-agents.data`，台北時間平日 20:00 執行 `./start.sh data`；只用 yfinance 抓價格日線，官方 OpenAPI 用於財報、公司資料與事件，不啟動決策 Agent 或帳本。Yahoo 當日缺檔仍繼續擷取其他來源，最後回報非零狀態。執行日誌在 `artifacts/daily_runs/data-launchd.log`。FinMind 需先把輪替後的 Token 放進執行環境或 macOS 鑰匙圈服務 `etf-agent-finmind`；目前未設定，排程會略過 FinMind 並回報降級。另有手動 `./start.sh daily` 的正式帳本路徑仍用官方價量計算比賽成交均價。

**剩下三個官方行情端點仍供正式比賽帳務使用。** [主辦方 D-Plan 指南](../game_docs/D-Plan_撰寫指南_v4.2.md)要求用交易所前日收盤價推導委託股數，並以當日成交金額 ÷ 成交股數結算；yfinance 日線沒有成交金額。上市個股月資料不再供研究日線回補，但 `daily` 仍以它補足全市場端點延遲時的帳本價量。探索性模擬若選用 Yahoo 收盤價，不得標成正式帳本。

## 一句話結論

- **原盤點時**抓資料的工具只有標準庫 `urllib` 與 `yfinance`；新增 FinMind 與台糖開放新聞的擷取器，已納入每日資料階段，但正式研究仍只用既有來源。
- **決策實際用的技術指標幾乎全來自 Yahoo**，官方歷史行情有 18 個月空白。
- **最大的即時問題**：上市當日行情（證交所 OpenAPI `STOCK_DAY_ALL`）每天晚一天更新，帳本無法在當晚結算。

## 一、資料是用什麼抓的

| 來源 | 抓什麼 | 用什麼抓 | 程式 | `data` 排程／`daily` 決策 |
| --- | --- | --- | --- | --- |
| 證交所 OpenAPI `STOCK_DAY_ALL` | 上市官方收盤價、成交股數與成交金額 | `urllib` | `scripts/collect_latest_prices.py` → `data/twse.py` | 不跑／會跑，供正式帳本 |
| 櫃買 OpenAPI `tpex_mainboard_quotes` | 上櫃官方收盤價、成交股數與成交金額 | `urllib` | 同上 → `data/tpex.py` | 不跑／會跑，供正式帳本 |
| 證交所 `exchangeReport/STOCK_DAY` | 上市個股月官方價量；全市場端點延遲時補當日資料 | `urllib`（4 執行緒、重試 3 次） | `scripts/collect_official_history.py` → `data/historical.py` | 不跑／會跑，供正式帳本補缺 |
| **Yahoo Finance** | 2 年日線、**還原價**、成交量 | **`yfinance` 套件**（每批 30 檔、重試 2 次） | `scripts/collect_history.py` → `data/yfinance_history.py` | ✅ |
| FinMind 三大財報 | 損益表、資產負債表、現金流量表 | `urllib`，Bearer header | `scripts/collect_supplemental_sources.py finmind` → `data/supplemental_sources.py` | ✅ 有 Token 時每日候選，未接 Snapshot |
| 台糖政府開放新聞 | 授權 RSS 新聞候選 | `urllib`，XML parser | `scripts/collect_supplemental_sources.py news` → `data/supplemental_sources.py` | ✅ 每日候選，未接研究 |
| 證交所／櫃買 MOPS 開放資料 `t187ap05` | 月營收（含月增、年增、累計） | `urllib` | `cli/data_agent.py collect` → `data/corporate.py` | ✅ |
| 同上 `t187ap04` | 重大訊息（當日公告） | `urllib` | 同上 | ✅ |
| 同上 `t187ap06`／`t187ap07`（各業別共 24 個端點） | 損益表、資產負債表（一般業、金融、保險、證券、金控…） | `urllib` | `scripts/collect_financial_statements.py --latest-due` → `data/financial.py` | ✅ |
| 政府開放 CSV × 7 | 停牌、變更交易、分盤、處置、注意（上市＋上櫃） | `urllib`，保存原始位元組與 SHA-256 | `scripts/run_daily_pipeline.py` 階段 0 → `data/source_capture.py` | ✅ |
| 證交所開休市日期表 CSV | 休市日、僅交割日 | `urllib`（同上封存） | 同上 → `data/trading_calendar.py` | ✅ |
| 證交所 `t187ap03` | 公司基本資料 → 產業分類 | `urllib` | `scripts/collect_sector_classification.py` → `data/sector_classification.py` | ✅ 每日候選存 `artifacts/sector_classification_latest.json`；正式決策仍用已驗證 `data/sector_classification.json` |
| 主辦方 PDF | 150 檔交易池 | 人工整理成 `data/official_universe.csv` | `data/universe.py` 驗證 | ❌ 一次性 |
| 來源健康探測 | 每天檢查官方端點能否連線 | `urllib` | `scripts/probe_data_sources.py` → `data/source_health.py` | ✅ |

櫃買 `afterTrading/tradingStock` 的擷取器、設定與解析器已移除。既有 `TPEX_TRADING_STOCK` 歷史列仍可讀取以重建舊封存；新資料不再請求此端點。上櫃帳本當日價量仍取 `tpex_mainboard_quotes`，研究日線取 yfinance。

既有正式資料寫進 SQLite，再由 Snapshot 固定 cutoff 與版本。新來源也保存 `raw_payloads`、`collection_runs`，並新增 `supplemental_facts`／`news_candidates`；目前尚未納入 Snapshot。

**沒有接入正式研究流程**：FinMind 財報與新聞候選；仍沒有 FinLab、Fugle、法說會、券商研究、情緒與分析師共識、三大法人與融資融券、除權息公告。Token 已由使用者提供，程式只讀環境變數，不保存憑證。

## 二、資料庫現況

| 資料 | 筆數與範圍 |
| --- | --- |
| `daily_prices`（行情） | 82,608 筆。Yahoo 73,326（151 檔、2024-09～2026-09-24）；證交所 `STOCK_DAY` 3,228；櫃買 `tradingStock` 4,037；`STOCK_DAY_ALL` 1,767（含全市場，交易池外也有）；`tpex_mainboard_quotes` 250 |
| 月營收 | 296 筆，約 150 檔 × 2 個月，149 檔有年增率 |
| 重大訊息 | 83 則，只涵蓋 **37 檔**，時間只有 9/14～9/28 |
| 財報 | 2026 Q2 損益表＋資產負債表，298／300 有資料，缺 `3718.TWO`（`TARGET_PERIOD_NOT_VERIFIED`） |
| 交易狀態 | 7 個來源已由你在 9/28 核准（時效 72 小時）；TWSE「管理股票」列為不適用；9/28 實測 148 檔 allowed、2 檔被處置擋下 |
| 產業分類 | 150／150 |
| 情緒／分析師共識 | 0（沒有合法來源，Agent 固定輸出 `unavailable`） |
| ETF 持股權重 | 0（`data/active_etf_top10.csv` 只有標題列，選配，不擋決策） |

## 三、你的 Agent 需要什麼資料

| Agent／步驟 | 需要的資料 | 現在的來源 | 夠不夠 |
| --- | --- | --- | --- |
| **技術分析師**（動能） | 每檔近 120 根日 K（OHLCV），至少 **61 根**才算 `available`；算收盤、5／20／60 日報酬、MA20／MA60、ATR14%、20 日下行波動、20 日均成交值 | 幾乎全是 Yahoo 還原價 | ⚠️ 有，但來源不是官方 |
| **基本面分析師** | 當季與去年同期的營收、淨利、營業利益、總資產、總負債，算營收年增、淨利年增、營益率、營益率年變化、負債比；月營收年增／月增／累計年增 | MOPS 財報 2026 Q2、月營收 | ⚠️ 只有一季的比較，沒有毛利率、現金流量、估值、多季趨勢；金融業公式未做 |
| **事件分析師** | 重大訊息與月營收文件，逐檔判斷重要性 | MOPS 重大訊息 | ⚠️ 只有兩週、37 檔；沒有新聞、法說會、股利／除權息公告 |
| **情緒分析師** | 經授權、有歷史發布時間的情緒與分析師共識 | 無 | ❌ 固定 `unavailable` |
| 重大事件研究（Fact／Bull／Bear／Adjudicator） | 被標為 high 的事件原文與證據 | 同事件分析師 | ⚠️ 受限於事件資料深度 |
| 多頭／空頭研究員、交易 Agent | 只讀上游分析師報告與共同輸入，不需要額外資料 | — | ✅ |
| 風險 Agent | 帳戶、產業分類、交易狀態、競賽規則 | 帳本、`sector_classification.json`、交易狀態包 | ✅ |
| 配置引擎／Guard | 最新價、ATR、規則（持股 20～30 檔、現金 <25%、個股 10%、2330 25%、費稅） | 同上 | ✅ |
| 交易可行性 | 150 檔每檔的停牌、變更交易、分盤、處置、注意狀態；休市日曆 | 政府開放 CSV × 7＋開休市日期表 | ✅ 但日曆只涵蓋 2026 年 |
| **帳本結算** | 成交日 150 檔的收盤價、**成交金額與成交股數**（算當日成交均價）；官方交割日曆 | 官方行情 | ❌ 上市當日資料晚一天（見下） |
| D-Plan 匯出 | 主辦方 schema、指南、規則 | 已有 D-Plan v4.0 schema／guide／範例 | ⚠️ 主辦方 `verify_dplan.py` 沒取得 |

## 四、目前缺什麼（依嚴重程度）

### 🔴 擋住每天流程

1. **上市當日收盤資料太晚。** `STOCK_DAY_ALL` 抓到的日期一直落後一天：

   | 抓取時間 | 拿到的交易日 |
   | --- | --- |
   | 9/22 18:25 | 9/21 |
   | 9/23 20:53 | 9/22 |
   | 9/29 17:08 | 9/24 |

   同一時間證交所的個股日成交 `STOCK_DAY` 已有 9/29（2330 收 2,475.00）。上櫃 `tpex_mainboard_quotes` 也已有 9/29。系統會把上市、上櫃對齊到同一天，所以整份 Snapshot 退回 9/24，帳本也在等收盤價。

   **2026-09-29 已處理**：實測 `STOCK_DAY_ALL` 到 23:04 仍是 9/24，確認是結構性晚一天。官方個股日線補抓（`collect_official_history.py`）的迄日改為「任一市場最新行情端點已公布的最新交易日」，並加進 `start.sh daily`，上市當日日線改由 `STOCK_DAY` 補齊；Snapshot 仍要求 150 檔全覆蓋才採用該日。

### 🟠 影響決策品質

2. **官方歷史行情有 18 個月空白。** 官方個股月資料只有 2024-09～11 和 2026-07 起，中間（2024-12～2026-06）上市為 0 筆、上櫃每月約 20 筆。
3. **技術指標建立在 Yahoo 上。** 我逐根比對 9/28 決策的 150 檔價格序列：14,108 根只與 Yahoo 還原價一致、3,785 根 Yahoo 與官方相同、只有 3 根確定是官方價格。
4. **Yahoo 還原價與官方價格差距大。** 同一天比較，150 檔裡 21 檔差超過 2%、9 檔超過 5%；`6669.TW`（緯穎）差 198%（Yahoo 把拆分前價格往回調整）。目前序列內部一致所以指標沒錯，但只要官方與 Yahoo 在同一序列接起來，這類股票的動能會出現假的暴漲暴跌（`2330.TW`、`3260.TWO` 已有 3 根混用）。
4b. **上櫃兩個官方來源同日成交量不一致。** 9/29 的 `6488.TWO`：`tpex_mainboard_quotes` 8,849,000 股／`tradingStock` 9,157,000 股，成交均價差 0.08 元。帳本沿用優先序（個股日成交優先）；哪一個才是比賽的「當日成交均價」，主辦方原文未載明，待確認。
5. **沒有官方還原價或除權息資料**（我沒有查到已接入的官方除權息來源）；Yahoo 的成交金額欄位是 0，不能算成交均價，只能靠官方行情。
6. **財報只有一季，沒有現金流量表。** 同期比較資料薄，毛利率、估值、金融業專用公式都沒做。
7. **事件資料太淺。** 兩週官方重大訊息，沒有新聞與法說。

### 🟡 規劃中但沒有

8. 籌碼（三大法人、融資融券）。
9. 情緒與分析師共識（需合法、有歷史時間標記的來源）。
10. ETF 持股權重（選配）。

### ⚪ 維運

11. 資料專用排程不補官方個股月歷史行情；`daily` 決策為帳本結算仍需補官方個股價量。公司分類每天擷取候選並保留原始回應，正式分類檔的升版仍需驗證。
12. 開休市日曆只涵蓋 2026 年，12/30 起的 T+2 交割會跨進 2027 而停止，需等證交所公布 2027 年日期表。
13. 交易狀態核准時效 72 小時；跨長假封存過舊會退為 `STALE_CAPTURE`，Guard 擋下。

## 五、可以決定的事

| 決定 | 選項 |
| --- | --- |
| 上市當日收盤與成交均價 | 保留官方每日行情；可評估 `STOCK_DAY` 補當日上市個股並做 150 檔覆蓋驗證，未完成前結算繼續 fail-closed |
| 補官方歷史行情 | 研究日線優先核驗 Yahoo 150 檔覆蓋與調整口徑；官方歷史回補先不列日常必要工作，僅在交叉核對或帳務證據缺口時執行 |
| 技術指標的價格來源 | 短期：維持 Yahoo 還原價，每天與官方漲跌幅對帳、差太多的股票標出不算指標；長期：用官方除權息資料自己算還原價 |
| FinMind 後續 | 已有 Token 環境變數擷取器；先逐股核對多季財報與現金流覆蓋、單位、口徑、發布時間，再決定能否升級 Snapshot 契約 |
| 新聞後續 | 台糖開放資料已可保存候選，但 150 檔相關新聞來源與逐篇授權仍需核實；未有合格來源前不宣稱新聞覆蓋 |
| 情緒／共識 | 目前無合法來源；接入前維持 `unavailable` |
