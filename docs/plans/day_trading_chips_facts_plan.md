# 逐檔當沖與短線籌碼事實（暫名 `position_sizing.chips_facts`）：評估與計劃

狀態（2026-11-15）：**只有計劃，尚未實作**。沒有改任何程式、設定、Policy 或來源核准檔。本文結論是「比賽結束前不接進決策鏈」，理由見第 7 節。

## 1. 起因與要回答的問題

使用情境：持股像 1802 台玻這樣短期大漲時，交易 Agent 要判斷「短線過熱，還是仍然強」。目前交易 Agent 看得到價格事實（報酬、持股檢視警示 `gain_alert`／`trail_alert`），看不到「這波量是誰做出來的」：當沖比重、融資增減、融券與借券賣出餘額。

想評估的做法：比照 `market_index_facts`、`exposure_view`、`quality_dip_facts`、`benchmark_facts`，把逐檔籌碼算成**純紀錄的背景事實**（`citable=false`、沒有 `evidence_id`、不改配置），由 Python 計算，Agent 不輸出數字。

## 2. 現況：哪些已接、哪些沒有（事實，2026-11-15 以 grep 核對）

| 資料 | 專案現況 | 位置 |
| --- | --- | --- |
| 全市場融資金額（合計，不是逐檔） | 已接入，只供恐懼貪婪指標 | `src/etf_agent/perception/market_fear_greed/`，FinMind `TaiwanStockTotalMarginPurchaseShortSale`，來源代碼 `FINMIND_MARGIN` |
| 逐檔融資融券 | **沒有** | `src/`、`scripts/`、`cli/`、`config/` 都沒有 `TaiwanStockMarginPurchaseShortSale` 或 `MI_MARGN` 的抓取程式 |
| 逐檔當沖 | **沒有** | 全專案沒有 `DayTrading`、`TWTB4U`、「當沖」的程式 |
| 三大法人（逐檔或合計） | **沒有** | 只在文件與 Skill 文字提到「法人買賣只作交易背景」，沒有抓取程式 |
| 借券、借券賣出餘額 | **沒有** | 同上 |

專案目前用到的 FinMind 資料集只有：`TaiwanStockBalanceSheet`、`TaiwanStockCashFlowsStatement`、`TaiwanStockFinancialStatements`、`TaiwanStockNews`、`TaiwanStockPER`、`TaiwanStockTotalMarginPurchaseShortSale`。

交易池是上市 100 檔、上櫃 50 檔（`data/official_universe.csv`），所以來源必須兩個市場都涵蓋。

## 3. 候選資料來源（2026-11-15 22:39 台北時間實測）

查證方式：讀 FinMind 文件頁原文、下載證交所與櫃買中心 OpenAPI 的 swagger、對公開端點各發一次唯讀請求（不帶金鑰、不帶任何使用者資料）。下表「文件寫」是來源自己的說法，「實測」是本次請求看到的結果，兩者分開。

### 3.1 FinMind

| 資料集 | 欄位 | 歷史（文件寫） | 更新時間（文件寫） | 實測 |
| --- | --- | --- | --- | --- |
| `TaiwanStockDayTrading` 當日沖銷交易標的及成交量值 | `stock_id`、`date`、`BuyAfterSale`、`Volume`（股）、`BuyAmount`、`SellAmount`（元） | 2014-01-01 起 | 標的名單與 `BuyAfterSale` 盤前可得；**成交量值收盤後 21:30** | 不帶金鑰逐檔可取；1802 有 2014-01-06 的列；最新列 2026-11-13；上櫃 8299 也有資料 |
| `TaiwanStockMarginPurchaseShortSale` 個股融資融券 | 融資／融券的買進、賣出、償還、昨日餘額、今日餘額、限額、`OffsetLoanAndShort`（資券互抵）、`Note` | 2001-01-01 起 | 平日 21:00 | 不帶金鑰逐檔可取；單位是**張**（1802 的 11-13 融資餘額 47796，與證交所 OpenAPI 同日數字相同） |
| `TaiwanDailyShortSaleBalances` 信用額度總量管制餘額 | 融券與借券賣出的前日餘額、賣出、回補／還券、當日餘額、限額 | 2005-07-01 起 | 平日 21:00 | 不帶金鑰逐檔可取；單位是**股**（與上一列不同單位） |
| `TaiwanStockInstitutionalInvestorsBuySell` 法人買賣 | `date`、`stock_id`、`name`（法人別）、`buy`、`sell` | 2005-01-01 起 | 平日 20:00；文件明寫**數值可能於初次公布後微幅更新**、沒有固定定案時間 | 不帶金鑰逐檔可取 |
| `TaiwanStockSecuritiesLending` 借券成交明細 | 交易方式、成交量、費率、收盤價、約定還券日與天數 | 2001-05-01 起 | 平日 15:00 | 不帶金鑰逐檔可取；一天多筆明細 |
| `TaiwanStockMarginMaintenance` 個股融資維持率 | 未查證 | 未查證 | 未查證 | 未實測；全市場維持率 `TaiwanTotalExchangeMarginMaintenance` 在 2026-11-04 核對時 free／register 等級被擋 |

其他實測事實：

- 「一次拿特定日期全部股票」被擋：不指定 `data_id` 回 400（`Your level is free`）。所以只能逐檔抓，150 檔 × 每個資料集一次請求。
- 請求上限：文件頁寫不帶金鑰 300 次／小時、帶金鑰 600 次／小時（該頁中英文數字互相矛盾，以此解讀為準，**實際上限未實測**）。專案既有的 20:00 財報排程與 20:40 新聞排程已為了這個額度錯開。
- 21:30 與 21:00 的更新時間是文件說法，**本次在週日查證，無法實測交易日當晚幾點真的拿得到**。
- `Volume` 的口徑（是否為單邊股數、是否含鉅額與盤後）**未查證**；實測 1802 的 `Volume` ÷ 同日 `TaiwanStockPrice.Trading_Volume` 都落在 100% 以內。

### 3.2 證交所與櫃買中心

| 端點 | 內容 | 實測 | 能不能當主來源 |
| --- | --- | --- | --- |
| 證交所 OpenAPI `exchangeReport/TWTB4U`（上市股票每日當日沖銷交易標的及統計） | `Date`、`Code`、`Name`、`Suspension` | 1244 列，`Date` 是 `1151116`（下一個交易日）；**只有標的名單與暫停註記，沒有成交量值** | 不能：沒有當沖量 |
| 證交所 OpenAPI `exchangeReport/MI_MARGN`（集中市場融資融券餘額） | 逐檔融資融券買賣、餘額、限額、資券互抵 | 1309 列；**回應沒有日期欄位**，只有當日 | 只能當同日核對基準，且資料日要另外推定；不能算近 N 日變化 |
| 證交所 OpenAPI `SBL/TWT96U` | 當日可借券賣出股數 | 只確認端點存在，內容未查證 | 未評估 |
| 櫃買中心 OpenAPI `tpex_intraday_trading_statistics`（上櫃股票現股當沖交易統計） | 日期、當沖量、占市場比率、買賣金額 | 10 列，**是全市場合計，不是逐檔** | 不能 |
| 櫃買中心 OpenAPI `tpex_mainboard_margin_balance`（上櫃融資融券餘額） | `Date`、逐檔融資融券買賣、餘額、使用率、限額 | 922 列，`Date` 是 `1151113`，只有當日 | 只能當同日核對基準 |
| 櫃買中心 OpenAPI `tpex_3insti_daily_trading`、`tpex_short_sell`、`tpex_margin_sbl` | 上櫃三大法人明細、當日融券與借券賣出、借券賣出餘額 | 只確認端點存在，內容未查證 | 未評估 |
| 證交所網站 `rwd/zh/dayTrading/TWTB4U`（可指定日期，含逐檔當沖量值） | 網頁說明寫自 2014-01-06 起提供 | **沒有實測**：專案 2026-10-23 的來源核對已認定證交所網站條款禁止未經同意的自動化擷取，本次不發請求 | 需使用者另行核准才可考慮 |
| 櫃買中心網站的逐檔當沖歷史查詢 | 未查證 | 未查證 | 未查證 |

小結（事實）：**官方開放 API 拿不到逐檔當沖成交量**，上市只給名單，上櫃只給全市場合計。逐檔當沖量目前只有 FinMind 能合規取得（或是需另外核准的證交所網站）。逐檔融資融券官方 OpenAPI 有，但只有當日，可用來與 FinMind 同日核對，做法同估值 V1。

### 3.3 授權狀態

| 來源 | 授權現況 |
| --- | --- |
| FinMind 整體 | 專案已核准的是 `FINMIND_MARGIN`（全市場融資）、`FINMIND_PER`（範圍限 V1 收集與稽核）、個股新聞與財報。**本文所有逐檔籌碼資料集都沒有核准紀錄**，須新增來源代碼與 `source_audit` 紀錄 |
| FinMind 免責聲明（2026-11-15 讀取 `finmind.github.io/en/Disclaimer/`） | 資料來自原始提供者的公開管道；不得轉售或再散布；需要原始提供者授權時由使用者自行取得。這是摘要，未逐條核對 |
| 當沖原始資料 | 第三方鏡像（國網中心資料平台）顯示「上市股票每日當日沖銷交易標的及統計」為政府資料開放授權條款第 1 版，但該資料集欄位只有名單與暫停註記，**不含成交量值**；`data.gov.tw` 原站頁面與資料集編號**未查證** |
| 逐檔融資融券原始資料 | 證交所融資融券餘額為政府資料開放平臺資料集 11680（專案 2026-10-23 的核對紀錄）；上櫃對應資料集**未查證** |
| 證交所網站 `rwd` | 專案既有結論：條款禁止未經同意的自動化擷取；`TWSE_TAIEX` 是使用者自行承擔的例外核准 |

結論：要用就必須由使用者核准，且當沖成交量值這一項找不到「原始資料是政府開放資料」的依據，屬於使用者自行承擔的判斷（比照 VIX、0050 成分權重的先例）。

### 3.4 實例：1802 台玻（FinMind 免金鑰實測，未與官方核對）

| 日期 | 收盤 | 成交股數 | 當沖股數 | 當沖占比 | 融資餘額（張） | 融券餘額（張） |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-11-06 | 57.8 | 86,576,215 | 64,243,000 | 74.2% | 45,031 | 897 |
| 2026-11-09 | 63.5 | 63,624,622 | 29,459,000 | 46.3% | 46,317 | 1,533 |
| 2026-11-10 | 69.8 | 172,431,331 | 63,299,000 | 36.7% | 38,819 | 3,608 |
| 2026-11-11 | 76.7 | 266,744,266 | 140,652,000 | 52.7% | 42,277 | 9,183 |
| 2026-11-12 | 81.7 | 486,399,100 | 260,771,000 | 53.6% | 32,814 | 10,749 |
| 2026-11-13 | 85.1 | 443,099,867 | 370,793,000 | 83.7% | 47,796 | 9,798 |

- 事實：11-06 到 11-13 收盤由 57.8 到 85.1，約 +47.2%，與使用情境描述相符；11-13 當沖占比約 83.7%，融資單日增加 14,982 張。
- 事實：同一檔在 10-20 到 11-06 沒有大漲時，當沖占比也常在 67%～80%。
- 推論：這檔平常就是當沖熱門股，「占比高」本身不代表過熱，要看相對自己歷史的變化，而且同樣的數字可以解讀成「短線客擁擠」或「人氣仍在」。**這個指標方向不明**，只能當背景，不能變成規則。

## 4. 要算的確定性指標（Python 計算，Agent 不輸出數字）

全部 Decimal 字串、逐檔、只對交易池成員；缺任何一項就該欄位為 null 並記 `data_gaps`，不補值。

| 指標 | 公式 | 需要的資料 |
| --- | --- | --- |
| `day_trade_share_1d` | 最新日當沖股數 ÷ 同日成交股數 | 當沖量、官方成交股數 |
| `day_trade_share_avg_5d`、`_avg_20d` | 近 5／20 個交易日「當沖股數合計 ÷ 成交股數合計」 | 同上 |
| `day_trade_share_vs_20d` | `1d` 減 `avg_20d`（百分點差，不設門檻） | 同上 |
| `margin_balance_change_1d`、`_5d` | 融資今日餘額相對 1／5 個交易日前的變動張數與比例 | 逐檔融資融券 |
| `margin_utilization` | 融資今日餘額 ÷ 融資限額 | 同上 |
| `short_balance_change_5d` | 融券餘額 5 日變動 | 同上 |
| `short_to_margin_ratio` | 融券餘額 ÷ 融資餘額（券資比） | 同上 |
| `offset_share_1d` | 資券互抵張數換算股數 ÷ 成交股數 | 同上，須先確認單位 |
| `volume_vs_20d` | 最新日成交股數 ÷ 近 20 日平均成交股數 | **只需既有官方價量，不需新來源** |

設計原則：

- 分母（成交股數）一律用專案已核准的官方價量（`STOCK_DAY`／櫃買中心），不用 FinMind 的 `TaiwanStockPrice`，避免兩個來源混用。
- 單位先統一：融資融券是張、當沖與借券賣出餘額是股；程式換算並在測試固定，單位對不上整份拒絕。
- 程式不設「過熱」門檻、不輸出標籤（例如不輸出 `overheated`）。只給數字與相對自己歷史的差，判讀留給交易 Agent。
- 法人買賣與借券明細第一版不做：法人資料文件明寫事後會更新（同一資料日有多個版本），借券明細是逐筆、要先定義聚合方式，兩者都會讓第一版變大。

## 5. 資料時間一致性

時間線（以 21:00 備案與隔天 07:00 正式重做為例）：

| 時間 | 事件 |
| --- | --- |
| T 日 13:30 | 收盤 |
| T 日 20:00 | 既有資料排程 |
| T 日 21:00 | 晚間備案決策開始。此時融資融券（文件寫 21:00）與當沖量（文件寫 21:30）**多半還沒有** T 日資料 |
| T 日 21:30 之後 | 來源才有 T 日當沖量 |
| T+1 日 06:00 | 既有資料排程；在這裡抓才拿得到 T 日籌碼 |
| T+1 日 07:00 | 正式決策重做，可用 T 日籌碼 |

規則：

- `available_at` 一律是**實際抓取時間**（含時區），不是資料日、不是來源宣稱的更新時間。資料日另存 `data_date`。
- 決策只取 `available_at <= decision_cutoff` 的抓取批次；每次抓取存成不可變 run，內容變了另存新版本，不覆寫。
- 21:00 的 run 看到的最新籌碼日會是 T−1 日，與 `snapshot.latest_trade_date`（T 日）不同：此時逐檔標 `stale` 並附 `data_date`，**不把 T−1 的數字當成 T 日**，也不讓決策失敗。07:00 的 run 才是完整的。
- 今天補抓的歷史（例如第一次開始收集時回補 20 日）`available_at` 是今天，不回填到過去；更早 cutoff 的決策或回放一律看不到這些資料。因此影子期前幾天的 20 日平均雖然算得出來（歷史在抓取當天就一次可得），但任何「回測過去 N 日」都只能標 `replay_reference_only`。
- 舊 run 逐位元不變：沒有閘門時不建立決策輸入的選配鍵（暫名 `chips_input`）、簡報、提示、Schema、`bundle_sha256`、`shared_input_sha256`、`team_inputs` 都不變。多空研究員不拿這份事實，共同輸入雜湊不動。
- 同一資料日 FinMind 與官方 OpenAPI 的融資融券數字不符：該檔拒絕、記原因碼，不取平均、不擇一（同估值 V1）。當沖量沒有官方可比對，標 `unverified_single_source`。

## 6. 版本閘門、fail-closed 與測試

### 6.1 分階段

| 階段 | 內容 | 是否進決策鏈 |
| --- | --- | --- |
| 0 收集與稽核 | 新增收集器與不可變保存、官方同日核對、稽核摘要；授權範圍登記為「只收集與稽核」 | 否。比照估值 V1、產業比較 C1，以盡力而為包住，失敗只記錄 |
| 1 影子事實 | 閘門 `position_sizing.chips_facts: {"version": "v1"}`：程式算事實並封存，**不放進任何 Agent 簡報** | 否（只多一份封存，供事後看） |
| 2 背景事實 | 同一閘門升 `v2`：交易 Agent 簡報附 `chips_background`（`citable=false`、沒有 evidence_id） | 是，但只是背景，不改配置 |

第 2 階段要等使用者把授權範圍從「只收集與稽核」擴大為「可作交易 Agent 背景」，並改來源核准檔；只改收集器常數會讓既有 run 重建失敗（估值 V2 已踩過這個限制）。

### 6.2 閘門設計

- 位置：`config/decision_policy.json` 的 `position_sizing.chips_facts`，只由人設定，列入 `strategy_reflection/contracts.py` 的人專用清單，策略反思不得改。形狀檢查登記在 `decision/sizing.py`；版本錯、多餘欄位整份 Policy 拒絕。
- 視窗（5、20）寫死在程式，不做成可調數字，避免多一組要人填的參數。若要人填（例如「最多容許幾個交易日的舊資料」），比照 `benchmark_facts.max_holdings_age_days`：必填、程式不給預設。
- 決策輸入選配鍵 `chips_input`：只在閘門開啟時建立、進 bundle 雜湊；事實由決策輸入重算，不新增 `team_inputs` 鍵、不升版（同 `benchmark_facts`）。
- 不新增 Agent 輸出欄位；不影響名額、權重、股數、現金、排序、換股、Guard。回放略過（notes 標 `chips_facts: ignored_in_replay`）。
- 交易 Agent 文字欄位出現 `chips_background`、`chips_facts` 等鍵名即驗證失敗（同 `dip_background`、`valuation_background`）。改寫轉述程式擋不住，這是既有的已知限制。
- Skill 只加一般性提醒：這是事實不是訊號；當沖占比高可能是擁擠也可能是人氣；行動仍須由本次採納的 claim 支持。不寫特定個股案例。
- 回滾：刪掉 `position_sizing.chips_facts`；收集可用環境變數關閉（比照 `VALUATION_COLLECT=0`）。

### 6.3 fail-closed 行為

| 情況 | 處理 |
| --- | --- |
| 來源未核准、核准時間晚於 cutoff、授權範圍不含該用途 | 整份 `unavailable`，原因碼分開（未核准／範圍不足） |
| 抓取時間晚於 cutoff 或缺時區 | 該批不採用；時間缺時區整份拒絕 |
| 資料日晚於 `snapshot.latest_trade_date` 或晚於抓取日 | 整份拒絕 |
| 資料日早於 `latest_trade_date` | 逐檔 `stale`，附資料日，不補值 |
| 歷史不足 5／20 個交易日 | 對應欄位 null、記 `insufficient_history` |
| 分母為 0、當沖量大於成交量、負值、非整數、欄位名變更、代號重複 | 該檔拒絕；欄位名變更整份拒絕 |
| FinMind 與官方同日融資融券不符 | 該檔拒絕，原因碼 `VALUE_MISMATCH` |
| 有資料的檔數低於交易池一定比例 | 整份 `unavailable`（沿用既有的 80% 覆蓋門檻，不另設數字） |
| 抓取失敗、額度用盡 | 只記錄，不讓決策或 D-Plan 失敗 |

### 6.4 需新增的測試（`unittest.TestCase` 風格）

- cutoff 之後才抓到的批次不進入；抓取時間或核准時間缺時區拒絕。
- 來源未核准、核准晚於 cutoff、授權範圍只有收集與稽核 → `unavailable`，原因碼正確。
- 樣本不足（少於 5 日、少於 20 日）→ 對應欄位 null，不報假值。
- 資料日落後一天 → `stale`，數字不被標成最新交易日。
- 單位換算（張與股）、分母為 0、當沖量大於成交量、同日兩來源不符。
- 原始回應被改、欄位名變更、代號重複、股票誤配（事實掛到別檔）→ 拒絕。
- 今天補抓的歷史不出現在較早 cutoff 的重建裡。
- 沒有閘門的舊 Policy：決策輸入、簡報、提示、Schema、`shared_input_sha256` 逐鍵相同。
- 交易 Agent 理由寫出區塊鍵名 → 驗證失敗。
- 策略反思提案想改 `chips_facts` → 拒絕。

### 6.5 會動到的檔案（真要做時，本次沒有動）

- 新增：`src/etf_agent/data/chips/`（`contracts.py`、`provider.py`、`repository.py`、`tools.py`、`validator.py`、`service.py`）、`cli/chips_collect.py`、`config/chips_sources.json`、`src/etf_agent/decision/chips_facts.py`、對應測試、`docs/source_audit/<日期>_chips_sources.md`。
- 修改：`decision/input_builder.py`、`decision/input_validator.py`、`decision/sizing.py`、`decision/service.py`、`automation/daily_pipeline.py`、交易階段簡報、`evaluation/replay.py`、`strategy_reflection/contracts.py`、`tradeagent/trading/trader/`、`start.sh` 資料階段。
- 文件：`docs/status.md`、`docs/decision-contracts.md`、`docs/glossary.md`、`docs/data_sources_inventory.md`、`README.md`、`AGENTS.md`。

## 7. 風險、不做的理由與建議

### 7.1 事實

- 今天是 2026-11-15（週日），比賽 2026-11-27 結束。11-16 到 11-27 的平日是 10 天（未對照休市表）。
- 專案自己的樣本門檻是每個 horizon 至少 10 個完整決策日（`calibration.MIN_DECISION_DAYS`）。扣掉實作、來源核准與第一次實跑，進決策鏈的天數一定少於 10。
- 逐檔當沖量沒有官方開放 API，只有 FinMind 單一來源，無法做官方同日核對。
- FinMind 免費等級只能逐檔抓：兩個資料集就是每天 300 次請求，會與既有財報、新聞排程搶同一個每小時額度。
- 當沖量文件寫 21:30 更新：21:00 的備案 run 用不到當日資料，只有 07:00 的 run 可能用得到（實際可得時間未實測）。
- 交易 Agent 簡報已經很大，專案最近才做過簡報縮減（`context_slimming_plan`、`cash_stance_brief` v2）。

### 7.2 推論

- 比賽結束前無法知道這份事實有沒有幫助：樣本不到門檻，任何「看起來有用」的結論都只是個案。
- 指標方向不明（第 3.4 節）：同一個高當沖占比，交易 Agent 可能因此賣掉強勢股，也可能因此續抱過熱股。剩下不到兩週時加入一個方向不明的新輸入，改變決策行為的風險大於可能的好處。
- 「過熱還是仍強」目前已有不需新來源的事實可用：近 5／20 日報酬、`gain_alert`／`trail_alert`、成交量相對 20 日均量（`volume_vs_20d` 用既有官方價量就能算）。籌碼資料的邊際資訊多少，未驗證。
- 新收集器若排進 06:00 資料階段，最壞情況是拖慢或耗盡 FinMind 額度，影響 07:00 正式決策前的財報與新聞抓取。這個風險直接落在正式交件路徑上。

### 7.3 建議

1. **比賽結束前不接進決策鏈**（第 1、2 階段都不做）。不新增閘門、不改 Policy、不改任何 Agent 簡報。
2. **最小可行範圍＝只做第 0 階段的一部分，而且是選配**：只收 `TaiwanStockDayTrading` 與 `TaiwanStockMarginPurchaseShortSale` 兩個資料集、150 檔、不可變保存、`available_at` 為實際抓取時間、融資融券與官方 OpenAPI 同日核對、授權範圍登記「只收集與稽核」。排在 21:40 之後的獨立排程（不放進 06:00 與 20:00 的資料階段），失敗只記錄。這樣不碰正式交件路徑，卻能留下「當時真的拿得到什麼」的即時紀錄，比賽後才有東西可以評估。
3. 連第 0 階段也要先有使用者對新來源的核准；若使用者不想在最後兩週增加任何排程，**完全不做也合理**，損失只是少了約 10 天的即時紀錄（歷史數值之後仍可從 FinMind 補抓，但補抓的可得時間是補抓當天，只能當 `replay_reference_only`）。
4. 若使用者只是想讓交易 Agent 多一個「量能是否異常」的事實，優先考慮只用既有官方價量算 `volume_vs_20d`：不需新來源、不需新授權。這仍是新的版本閘門，同樣建議比賽後再做。

### 7.4 需要人決定的事

- 是否核准 FinMind 這兩個逐檔資料集（用途範圍：只收集與稽核，或可作交易 Agent 背景）。
- 當沖成交量值找不到政府開放資料依據，是否比照 VIX 由使用者自行承擔。
- 是否接受單一來源、無官方核對的當沖量。
- 第 0 階段要不要現在做，還是比賽後再說。

## 8. 來源依據

- [FinMind 技術面資料集（含 `TaiwanStockDayTrading`）](https://finmind.github.io/tutor/TaiwanMarket/Technical/)
- [FinMind 籌碼面資料集](https://finmind.github.io/tutor/TaiwanMarket/Chip/)
- [FinMind 免責聲明](https://finmind.github.io/en/Disclaimer/)
- [FinMind 快速開始（請求上限）](https://finmind.github.io/quickstart/)
- [證交所 OpenAPI swagger](https://openapi.twse.com.tw/v1/swagger.json)
- [櫃買中心 OpenAPI swagger](https://www.tpex.org.tw/openapi/swagger.json)
- [證交所當日沖銷交易統計頁（只引用說明文字，未擷取資料）](https://www.twse.com.tw/zh/page/trading/exchange/TWTB4U.html)
- 專案內：[融資水位來源核對](../source_audit/2026-11-04_margin_level_sources.md)、[恐懼貪婪來源核對](../source_audit/2026-10-23_market_fear_greed_sources.md)、[估值來源核對](../source_audit/2026-11-10_valuation_sources.md)、大盤指數事實計劃（見 [名稱對照](../glossary.md)）、[版本閘門狀態](../status.md#版本閘門啟用狀態)
