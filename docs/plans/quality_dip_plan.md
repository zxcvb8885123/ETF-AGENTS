# 優質股被大盤拖累時的進場判斷（`quality_dip`）：設計計畫

狀態（2026-11-10，第七版）：**第三階段（改走換股提名，子閘門 `position_sizing.agent_swap.quality_dip`）與估值 V2（閘門 `position_sizing.valuation_facts`）已實作、閘門預設不存在＝關閉、尚未開啟**（見第 10.4、10.5 節；正式設定未改）；**估值 V3 未實作**，需要人先決定的事列在第 10.6 節。

第六版狀態：**第二階段（`quality_dip_assessment`、`event_direction`）與估值 V1（收集與稽核，2026-11-10 已掛進資料排程）已實作；第二階段並於 2026-11-10 隨第一階段一起由使用者開啟（純紀錄、不影響配置；回滾＝刪掉三個欄位，樣板快照 `config/policy_versions/2026-11-10-before-quality-dip.json`）；第三階段改走換股提名、尚未實作（見第 10 章）；V2、V3 與資料 A／B 未做。**

第五版狀態：**第一階段（事實與標示，閘門 `position_sizing.quality_dip_facts` v1）已實作、閘門預設關閉、尚未開啟**——正式設定沒有這個欄位，是否開啟由人決定；第二、三階段與估值（V1～V3）、資料 A／B 都尚未實作。實作紀錄與待確認項目的結論見第 9 章。以下第四版內容保留原樣（第 1～8 章的「尚未實作」指第四版當時）。

第四版狀態：**只有計畫，尚未實作，沒有任何閘門；開工需使用者另行下令**。設計取捨已由使用者於 2026-11-10 回覆「照建議」定案（第 7.2 節）；這只決定了設計，不是實作或開啟閘門的指令。本檔不改程式、設定與既有文件。第二版依使用者意見改了兩件事：主軸改為短期價格事實、財報為輔；品質條件由「六條全真才合格」改為「沒有新的重大壞事」，年減與缺資料只列為疑慮，不擋進場。第三版再加一條：跌幅超出大盤所能解釋、但查無新壞事的股票另列為「超跌候選」，與「被大盤拖累候選」並列、分開標示（第 2.4 節）；並新增估值資料接入計畫（第 2.6 節：資料拿得到、系統尚未接入）。

文中的案例數字是用 2026-11-10 封存 run 的 `decision_input.json`／`team_inputs.json`／`intent.json`／`debate.json` 以暫存腳本唯讀重算的（腳本不在專案內）；第 2.6 節的估值數字是 2026-11-10 唯讀呼叫公開端點取得的。證據等級一律是 `replay_reference_only`：只代表「同一份輸入會算出什麼事實」，不是對 LLM 判斷的前瞻驗證，也不代表買了會賺。

## 1. 問題定義與非目標

### 1.1 使用者要的是什麼

優質股被大盤拖累而大跌時，系統應該**考慮**進場。重點是分辨「跌是因為大盤」還是「跌是因為公司本身」。

使用者 2026-11-10 的補充（設計前提）：

- 比賽只有約一個月，等財報確認才買會買不到。
- 財報上的年減是**過去**的事，股價可能早已反映（例：2454 營收獲利年減，股價 20 日已跌 23%）。不能只用基本面判斷。
- 財報缺資料是**疑慮，不是擋住**。當天 2610（缺去年同期比較）與 5880（金融股、沒有現金流）仍以 medium 買進。
- 比大盤多跌不一定代表「不該買」，也可能是跌過頭。2308 比大盤能解釋的多跌 18 個百分點，應該另列為候選，而不是只標成「不算大盤因素」。

### 1.2 現況（事實，2026-11-10 run `decision-20261109T230435Z`）

- 交易池 150 檔 20 日等權報酬 −18.43%，加權指數 20 日 −16.35%，regime 為 bear，姿態 `defensive`／`lower`／`diversified`，`turn_assessment` 為 `turn_unconfirmed`。
- 起始持股 27 檔、空名額 3 個、現金 17.96%。交易 Agent 給了 10 檔 buy（7 檔 medium、3 檔 low），3 檔成交（2327、2610、5880），7 檔被 `MAX_POSITIONS` 擋下；成交後 30 檔滿檔，緩衝 15%。
- 2327 是唯一被買進的跌深股，但目標權重只有 1.21%（27 張）：可動用的錢大致只有「17.96% − 15%」加上 2890 減碼的賣款，三檔平分。**現有機制下即使判斷正確，部位也很小。**
- 2308、2317、2382、2454 都是 `no_trade`，理由各是專利調查未定、營業現金流為負、營業利益率下降、獲利年減。
- 系統沒有「相對大盤的超額下跌」這類個股事實：`position_review.context` 的 `relative_to_pool_*` 只算**持股**（`decision/position_review.py`），`exposure_review` 的 beta 也只給持股；未持有的股票只有 `MomentumEngine` 的絕對報酬與均線。
- 系統**尚未接入**任何估值資料（資料本身拿得到，見第 2.6 節）。基本面分析師的資料缺口普遍寫「未附產業比較標準」（`build_analyst_brief` 的 `company_profiles[].comparison_standard` 固定是 `None`）。
- 150 檔中 79 檔的財報狀態是 `degraded`（多數是缺去年同期可比數字，或金融業報表格式算不出比率）。

### 1.3 三個詞的定義（本計畫內的操作性定義）

| 詞 | 本計畫的定義 | 由誰判定 |
| --- | --- | --- |
| 被大盤拖累（主軸） | 個股在評估視窗內的跌幅，扣掉「視窗前估計的 beta × 交易池同期報酬」後的殘差有多大 | 殘差與名次由程式算；殘差算不算「大」是判斷，歸 Agent。程式不設門檻 |
| 超跌（與上一列並列） | 同一個殘差為負：跌幅有一部分不能由大盤解釋。**原因不明**——可能是跌過頭，也可能真有壞事，程式分不出來 | 殘差的正負與名次由程式算；「多跌得夠不夠多、值不值得買」歸 Agent。超跌只描述價格，不等於低於本來價值 |
| 優質（輔助） | **沒有新的重大壞事**：評估視窗內沒有程式可算的營運突變（第 2.2 節）。年減、利益率下降、現金流為負只列為「疑慮事實」；缺資料列為「不知道」 | 每一條由程式算；疑慮與不知道都**不擋進場**，也都**不算通過** |
| 低於本來價值 | **不宣稱**。已接入的資料只能算「相對自身近期高點的回檔」與「距均線」，這是價格位置，不是估值。估值資料接入後也只給「位置」事實，不給便宜或貴的結論 | 程式只給位置；估值接入見第 2.6 節 |

「不擋也不算通過」的意思：程式不會因為疑慮或缺資料否決一檔股票，也不會因為「查無壞事」就替它背書。它只把三種狀態分開列出，買不買仍要有本次多頭 claim 支持。

### 1.4 非目標

- 不預測底部、不預測反彈時點、不設「跌幾 % 就買」或「殘差多少算大盤」的死數字。
- 不放寬 CompetitionGuard、競賽規則（20～30 檔、現金 < 25%、單股 10%）、現金下限（`cash_ratio_range.grid`）、換手上限（`max_turnover_rate`）、回撤下限。
- 不自動下單、不自動上傳；不改寫交易 Agent 的意圖。
- 不讓 Agent 輸出數字；不用模型記憶、搜尋摘要或公司財測冒充估值或分析師共識。
- 不做放空（比賽只能做多）；不做攤平規則（已持有的跌深股仍由 `position_review` 與交易 Agent 判斷）。

## 2. 證據從哪來

全部由確定性程式從 `DecisionInputBundle` 重算，只用 `available_at <= decision_cutoff` 的資料；任何 bar 晚於 `snapshot.latest_trade_date`、時間缺時區即拒絕（沿用 `turn_facts`／`market_index_facts` 的做法）。建議新模組 `src/etf_agent/decision/dip_facts.py`，對外 `build_dip_facts(bundle, gate)` 與 `validate_dip_facts(...)`。

### 2.1 主軸：短期價格事實（「是大盤不是個股」）

| 事實 | 算法 | 現有可重用的程式 | 缺資料時 |
| --- | --- | --- | --- |
| 個股 5／20 日報酬、交易池同期等權報酬、相對交易池差 | 與持股檢視同一公式，擴大到**全交易池** | `position_review._window_return`、`_pool_return` | 根數不足為 `null` |
| 視窗前 beta | 用評估視窗**之前**的 60 個交易日估計（需要 81 根 bar；目前 `DEFAULT_LOOKBACK_BARS` 是 120，夠用）。口徑寫死在版本內 | `exposure.BetaModel` 的公式，但視窗要往前平移，需新增參數或另寫（待確認哪個改法不影響 `exposure_review` 逐位元重建） | 窗內缺任一天即 `null`，不補值 |
| beta 調整後殘差 | 個股 20 日報酬 − 視窗前 beta × 交易池 20 日報酬 | 新增 | beta 或報酬為 `null` 即 `null` |
| 殘差在交易池內的名次（由弱到強）與樣本數 | 對全部有殘差的股票排序 | 新增 | 樣本覆蓋不足時整份 `data_status: unavailable` |
| 相對加權指數的 20 日報酬差 | 個股報酬 − 加權指數同期報酬 | `market_index._series`（讀 `market_fear_greed_input` 的 `TWSE_TAIEX`，須 `license_status=approved`） | 來源未核准或日期對不上：該欄 `null` 並記 `data_gaps`，不影響交易池口徑 |
| 價格位置 | 距近 60／120 日收盤高點的回檔、收盤相對 MA20／MA60、是否創 20 日新低 | `MomentumEngine` 的 `ma20`／`ma60`；新低定義與 `turn_facts` 相同（是否抽成共用函式待確認） | 歷史不足即 `null` |

設計說明：

- **主要口徑用交易池等權，加權指數為輔**。`exposure_review`、`position_review`、`turn_facts` 都以交易池等權為基準，口徑一致；加權指數不含上櫃股且由台積電主導。
- **beta 必須用視窗前的資料估**。若沿用 `BetaModel` 的「最後 60 日」，估計窗包含下殺本身，跌得兇的股票 beta 被灌高、殘差被吃掉。案例中 2382 兩種口徑的殘差符號相反（−2.9／+2.9 個百分點），所以口徑必須固定。（第五版註：−2.9 是用各股 20 日報酬的簡單平均當池報酬算的；改用實作口徑「池等權日報酬連乘」為 −2.6，符號仍相反，結論不變。見第 9.2 節。）
- **程式不設殘差門檻**。只給數字與名次，「−2 個百分點算不算大盤因素」由 Agent 判斷。
- **殘差很負不等於有壞消息**。案例中 3665 殘差 −13.6 個百分點（交易池第 7 弱），但視窗內唯一的 high 事件是 10 月營收年增 72%。殘差只說明「跌幅不能由大盤解釋」，原因仍要由事件與論點判斷。

### 2.2 輔助：有沒有新的重大壞事、疑慮事實、不知道

把原本的「六條符號條件」拆成三個互不混用的清單。

**(a) 新壞事 `new_adverse_facts`**（評估視窗內才新出現、程式可算）：

| 項目 | 算法 | 限制 |
| --- | --- | --- |
| 月營收年增由正轉負 | 視窗內新可得的最新一筆 `monthly_revenue.yoy_pct` < 0，且前一筆 > 0 | 金融業等月營收內容不可比的產業（`growth_signal_quality` 為 `industry_formula_unsupported`）不算突變，改列疑慮。當天有 9 檔出現這個轉負，其中 5880 是金融股 |
| 淨利由盈轉虧 | 視窗內新可得的財報 `net_income` < 0，且前一期可比口徑 > 0 | 沒有可比的前一期即 `unknown` |
| high 重大訊息且事件面負面 | 事件分析師對該股的 `event_outlook` 為 `negative`，且視窗內有 `materiality: high` 的事件 | 見下方「這一項目前很弱」 |

**(b) 疑慮 `concerns`**（既有事實，**不擋進場**）：營收年增為負、淨利年增為負、營業利益率年差為負、營業現金流或自由現金流為負。來源是 `fundamental_metrics` 與 `cash_flow_context`，原樣附 `evidence_ids` 與期別。期別要明寫（例如 2026Q2），讓讀者知道這是哪一季的事。

**(c) 不知道 `unknown`**：指標 `unavailable`（`COMPARABLE_FACT_NOT_REPORTED`、`REQUIRED_FACT_NOT_REPORTED`、`NONPOSITIVE_COMPARISON_BASE`、`UNSUPPORTED_INDUSTRY*`）、現金流文件缺或核對不符、沒有月營收文件。**恆為不知道：不當成通過，也不當成否決。**

另附兩個時間事實：最新月營收與最新財報的 `available_at`，以及 `no_new_fundamental_data_in_window`（最新資料早於視窗起點）。「視窗內沒有新資料」只能說查無新壞事，不能說營運沒變。

**這一項目前很弱（事實，必須先講清楚）**：

- 事件分析師的每則事件只有 `materiality`，沒有方向；方向只有整檔的 `event_outlook`。當天 150 檔的 `event_outlook` 是 neutral 85、positive 58、unknown 7，**沒有任何一檔是 negative**。所以「high 且負面」當天一檔都不會觸發。
- 2308 的美國 337 調查公告（`document:5193`）被評為 `medium`，該檔 `event_outlook` 是 positive（因為同時有 10 月營收年增 30% 的 high 事件）。**程式的新壞事清單抓不到它。**
- 「交易 Agent 採納了事件類空頭論點」也不能當程式判準：2308（337 調查）與 2327（上半年不分派盈餘）當天都採納了 `material_event` 種類的空頭 claim，後者照樣買進。
- 結論：「是不是重大壞事」程式做不到可靠判定，只能列事實；真正的判斷在空頭研究員的 claim 與交易 Agent 的取捨。已決定（第 7.2 節第 4 項）：第二階段讓事件分析師對每則事件多給方向列舉，屆時這一項改為「視窗內有 `materiality: high` 且方向為負面的事件」。這會改事件報告契約與版本，須另設版本閘門、舊報告驗證不變；在那之前這一項照上表的整檔 `event_outlook` 計算。

### 2.3 「低於本來價值」：誠實的範圍

- 已接入的資料**算得出來**的只有第 2.1 節的價格位置（欄位名不得出現 valuation／便宜／低估；120 日是目前 bundle 的歷史上限，**不是** 52 週高點）。
- 本益比、股價淨值比、殖利率：**資料可得、系統尚未接入、尚未登記授權與核對方式**（第 2.6 節）。Snapshot 的損益表 `facts` 只有 `revenue`／`operating_profit`／`net_income`，自己算不出來；[資料來源清單](data_sources_inventory.md)目前沒有這個來源。
- 目標價、分析師共識：沒有合法歷史化的 Provider，依 AGENTS.md 一律 `unavailable`。
- 估值事實核准接入並寫明口徑之前，任何 Skill、簡報與報告都不得寫「低於合理價值」「便宜」「低估」；只能寫「相對近期高點回檔多少、殘差多少」。接入之後可以寫的也只有第 2.6 節定義的位置事實。

### 2.4 兩個候選標示：被大盤拖累、超跌

程式為每檔輸出一個價格型態 `price_pattern`，四個值互斥、可重算：

| `price_pattern` | 條件（只有符號與資料品質） |
| --- | --- |
| `unavailable` | 殘差算不出來（歷史不足、窗內缺行情、交易池覆蓋不足） |
| `no_decline` | 個股或交易池的 20 日報酬不為負 |
| `market_explained` | 兩者皆為負，且殘差 ≥ 0（跌幅不超過 beta × 大盤） |
| `beyond_market` | 兩者皆為負，且殘差 < 0（跌幅超過 beta × 大盤） |

再與第 2.2 節 (a) 的新壞事清單合成候選標示 `candidate`，同樣互斥：

| `candidate` | 條件 | 白話 |
| --- | --- | --- |
| `market_dragged` | `market_explained` 且新壞事清單為空 | 被大盤拖累候選 |
| `oversold` | `beyond_market` 且新壞事清單為空 | 超跌候選，附固定欄位 `cause: unknown` |
| `none` | 其餘（沒跌、資料不可用、或有新壞事） | 不是候選 |

疑慮與不知道都不影響標示（不擋，也不加分）。兩個候選分開標示，不合併成一個「跌深」事實。

**「明顯」怎麼定義而不設門檻**。三種做法，已決定採 A（第 7.2 節第 1 項）：

| 做法 | 內容 | 取捨 |
| --- | --- | --- |
| A（已採用） | 程式只用殘差的正負分類，另附殘差數值、交易池內由弱到強的名次與樣本數；「多跌得明不明顯」由交易 Agent 的列舉判斷（第 3.3 節） | 沒有任何擬合數字，符合「確定性歸程式、不確定性歸 LLM」。代價是標示本身很寬，不能單看標示 |
| B（未採用） | 以交易池相對名次切一刀（例如最弱的四分之一才標超跌） | 會隨市況自動調整、不是報酬率門檻；但「四分之一」仍是一個人設的數字，沒有樣本支持，名次差一名就翻面 |
| C（未採用） | 人在 Policy 設殘差門檻 | 最直觀，但正是本專案在 `rebound_participation` v1 踩過的過擬合做法，不建議 |

採 A 必須知道的事實：**標示很寬**。當天 149 檔有殘差的股票中，`beyond_market` 72 檔、`market_explained` 56 檔、`no_decline` 21 檔；扣掉月營收轉負的，超跌候選約 66 檔、被大盤拖累候選約 55 檔。也就是八成的股票都是某一種候選。候選標示只是分類，**不是訊號**；有鑑別力的是殘差名次（2308 是第 4 弱）與 Agent 的判斷。

邊界上的雜訊：殘差在零附近（例如 2327 的 +0.5、2382 的 +2.9）時，正負只差在 beta 估計誤差。簡報 note 要明講「殘差接近零時兩種標示沒有實質差別，請看數值與名次」。

**超跌只描述價格事實**：它說的是「比 beta × 大盤多跌」，不是「低於本來價值」。程式不能、Agent 也不得據此寫便宜或低估；估值資料接入後這句話仍然成立（第 2.6 節）。

### 2.5 資料面：財報缺口（使用者 2026-11-10 說明，尚未接入）

- **FinMind 去年同期補抓**（`cli/fundamental_repair.py`）：目前只能手動執行，不在每日排程。使用者今天已對全交易池手動補抓一次；補抓後 `degraded` 的檔數會下降多少，要等下一份 Snapshot 才知道（待確認）。是否排進每日流程是待辦，不在本計畫第一階段。
- **Yahoo Finance 作財報缺口的備援來源**：使用者已核准「可以用」，但**現在不是已接入的來源**。目前 Yahoo 只核准用於研究日線與隔夜指數（見[資料來源清單](data_sources_inventory.md)）。實作前須另案定義三件事，缺一不得進正式計算：
  1. 授權狀態怎麼登記（`license_status`、用途範圍、核准日期與核准人）。
  2. 與官方數字的核對方式（比照 `FINMIND_VERIFIED_PRIOR`：當期金額與官方相符才採用；核對不過即不用，不補值）。
  3. `available_at` 規則（第一次抓取時間，不回填；重編或改寫的偵測方式）。
- 去年同期補抓排程、Yahoo 現金流備援與產業比較標準的完整設計見 [fundamental_data_gaps_plan.md](fundamental_data_gaps_plan.md)。
- 這兩件事只會讓「不知道」變少，不改變本計畫的規則：補到的數字如果是年減，仍然只是疑慮。

### 2.6 估值資料接入（資料可得，尚未接入）

**實測（2026-11-10，唯讀）**：

| 來源 | 內容 | 實測結果 |
| --- | --- | --- |
| 證交所 OpenAPI `https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL` | `Date`（民國日期，如 `1151109`）、`Code`、`Name`、`PEratio`、`PBratio`、`DividendYield`；只有當日、無歷史 | 回傳 1087 檔；交易池 100 檔上市股全部找得到，其中 2 檔 `PEratio` 是空字串 |
| 櫃買中心 OpenAPI `https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis` | `Date`、`SecuritiesCompanyCode`、`CompanyName`、`PriceEarningRatio`、`PriceBookRatio`、`YieldRatio`、`DividendPerShare`；只有當日 | 回傳 894 檔；交易池 50 檔上櫃股找到 49 檔（缺 3718.TWO），1 檔本益比無值。**待確認**：這是否就是與上市對應的正式端點、欄位口徑是否相同、授權條款 |
| FinMind `TaiwanStockPER` | `date`、`stock_id`、`PER`、`PBR`、`dividend_yield`；有歷史 | 2308 自 2019-01-02 起 1905 筆；2026-11-09 為 34.83／9.47／1.0，與證交所同日 34.83／9.47／1.00 一致 |

櫃買端點的確認方式：對照櫃買中心 OpenAPI 的官方說明文件確認資料集名稱與授權；連續數個交易日抓取，逐檔與 FinMind 同日數字比對；確認 3718.TWO 缺漏的原因。確認前上櫃股的估值一律 `unavailable`。

**(1) 來源與角色**（沿用[基本面缺口補抓](fundamental_data_repair.md)的「官方當標準答案、FinMind 補歷史、對得上才收」）：

- 官方當日資料是核對基準；FinMind 提供歷史序列。
- 每個抓取日，逐檔比對 FinMind 同一資料日的 PER／PBR／殖利率與官方數字（以 Decimal 比較，兩位小數）。完全相符才接受該檔的 FinMind 序列。
- 不符、任一邊缺該檔、資料日對不上、欄位名變更：該檔當日估值拒絕並回報缺口，不取平均、不擇一、不填假值。
- 官方端點沒有歷史，所以 FinMind 在官方開始收集之前的歷史**無法逐日核對**，只能標為「以當日核對推定口徑相同的未核對歷史」。證交所是否有可指定日期的官方查詢可用來回補核對，**待確認**。

**(2) 契約與稽核**：

- 每筆保存：來源、抓取時間、內容時間（資料日，民國日期須轉換並驗證）、`available_at`、授權狀態、映射版本、原始回應識別與內容雜湊。
- `available_at` 規則（依專案既有的 FinMind 做法）：**一律是實際抓取時間**，來源沒給發布時間就留空，不得回填到過去。
  - 官方當日資料：從開始收集那天起，逐日累積成真正的時間點歷史。
  - FinMind 歷史：資料日在過去，但 `available_at` 是抓取當下。所以它可以用在抓取之後的決策（「截止當下已知的歷史序列」），**不能**拿來重跑抓取日之前的 cutoff；回放遇到這種情況一律 `unavailable`。
  - FinMind 之後若改寫歷史數字，每次抓取另存版本，不覆蓋舊版。
- 授權登記：兩個來源的這個資料集都要由使用者核准並登記 `license_status`（用途範圍、核准日期）。只有 `approved` 才進正式計算；本計畫不替使用者認定已核准。
- 例外處理：
  - PER 為空、0 或負（虧損或來源未提供）：記 `null` 與原因碼，不當成 0，也不當成「很便宜」；該檔的 PER 位置不可用，PBR 可獨立使用。
  - 金融股：PBR 與 PER 的意義與一般業不同，不與其他產業並列比較；只允許與自身歷史比。
  - 盈餘更新造成的跳動：PER 會在新財報納入時跳變（口徑是近四季或其他，**待確認**），位置事實要附資料日，Skill 提醒「PER 下降可能來自盈餘上修，不一定是股價下跌」。
  - 殖利率的股利基準（哪一年度、是否含股票股利）**待確認**，確認前不進簡報。

**(3) 能回答什麼、不能回答什麼**：

| 能算的確定性事實 | 限制 |
| --- | --- |
| 目前 PER／PBR 在該股自身歷史區間的位置（百分位、區間最小／中位／最大、樣本數、起訖日） | 已決定並列三個視窗：近 250 日、近 750 日、全部可得歷史；寫死在版本內（見下方 2308 的例子） |
| 目前 PER／PBR 在交易池當日橫斷面的名次 | 跨產業直接比沒有意義，只能當描述 |
| 相對同產業中位數 | **目前做不到**：Snapshot 只有上市股的 `company_profile.industry_code`（100 檔），沒有名稱對應、沒有上櫃股；交易池內同產業樣本常只有個位數。需要另接產業分類來源才談得上 |

不能回答的：單看 PER／PBR 數值不能說便宜或貴；位置低也不等於低於本來價值（盈餘可能正在下修）。程式不設「低於第幾百分位算便宜」的門檻，Agent 不輸出數字。「超跌」不得寫成「低於本來價值」；估值事實核准接入後，允許的寫法只有「PER 位於自身近 N 日區間的第幾百分位」這類帶口徑的描述。

**案例：2308 的位置隨視窗完全翻轉**（FinMind `TaiwanStockPER` 唯讀實算，`replay_reference_only`；2026-11-09 PER 34.83、PBR 9.47）：

| 視窗 | PER 區間（最小／中位／最大） | PER 百分位 | PBR 區間 | PBR 百分位 |
| --- | --- | --- | --- | --- |
| 2019-01-02 起（1905 日） | 12.47／25.17／93.56 | 83.7% | 2.06／4.32／21.85 | 85.4% |
| 近 750 日 | 20.69／30.36／93.56 | 59.2% | 3.17／4.99／21.85 | 62.9% |
| 近 250 日 | 31.68／59.99／93.56 | 1.2% | 8.61／14.68／21.85 | 1.2% |
| 近 60 日 | 31.68／56.17／65.25 | 5.0% | 8.61／15.61／18.13 | 5.0% |

同一天、同一個數字：和近一年比在最低的 1%，和 2019 年以來比在最高的 16%。這正是不能讓程式或 Agent 下「便宜」結論的原因，也是視窗必須寫死並並列的原因。另一個事實：2308 的 PER 在 10-07 是 63.34，一個月降到 34.83，降幅大於股價跌幅，其中有盈餘更新的成分（口徑待確認）。

**(4) 與計畫其他部分的關係**：

- 進簡報的方式：估值位置是第三個獨立事實 `valuation_position`，**不併入** `price_pattern` 或 `candidate`。「超跌候選且 PER 位於近一年低位」仍然是兩個並列的事實，程式不合成結論。
- 可否引用：
  - 先當背景（`citable=false`，比照 `market_index_facts`）給基本面分析師與交易 Agent。現行 `typed_evidence_v1` 下，未登記的文件種類本來就不能被任何 claim 引用，所以先接資料不會動到既有驗證。
  - 之後若要可引用，建議對映到既有的 `price` 種類，不新增種類。理由：PER／PBR 是「股價除以財報數字」的每日價格衍生值；新增種類要改 `CLAIM_KINDS`、全部 schema 與 Skill。對映變更屬於證據政策，須出新版本（例如 `typed_evidence_v2`），舊 bundle 不變。歸在 `price` 也讓它自動適用「單獨價格論點不構成買進依據」的規則。
- 第三階段條件：**不建議**把估值位置寫成程式條件（那就是門檻）。它只透過交易 Agent 的判斷起作用。

**(5) 排序與進入條件**：

| 步驟 | 內容 | 進入條件 |
| --- | --- | --- |
| V1 收集與稽核 | 每日抓官方與 FinMind，逐檔核對，存不可變原始回應與核對結果；**不進決策、不進簡報**。已決定先做上市股，上櫃端點確認後再加入 | 使用者已同意核准證交所 `BWIBBU_ALL` 與 FinMind `TaiwanStockPER` 用於 V1；`license_status` 的實際登記屬實作的一部分，尚未做 |
| V2 位置事實（背景） | 程式算 `valuation_position`，以 `citable=false` 進基本面分析師與交易 Agent 簡報；閘門 `position_sizing.valuation_facts`，預設關閉 | V1 連續核對相符達人指定的天數；口徑待確認項目（EPS 基準、殖利率基準）已釐清（視窗已定案） |
| V3 可引用 | 新證據政策版本，對映 `price` | V2 開啟後有即時紀錄；人決定 |

V1 與第一階段（價格事實）互不相依，可以並行。官方端點沒有歷史，所以 V1 越早開始，可核對的時間點資料越多；但開始收集仍需使用者下令開工。

## 3. Agent 邊界

### 3.1 邊界（第一階段，已決定）

| 角色 | 拿到什麼 | 能不能引用 | 輸出有沒有變 |
| --- | --- | --- | --- |
| 技術分析師 | 每檔多一個 `market_attribution` 區塊與 `price_position`（第 2.1 節），附該股既有的行情 `evidence_id` | 可以：finding 照舊引用該股行情證據，`finding_kind: price` | 不變 |
| 基本面分析師 | 每檔多一個 `fundamental_flags` 區塊（第 2.2 節的新壞事／疑慮／不知道三個清單與時間事實），每條附對應的 `evidence_ids` | 可以：`finding_kind: financial`／`revenue` | 不變 |
| 多頭／空頭研究員 | 不直接拿事實區塊；照舊只讀分析師報告 | 透過分析師 finding 與該股證據 | 不變 |
| 交易 Agent | 整份 `dip_background`（含 `price_pattern` 與 `candidate`；`citable=false`、沒有 `evidence_id`） | **不可引用**；文字欄位出現其識別碼即拒絕（比照 `portfolio_slots`） | 不變 |
| 風險 Agent | 不給（個股事實與整體現金姿態無關；它已有 `turn_facts`、`market_index_facts`） | — | 不變 |

### 3.2 為什麼這樣切

- **不新增證據種類**。`CLAIM_KINDS` 與 `CLAIM_KIND_EVIDENCE`（`decision/input_context.py`）屬於 bundle 的 `typed_evidence_v1`；加一種就得出 `typed_evidence_v2`。殘差本質上是「該股行情加交易池行情」算出的價格事實，主要依據是該股的行情證據（種類 `price`），符合「至少一個證據屬於該股票、市場層級只能補充」的現行規則。
- **多空隔離完全不動**。`shared_input_sha256`（`decision/stance.py`）已涵蓋分析師報告的 `content_sha256`，事實經分析師 finding 進入共同輸入，兩方讀到的內容相同、`peer_packet_ids` 仍為空。
- **交易 Agent 的契約不動**。它仍須把每個 claim 剛好採納或否決一次、不得新增事實；行動仍須由本次 claim 支持，多頭沒有論點就不能買。
- **給 Agent 的取捨規則（寫進 Skill，程式不驗文字）**：
  - 「跌幅可由大盤解釋」是可以用來回應空頭價格論點的事實，但單獨一個價格 claim 不構成買進依據，要搭配該股自己的營運或事件論點。
  - 疑慮是過去某一期的事實：要寫明是哪一期，並說明價格是否已經反映；不得只因有疑慮就否決，也不得當作沒看到。
  - 缺資料只代表不知道：不是利空，也不是利多（現行 `trader` Skill 已有這句，延伸到研究員）。
  - 不得寫便宜／低估。
- **超跌候選的專屬規則**：
  - 買不買仍由交易 Agent 單獨決定。候選標示不是買進證據：不得因為「多跌」就買，也不得因為「多跌」就否決。
  - 多跌的原因程式分不出來，理由裡要如實寫「原因不明」；若要主張是跌過頭，必須指出該股自己的營運或事件論點，並逐一處理空頭提出的事件論點。
  - 程式不會因為候選標示自動換股、自動加權或調整信心；`selection_ranking` 的排序鍵也不讀它。

### 3.3 第二階段：交易 Agent 的列舉（紀錄用）

比照 `turn_assessment`：交易 Agent 對每個 buy／add 多填列舉 `dip_assessment` ∈ `not_a_dip`／`market_driven`／`oversold_cause_unclear`／`stock_specific`／`undetermined`。`oversold_cause_unclear` 的意思是「比大盤多跌、原因不明、我仍判斷值得買」；`stock_specific` 是「多跌有個股原因」。驗證只核必填、值域，以及一條 fail-closed：該股價格事實 `data_status` 為 unavailable 時只能填 `undetermined`。列舉與意圖、信心**不設對應**，不影響配置；目的是累積「Agent 怎麼判、之後怎麼走」的即時紀錄。

### 3.4 替代方案（未採用）

把事實區塊直接放進多空研究員的簡報。使用者已決定不採用（第 7.2 節第 7 項）。代價是簡報內容取決於 Policy 閘門，而 `shared_input_sha256` 不含 Policy，必須在閘門存在時把事實雜湊併入共同輸入雜湊（舊 run 公式不變），辯論驗證與 Finalizer 都要跟著改。

## 4. 配置面

### 4.1 版本閘門（全部預設不存在＝關閉；只由人設定、策略反思不得改）

| 階段 | 欄位 | 內容 | 影響配置 |
| --- | --- | --- | --- |
| 1 | `position_sizing.quality_dip_facts` | `{"version": "v1"}`；視窗長度是版本內的程式常數，不是旋鈕 | 否（紀錄） |
| 2 | `position_sizing.quality_dip_assessment` | `{"version": "v1"}`；前提 `quality_dip_facts`、`typed_evidence_v1` | 否（紀錄） |
| 3 | `position_sizing.quality_dip_swap` | `{"version": "v1", "max_swaps_per_run", "max_entry_weight", "min_new_conviction", "min_holding_days", "min_turn_assessment"}`，全部必填、程式不給預設；`min_turn_assessment` 只允許 `turn_unconfirmed` 或 `turn_confirmed`（已決定必須有轉向跡象）；選填 `oversold` 區塊（沒寫＝超跌候選不走換股，見第 4.2 節） | 是 |

三個欄位都要登記到 `decision/sizing.py` 的 `GATE_PREREQUISITES` 與 `strategy_reflection/contracts.py` 的 `FORBIDDEN_PREFIXES`。沒有欄位的舊 Policy／舊 run：簡報逐鍵相同、`team_inputs` 與雜湊逐位元不變。回放比照既有做法略過（notes 標 `quality_dip_swap: ignored_in_replay`）。

`team_inputs`：事實要另存並由 Finalizer 整份重算（比照 `fundamental_metrics`）。目前版本鏈是線性超集（2.8 ⊃ 2.7 ⊃ …，見 `decision/finalization.py`），新增「2.9 = 2.8 加 `dip_facts`」在正式設定下可行（目前就是 2.8），但會讓它依賴 `rebound_participation`。是否改成「與 Policy 閘門雙向檢查的選配鍵」**待實作時確認**。（第五版：已確認改為選配鍵，見第 9.1 節 (b)。）

### 4.2 第三階段的行為（設計已決定；尚未實作，是否開啟另由人決定）

只做一件事：**滿檔時，讓交易 Agent 判為「被大盤拖累」的買進候選可以用換股取得名額**。它是 `conviction_gap_swap` 的延伸（現行只允許 high 新股換 low 持股），不新增現金、不新增檔數。

換入者（全部成立）：

1. 交易 Agent 本次標 `buy`、信心 ≥ `min_new_conviction`、被 `MAX_POSITIONS` 擋下。
2. 價格事實 `data_status` 為 available，且個股與交易池 20 日報酬皆為負（符號條件：確實是下跌市場裡的下跌股）。**程式不看殘差大小。**
3. 交易 Agent 的 `dip_assessment` 為 `market_driven`。
4. `new_adverse_facts` 為空。**疑慮與不知道都不擋。**
5. 交易 Agent 本次至少採納一個該股、種類不是 `price` 的多頭 claim（程式可驗 `claim_kind` 與 `adopted_claim_ids`）。這一條是把「缺資料不算通過」落實成「要有該股自己的營運或事件依據」，不是否決缺資料的股票。
6. 風險 Agent 的 `turn_assessment` 不低於 `min_turn_assessment`，而且至少是 `turn_unconfirmed`；`no_turn_evidence` 時整個閘門不動作。
7. 可交易、不在 `reentry_cooldown` 名單。

**超跌候選在第三階段的處理**（已決定）：`quality_dip_swap` v1 **預設不含超跌候選**（`candidate` 為 `oversold` 的買進不走這條換股，照現行規則排隊）。只有人在閘門另外明寫 `oversold: {"max_swaps_per_run", "max_entry_weight"}` 才納入，而且除了上面第 1、5、6、7 項，還要全部成立：

- `dip_assessment` 為 `oversold_cause_unclear`（交易 Agent 明確承認原因不明仍要買）。
- 新壞事清單為空。
- 交易 Agent 採納的該股多頭 claim 至少一個不是 `price` 種類（與第 5 項相同，這裡是必要條件而不是補強）。
- `turn_assessment` 至少 `turn_unconfirmed`（與第 6 項相同）。
- 檔數與單檔權重用 `oversold` 自己的上限，且不得高於被大盤拖累候選的上限；兩類合計仍受本閘門 `max_swaps_per_run` 封頂。
- 現金下限、換手上限、買進不超過賣款等規則完全相同，不另放寬。

任何情況下，換股都只發生在「交易 Agent 自己標了 buy」之後；候選標示本身不會產生買單、賣單或權重。

換出者：與信心差換股相同——交易 Agent 標 `hold` 且信心 `low`、持有天數 ≥ `min_holding_days`、可交易。標 add／trim／exit 與信心 medium／high 的持股永遠不換。賣單由程式標 `exit`、旗標 `RISK_QUALITY_DIP_SWAP`（不觸發留 `…_SKIPPED:<原因>`），不改寫交易 Agent 意圖，換入者拿不到買單就捨棄該對，**絕不只賣不買**。

### 4.3 與既有機制的互動

| 既有機制 | 互動 |
| --- | --- |
| `MAX_POSITIONS`、`positions_by_breadth` | 不變。只換股，不增加檔數 |
| 現金下限（`cash_ratio_range.grid`、`cash_buffer_floor`） | **不放寬**。買進金額不得超過該對換出者的賣款；不動用原有現金 |
| `max_turnover_rate`、`turnover_fit` | **不放寬**，仍是最終上限；被砍到沒有買單時整對取消 |
| `held_weight_budget` | 不變。已持有的跌深股要加碼仍受信心份額限制，本閘門不處理 add |
| `rotation`／`forced_swap` | 優先序：`forced_swap` 與交易 Agent 的 exit／trim → 曝險換股（含反彈參與放寬後）→ 信心差換股 → 本閘門。總檔數受 `rotation.max_swaps_per_run` 封頂，本閘門另有自己的 `max_swaps_per_run` |
| `exposure_rotation`／`rebound_participation` | 不互斥但共用名額與換出者池，排在它們之後；同一檔持股只換出一次。矛盾情境（風險 Agent 判沒有轉向跡象，本閘門卻要換入高 beta 股）由 `min_turn_assessment` 擋 |
| `drawdown_derisk`、`drawdown_control.stance_floor` | 減碼日與回撤強制防守時整個略過（與曝險換股相同） |
| `sector_fit`、`stock_limit_headroom`、`buy_gap_reserve`、Active Share | 全部照舊，是後段上限 |

### 4.4 防守姿態下放寬多少

本閘門在防守姿態下的放寬**僅限**：每次最多 `max_swaps_per_run` 檔（建議 1）、每檔買進權重不超過 `max_entry_weight`（建議不高於 low 信心份額，數值由人定）、買進金額不超過換出賣款。現金下限、換手上限、檔數、單股上限一律不動。

已知缺口：沒有「跌深部位合計上限」。要做就得跨 run 記住哪些持股是經本閘門買進的，等於新增跨日狀態；先不做，靠每次檔數上限與 `min_holding_days` 限速。

## 5. 風險與擋法

### 5.1 這次放寬（財報由門檻改為疑慮）新增的風險

| 風險 | 具體情境 | 擋法 |
| --- | --- | --- |
| 接刀 | 年減不再擋進場後，基本面正在走弱的高 beta 股也能被買；大盤續跌時它跌更多，而且沒有基本面墊底 | 現金下限與換手不放寬；第三階段只換股、買進不超過賣款、每次檔數與單檔權重有上限、需 `min_turn_assessment`；減碼日與回撤強制防守略過；買進後仍受 `position_review` 的 `loss_alert` 0.15 檢視 |
| 把壞消息誤當大盤因素 | 程式的新壞事清單很弱（第 2.2 節）：2308 的 337 調查抓不到；尚未反映在價格的壞消息，殘差也看不出來 | 殘差是第一道事實（2308 為 −18 個百分點）；空頭研究員的事件 claim 與交易 Agent 的逐一採納／否決照舊；`dip_assessment` 必須明確填 `market_driven` 才有配置效果；第三階段要求至少一個非價格的多頭 claim |
| 「價格已反映」無法驗證 | 這是判斷，不是事實；估值資料尚未接入，接入後也只有位置事實、沒有「合理價」 | 程式不替它背書：只列期別與價格位置；Skill 禁寫便宜／低估；`dip_assessment` 累積即時紀錄後才評估 Agent 這類判斷的準確度 |
| 缺資料的股票大量進場 | 79 檔 `degraded` 不再被擋；其中可能有真的變差但看不到的 | 缺資料不算通過：第三階段的額外名額要求非價格多頭 claim；一般買進路徑照現行規則（當天 2610、5880 就是這樣買的），本計畫不改 |
| 單一鑰匙 | 放寬後，配置效果主要取決於交易 Agent 一個列舉（與 `rebound_participation` v2 同型的已知缺口） | 影響範圍由程式硬上限封住（每次 1 檔、買進不超過賣款）；外部文字照舊包成不受信任資料 |

### 5.2 超跌候選的專屬風險：接刀

這一條的最大風險就是接刀。2308 是現成的例子：當天殘差 −18 個百分點、查無程式可算的新壞事，會被標為超跌候選；但視窗內有美國 337 調查，結果與影響範圍未定，而程式抓不到它（第 2.2 節）。3665 也是超跌候選（殘差 −13.6 個百分點），它的事件面卻是營收加速。**同一個標示底下，兩種情況程式分不出來。**

| 擋法 | 階段 | 內容 |
| --- | --- | --- |
| 只列候選、不改配置 | 一、二 | 標示只進簡報與背景；名額、權重、現金、排序都不讀它 |
| 原因如實標示 | 一 | `cause: unknown` 是固定欄位；Skill 要求理由寫「原因不明」，不得寫成跌過頭或便宜 |
| 空頭論點照舊 | 一 | 空頭研究員的事件 claim 不受標示影響，交易 Agent 仍須逐一採納或否決 |
| 預設排除 | 三 | `quality_dip_swap` 不含超跌候選，除非人另外明寫 `oversold` 區塊 |
| 非價格多頭依據 | 三 | 交易 Agent 採納的多頭 claim 至少一個不是 `price` 種類 |
| 轉向跡象 | 三 | `turn_assessment` 至少 `turn_unconfirmed`（已決定，兩類候選都適用） |
| 硬上限 | 三 | 專屬的每次檔數與單檔權重上限，不高於被大盤拖累候選；買進不超過賣款 |
| 不放寬 | 全部 | 現金下限、換手上限、檔數、單股上限、Guard、回撤下限一律不動；減碼日與回撤強制防守略過 |
| 買進後 | 全部 | `position_review` 的 `loss_alert` 0.15 與相對交易池報酬照舊檢視 |

擋不住的部分（如實列出）：若交易 Agent 判斷錯誤、而多頭又有非價格論點，第三階段仍會換入一檔正在出事的股票；損失上限是單檔權重上限乘以之後的跌幅。

### 5.3 原有風險

| 風險 | 擋法 |
| --- | --- |
| 資料落後：月營收最多落後約 40 天，季報更久 | 事實區塊帶 `available_at` 與 `no_new_fundamental_data_in_window`；Skill 明講「沒有新資料只能說查無新壞事」 |
| 把回檔或超跌當估值 | 欄位命名只用 `price_position`、`price_pattern`；估值事實核准接入前禁寫便宜／低估，接入後也只能寫帶口徑的位置 |
| 估值位置被挑視窗 | 視窗寫死在版本內並並列多個視窗；Skill 要求引用位置時寫明視窗 |
| beta 估計不穩（60 日視窗、極端行情） | 口徑寫死在版本內；窗內缺一天即 `null`；交易池報酬變異為 0 或覆蓋不足時整份 unavailable |
| 與 `rebound_participation` 重疊 | 明訂優先序與共用上限；同一檔持股只換出一次 |
| 過擬合 | 程式不設殘差與跌幅門檻；所有回放標 `replay_reference_only` |

## 6. 驗證與測試

測試維持 `unittest.TestCase` 風格，建議檔案 `tests/decision/test_dip_facts.py`、`test_quality_dip_swap.py`。

- **重算比對**：`validate_dip_facts` 以同一 bundle 與閘門整份重算，任一欄被改即拒絕；Finalizer 與 `DecisionResultValidator` 都要過。第三階段另重算換股配對、旗標與 `effective_policy`。
- **時間**：任何 bar 晚於 `snapshot.latest_trade_date` 拒絕；`available_at` 晚於 cutoff 的文件不計入；時間缺時區拒絕（`parse_aware_time`）；加權指數最新日不等於最近交易日時該欄 `null` 並記 `data_gaps`。
- **候選標示互斥且可重算**：每檔恰有一個 `price_pattern` 與一個 `candidate`；殘差為 0 歸 `market_explained`；有新壞事時 `candidate` 必為 `none`；疑慮與不知道不改變標示；`oversold` 必帶 `cause: unknown`。
- **超跌不自動動作**：只有候選標示、交易 Agent 沒標 buy 時，訂單、權重、排序與沒有閘門時逐位元相同；第三階段沒有 `oversold` 區塊時超跌候選不換股。
- **三個清單不混用**：年減只進 `concerns`、不進 `new_adverse_facts`；指標 `unavailable` 只進 `unknown`，不進另外兩個；金融業的月營收轉負進 `concerns`。
- **缺資料既不通過也不否決**：財報 `degraded` 的股票，第三階段在其他條件成立且有非價格多頭 claim 時可以換入；沒有非價格多頭 claim 時不換入，旗標寫明原因；兩種情況都不得改寫交易 Agent 意圖。
- **缺資料降級**：歷史不足 81 根 → beta 與殘差 `null`、`dip_assessment` 只能 `undetermined`；加權指數來源未核准 → 指數欄 `null`。
- **誤配與版本**：股票誤配的文件、重複 `evidence_id`、閘門版本不合法、前提閘門缺少一律拒絕。
- **邊界**：多空兩方簡報除 role 與 envelope 外逐鍵相同；交易 Agent 文字欄位出現 `dip_background` 識別碼拒絕。
- **舊 run 不變**：沒有閘門的 Policy，分析師與交易 Agent 簡報逐鍵相同；封存 run（含本案例）重建雜湊不變；`exposure_review` 不因 `BetaModel` 改動而變。
- **第三階段 fail-closed**：不只賣不買、減碼日略過、`stance_floor` 防守略過、買進金額不超過賣款、不突破 `max_turnover_rate` 與現金下限。

### 6.1 案例：2026-11-10 這份決策在新定義下各得到什麼事實

評估視窗 2026-10-07～11-09（20 個交易日）；交易池 20 日 −18.43%、加權指數 20 日 −16.35%。`replay_reference_only`。「新壞事」欄依第 2.2 節 (a) 的三項；「候選標示」依第 2.4 節做法 A。

| 代號 | 20 日報酬 | 相對交易池 | 視窗前 beta | 殘差（由弱到強名次／149） | 新壞事 | 疑慮 | 不知道 | 候選標示 | 實際意圖 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2327 | −36.48% | −18.04 pp | 1.94 | +0.5 pp（第 79） | 無 | 無 | 無 | 被大盤拖累 | `buy`／medium |
| 2308 | −41.71% | −23.27 pp | 1.24 | −18.0 pp（第 4） | 無（程式抓不到 337 調查） | 無 | 營收／淨利年增、利益率年差（缺去年同期） | **超跌（原因不明）** | `no_trade` |
| 2454 | −22.96% | −4.52 pp | 1.46 | +5.0 pp（第 105） | 無（9 月營收年增 +2.0%，未轉負） | 2026Q2 營收年增 −0.8%、淨利年增 −15.0%、利益率年差 −4.39 pp、自由現金流為負 | 無 | 被大盤拖累 | `no_trade` |
| 2317 | −5.16% | +13.28 pp | 0.80 | +10.1 pp（第 133） | 無 | 2026Q2 營業現金流與自由現金流為負 | 無 | 被大盤拖累（跌得比交易池少） | `no_trade` |
| 2382 | −11.49% | +6.94 pp | 0.75 | +2.9 pp（第 93） | 無 | 2026Q2 利益率年差 −1.44 pp | 無 | 被大盤拖累（跌得比交易池少） | `no_trade` |

逐檔說明（事實與推論分開）：

- **2327**：被大盤拖累候選。跌 36% 幾乎全部可由「高 beta × 大盤」解釋，沒有新壞事、沒有疑慮，與當天的買進一致。
- **2308**：第三版的標示由「不算大盤因素」改為**超跌候選、原因不明**。事實：殘差 −18.0 個百分點，交易池第 4 弱；程式可算的新壞事清單為空；財報缺去年同期，列為不知道、不擋。同時存在的事實：視窗內有美國 337 調查公告，空頭研究員據此提出事件類 claim（`bear-2308-2`），交易 Agent 採納。程式分不出這 18 個百分點是跌過頭還是調查的合理反映；標示只是把它列進候選，**買不買仍是交易 Agent 的判斷**，當天它的判斷是等調查結果。估值位置（第 2.6 節）對這個判斷沒有單向的幫助：近一年在最低 1%，2019 年以來在最高 16%。
- **2454**：被大盤拖累候選（殘差 +5.0 個百分點、沒有新壞事）；年減三項與自由現金流為負是 2026Q2 的疑慮，不擋。但當天多頭研究員對它沒有任何論點（`strength: none`），交易 Agent 沒有可採納的買進依據。新事實出現後多頭會不會提出論點，是 LLM 的行為，無法由重算得知。
- **2317、2382**：依符號規則都落在被大盤拖累候選，但兩檔都跌得比交易池少（相對交易池 +13.3、+6.9 個百分點），稱不上跌深；這正是「候選標示很寬、要連同數值一起看」的例子。現金流為負、利益率下降都只是疑慮。
- 旁證：當天被名額擋下的 3665 也是超跌候選（殘差 −13.6 個百分點，第 7 弱），它的事件面是營收加速。2308 與 3665 標示相同、背景相反。

當天會不會多出買單（誠實版）：

- **第一、二階段：不會。** 這兩階段不改名額與現金，候選標示也不產生任何訂單。當天 3 個空名額已用完，另有 7 檔買進被 `MAX_POSITIONS` 擋下。即使交易 Agent 因新事實把 2308 或 2454 改成買進，它們也只是加入排隊；最多改變「是哪三檔拿到名額」，總筆數不變。
- **第三階段若開啟（每次 1 檔、未另開 `oversold`）：最多多出一對換股，而且不會是 2308**（超跌候選預設不走換股）。當天有 7 檔持股被標 `hold`／low 可當換出者；被擋下的 medium 買進中屬於被大盤拖累候選的是 1815、6770。實際會不會換取決於尚不存在的 `dip_assessment`，以及換手與單股上限。
- **第三階段另開 `oversold`**：2308 要換入，還需要交易 Agent 自己標 buy、填 `oversold_cause_unclear`、採納非價格多頭 claim。當天交易 Agent 給的是 `no_trade`，所以仍然不會有 2308 的買單。
- 部位大小不會因此變大：買進金額不超過換出賣款。

## 7. 分階段開發順序

| 階段 | 範圍 | 完成條件 | 文件同步 |
| --- | --- | --- | --- |
| 1 事實（紀錄） | `decision/dip_facts.py`（價格事實、三個清單）、技術／基本面分析師簡報、交易 Agent `dip_background`、`team_inputs` 另存與 Finalizer 重算、閘門登記、測試 | 全套測試通過；用本案例重算出第 6.1 節的表；人決定是否開啟 | `README.md`、`AGENTS.md` 閘門清單、`docs/status.md` 閘門表、`docs/glossary.md`、`docs/decision-contracts.md`（新小節與 `team_inputs` 版本表）、`docs/architecture.md`、`docs/README.md` 索引、`src/etf_agent/decision/README.md`；Skill：`technical-analyst`、`fundamental-analyst`、`bull-researcher`、`bear-researcher`、`trader`（`references/data-rules.md`） |
| 2 列舉（紀錄，**已實作、未開啟**，見第 10 章） | 交易 Agent `dip_assessment`；事件分析師的每則事件方向列舉（事件報告新版本、另設版本閘門）；schema（`automation/agent_schemas.py`）、驗證 | 第 1 階段開啟後驗證失敗沒有明顯增加 | `docs/status.md`、`docs/glossary.md`、`docs/decision-contracts.md`、`docs/architecture.md`；Skill：`trader`、事件分析師 |
| 3 換股（影響配置，**第六版改走換股提名**，見第 10.3 節；以下範圍欄為第四版原文） | `quality_dip_swap`、`decision/allocation.py` 配對、旗標、回放略過 | 第 1、2 階段開啟並累積數個決策日的即時紀錄，人看過後決定（已決定不與前兩階段一起開；觀察天數由人定） | 全部上述文件，另加 `docs/decision-details.md`、`docs/operations.md`（回滾方式）；Skill：`trader`、`portfolio-risk-review` |
| 資料 A（待辦） | FinMind 去年同期補抓排進每日流程 | 補抓失敗不得讓當日決策失敗；補抓結果仍須與官方金額核對 | `docs/fundamental_data_repair.md`、`docs/operations.md`、`docs/status.md` |
| 資料 B（待辦，另案） | Yahoo Finance 財報備援：已決定排在資料 A 之後，先只做唯讀比對（見 [fundamental_data_gaps_plan.md](fundamental_data_gaps_plan.md)） | 資料 A 上線後看缺口剩多少；未定案前不得進正式計算 | `docs/data_sources_inventory.md`、`docs/source_audit/`、`docs/status.md` |
| 估值 V1（**已實作、已掛進資料排程**，見第 10.2 節） | 官方與 FinMind 估值的取得、逐檔核對、不可變保存；只收集與稽核，不進決策；先做上市股 | 使用者下令開工；授權登記隨實作完成（第 2.6 節） | `docs/data_sources_inventory.md`、`docs/source_audit/`、`docs/status.md` |
| 估值 V2（待辦） | `valuation_position` 以背景進簡報，閘門 `valuation_facts` 預設關閉 | V1 連續核對相符達人指定天數；口徑釐清（視窗已定案） | 同第 1 階段的文件與 Skill：`fundamental-analyst`、`trader` |
| 估值 V3（未定） | 新證據政策版本，估值可引用（對映 `price`） | V2 有即時紀錄後由人決定 | `docs/decision-contracts.md`、`AGENTS.md`、各研究與交易 Skill |

資料 A、B、估值 V1 與第 1 階段互不相依，可以並行；資料 A、B 只影響「不知道」的多寡。每階段都要保留切換前的 Policy 樣板於 `config/policy_versions/`，回滾＝刪除該欄位。

### 7.1 第一階段明確不含

名額、現金、權重的任何改動；新的 Agent 輸出欄位；新的資料來源；估值事實（估值只先做 V1 的收集與稽核，不進簡報）。

### 7.2 已決定（使用者 2026-11-10 回覆「照建議」）

以下九項都採用原先標示的建議選項。**這些是設計取捨的決定，不是開工指令：全部尚未實作，開工需使用者另行下令；閘門是否開啟仍由人逐一決定。**

1. **超跌候選怎麼定義「明顯」**：程式只用殘差正負分類並附名次，由交易 Agent 列舉判斷。理由：沒有擬合數字；代價是標示很寬（當天約 66 檔是超跌候選），已寫進第 2.4 節。
2. **超跌候選能不能走第三階段換股**：預設不含，只當紀錄；之後由人另開 `oversold` 區塊並套用全部額外條件。理由：接刀風險最高，程式分不出跌過頭與真有壞事。
3. **第三階段的時程**：第 1、2 階段開啟後累積數個決策日的紀錄、人看過再開，觀察天數由人定。理由：先有即時紀錄；樣本仍然不足，開啟是人承擔風險的決定，不代表驗證通過。
4. **事件方向列舉**：事件分析師對每則事件多給方向，排在第 2 階段。理由：現行契約下「high 且負面」當天一檔都抓不到。
5. **估值授權與起步範圍**：核准證交所 `BWIBBU_ALL` 與 FinMind `TaiwanStockPER` 用於 V1 收集與核對，先做上市股，上櫃端點確認後再加。理由：官方沒有歷史，越早開始可核對的時間點資料越多。
6. **估值位置的視窗**：並列近 250 日、近 750 日與全部可得歷史。理由：2308 的例子顯示單一視窗會誤導。
7. **事實給誰看**：只給分析師與交易 Agent 當背景，多空研究員經分析師報告取得。理由：不動共同輸入雜湊與多空隔離。
8. **第三階段的轉向跡象**：`turn_assessment` 至少 `turn_unconfirmed`。理由：財報不再把關後，這是僅剩的接刀防線之一。
9. **Yahoo 財報備援的優先序**：先把 FinMind 補抓排進每日流程，看缺口剩多少再決定 Yahoo。理由：今天補抓後缺口已很小，Yahoo 實測暫時補不到。

仍待確認的技術問題（不是取捨，實作時查明）：`BetaModel` 視窗平移對 `exposure_review` 重建的影響、`team_inputs` 線性版本鏈、櫃買估值端點、PER 的 EPS 基準與殖利率基準。（第五版：前兩項已於第一階段實作時查明，見第 9.1 節；後兩項屬估值，仍待確認。）

## 8. 摘要

- 做什麼：程式對全交易池算「跌幅能不能被大盤解釋」（視窗前 60 日 beta 調整後殘差），並分成兩個並列的候選：被大盤拖累（殘差不為負）與超跌（殘差為負、原因不明）；兩者都要求程式查無新壞事。
- 財報為輔：年減、利益率下降、現金流為負只是疑慮，缺資料只是不知道；都不擋進場，也都不算通過。
- 超跌只描述價格：不等於低於本來價值，不是買進證據，不自動換股或加權；買不買由交易 Agent 單獨決定。
- 不做什麼：不設殘差、跌幅或估值門檻，Agent 不輸出數字，不放寬現金下限／換手／檔數／Guard，不自動下單，不寫便宜／低估。
- 估值：資料拿得到（證交所與櫃買當日、FinMind 有歷史，2308 兩邊一致），但系統尚未接入、授權未登記。先只做收集與核對，累積後才以背景進簡報。
- 估值的限制：2308 的 PER 和近一年比在最低 1%，和 2019 年以來比在最高 16%；單看位置不能下便宜或貴的結論。
- 案例：2327、2454、2317、2382 是被大盤拖累候選（後兩檔其實跌得比交易池少）；2308 是超跌候選，同時有 337 調查未定。
- 當天買單：第一、二階段不會多出買單（名額已滿）；第三階段開啟最多多一對換股，且預設不含超跌候選，不會是 2308。
- 第一階段：只有價格事實、三個財報清單與候選標示進簡報（閘門 `quality_dip_facts`，預設關閉、不影響配置），Finalizer 整份重算，舊 run 逐位元不變。
- 設計取捨九項已由使用者決定照建議（第 7.2 節）；全部尚未實作，開工需使用者另行下令。

## 9. 第一階段實作紀錄（2026-11-10，第五版）

狀態：**已實作、閘門預設關閉、尚未開啟**。沒有 `position_sizing.quality_dip_facts` 的 Policy（含目前正式設定）、舊 bundle 與舊 run：簡報、`team_inputs` 與重建行為逐位元不變。開啟＝在 `config/decision_policy.json` 的 `position_sizing` 加 `{"quality_dip_facts": {"version": "v1"}}`，回滾＝刪除該欄位；開不開由人決定。

程式：`src/etf_agent/decision/dip_facts.py`（`build_dip_facts`、`analyst_dip_view`、`validate_dip_facts`、`dip_background_brief`）；閘門登記在 `decision/sizing.py` 的 `GATE_PREREQUISITES`（沒有 Policy 層的前提）與 `strategy_reflection/contracts.py` 的 `FORBIDDEN_PREFIXES`；接線在 `automation/daily_pipeline.py`、`automation/team_stages.py`；Finalizer 在 `decision/finalization.py`。測試：`tests/decision/test_quality_dip_facts.py`。

### 9.1 待確認項目的結論

**(a) `BetaModel` 視窗平移**：`BetaModel` 一行未改。視窗前 beta 是把 `price_series` 截到基準日（bar 的 `trade_date` ≤ D[-21]）後，用同一個 `BetaModel(…, 60)` 建模型；評估視窗的池日報酬用 `BetaModel(bundle, 20)` 的 `pool_returns`。兩者都在交易日聯集的日期格上，評估視窗是最後 21 個日期，beta 窗是 D[-81]～D[-21]（61 個日期、60 個日報酬），在基準日相接。`exposure_review` 的程式路徑完全沒動；封存 2026-11-10 run 的 `exposure_review` 仍以 `validate_exposure_review` 重算相符（測試 `ArchivedCaseTests`）。

**(b) `team_inputs` 版本**：不新增 2.9，改為**選配鍵 `dip_facts`**：不升 `schema_version`、不排進線性鏈，只要求 2.6 以上（事實讀 `fundamental_metrics`）。Finalizer 先以「去掉 `dip_facts` 後的欄位集合」做原本的版本白名單檢查，再要求 `dip_facts` 與 Policy 閘門雙向同時存在，存在時以 `validate_dip_facts` 整份重算。取捨：兩種做法對沒有閘門的舊 run 都逐位元不變；線性 2.9 會讓這個紀錄用閘門依賴 `rebound_participation`（關掉反彈參與就得一起關），選配鍵沒有這個耦合。代價是同一個版本號可能多一個鍵，所以檢查一律以閘門為準。

**(c) 事實進簡報的位置與可否引用**：照第 3.1 節，與計畫沒有矛盾，只有一處時序上的必要調整：

- 技術分析師：每檔 `market_attribution` 與 `price_position`，各附該股既有行情 `evidence_id`；頂層 `dip_facts_background`（`citable=false`）。
- 基本面分析師：每檔 `fundamental_flags`（三個清單與時間事實），各條附原始財報／月營收證據。**事件面那一項不在這裡**：三位分析師是同時跑的，基本面分析師拿到簡報時事件報告還不存在，所以標 `event_check: not_applied`，Skill 也寫明。
- 兩位分析師的簡報都**不含** `price_pattern`／`candidate`（研究員讀得到分析師簡報檔，標示只給交易 Agent）。
- 交易 Agent：多空研究完成後才建立完整事實（含事件一項、`price_pattern`、`candidate`），存成 `dip_facts.json`（研究員拒讀清單 `LATER_STAGE_GLOBS` 已加 `dip_facts*`），以 `dip_background`／各股 `dip` 進簡報，去掉 evidence_id 與事實識別碼；文字欄位出現 `dip_background`、`dip_facts` 或事實識別碼即拒絕。
- 多空研究員與風險 Agent：不給，`shared_input_sha256` 不變。不新增證據種類。

### 9.2 實作時寫死的口徑（第四版沒寫清楚、或與第四版用語不同之處）

- **殘差的池報酬是「交易池等權日報酬連乘」**（與估 beta 用的日報酬序列同一口徑），不是各股 20 日報酬的簡單平均。案例表的殘差、名次與第 2.4 節的 72／56／21 都只有這個口徑重算得出來；第 6.1 節「相對交易池」一欄則是簡單平均（−18.43%）。兩者以不同欄位名並列：`pool.compound_return_20d`（−19.15%）與 `pool.return_20d`（−18.43%）。若改用簡單平均，2327 的殘差會變 −0.8 個百分點、標示翻成超跌。
- **覆蓋門檻**：有殘差的股票少於交易池 80%（沿用 `momentum.MIN_REGIME_COVERAGE`）時整份 unavailable；這是資料品質常數，不是旋鈕。
- **「視窗內新可得」**：`available_at` 的台北日期晚於基準日 D[-21]、且不晚於 cutoff。
- **淨利由盈轉虧的前一期**：系統唯一的可比口徑是基本面指標 `net_income_yoy_pct` 的去年同季依賴；指標不可用即列不知道。同一事實列為新壞事時，不再重複列入淨利年減的疑慮。
- **金融業月營收轉負列疑慮**：第 2.2 節以 `growth_signal_quality` 為 `industry_formula_unsupported` 判定，但金融股的淨利年增算得出來、等級是 `comparable`，抓不到 5880 這類股票。實作另以財報產業別 fh／basi／bd／ins（與 `corporate_parsing` 同一組代碼）判定為金融業，兩者任一成立即改列疑慮（符合第 6 章「金融業的月營收轉負進 concerns」）。所以 2026-11-10 的月營收轉負新壞事是 5 檔＋淨利由盈轉虧 1 檔（3363），超跌候選 67 檔、被大盤拖累候選 55 檔（第 2.4 節寫「約 66／約 55」）。
- 單位衝突（指標單位不是預期的 percent／percentage_points、依賴幣別不一致、現金流不是 TWD）列不知道，不拿來比大小。
- bundle 須有 `fundamental_period_policy`（2.6 以上）；第一階段**不要求** `typed_evidence_v1`（第 4.1 節只把它列為第二階段前提）。

### 9.3 2026-11-10 案例重算（`replay_reference_only`）

| 代號 | 20 日報酬 | 相對交易池 | 視窗前 beta | 殘差（名次／149） | 計畫表 | 候選 |
| --- | --- | --- | --- | --- | --- | --- |
| 2327 | −36.48% | −18.04 pp | 1.934 | +0.57 pp（79） | 1.94／+0.5（79） | 被大盤拖累 |
| 2308 | −41.71% | −23.27 pp | 1.241 | −17.94 pp（4） | 1.24／−18.0（4） | 超跌（cause: unknown） |
| 2454 | −22.96% | −4.52 pp | 1.461 | +5.02 pp（105） | 1.46／+5.0（105） | 被大盤拖累 |
| 2317 | −5.16% | +13.28 pp | 0.797 | +10.12 pp（133） | 0.80／+10.1（133） | 被大盤拖累 |
| 2382 | −11.49% | +6.94 pp | 0.753 | +2.93 pp（93） | 0.75／+2.9（93） | 被大盤拖累 |

名次與 72／56／21 完全相同；殘差差在 0.1 個百分點內（四捨五入與 beta 量化到 6 位），2327 的 beta 1.934 與表中 1.94 差在第二位小數。疑慮與不知道與第 6.1 節一致（2454 的四項疑慮、2317 的兩項現金流、2382 的利益率、2308 的三項缺去年同期）。3665 殘差 −13.54 pp、第 7 弱。

### 9.4 仍需人決定

- 是否開啟 `quality_dip_facts`（開啟後每日決策會多算一次全池事實、分析師與交易簡報變長）。
- 第二、三階段與估值 V1 的開工。

## 10. 第二階段、估值 V1 實作紀錄與第三階段修訂（2026-11-10，第六版）

### 10.1 第二階段（已實作、閘門預設關閉、尚未開啟；證據等級 `replay_reference_only`）

兩個閘門都只由人設定、策略反思不得改（已登記 `FORBIDDEN_PREFIXES`）、前提都是 `quality_dip_facts`；沒有欄位的舊 Policy、舊報告與舊 run 簡報、Schema、驗證逐位元不變。開啟＝在 `position_sizing` 加 `{"quality_dip_assessment": {"version": "v1"}}`（交易 Agent 列舉）與／或 `{"event_direction": {"version": "v1"}}`（事件方向）；回滾＝刪除該欄位。

- **`quality_dip_assessment` v1**（`decision/dip_facts.py`、`decision/trader.py`）：交易 Agent 對每個 buy／add 多填列舉 `dip_assessment`（`not_a_dip`／`market_driven`／`oversold_cause_unclear`／`stock_specific`／`undetermined`），其餘意圖填 `null`。驗證只核必填、值域，以及 fail-closed：該股價格事實不可用（整份 unavailable、`price_pattern` 為 `unavailable` 或事實裡找不到該股）時只能 `undetermined`；沒有 `team_inputs.dip_facts` 或決策輸入不是 `typed_evidence_v1` 即拒絕。列舉與意圖、信心、`candidate` **不設對應**，不進 Policy 綁定與配置；Finalizer、回放與每日 pipeline 都以 Policy 閘門重建同一個驗證（`TradeDecisionValidator(dip_assessment=…)`）。Schema 只在閘門開啟時才多這個欄位（`trade_schema_with_dip_assessment`）。
- **`event_direction` v1**（`decision/dip_facts.py`、`decision/analysts.py`）：事件分析師對每則事件（含個股新聞）多填 `direction`（`positive`／`negative`／`neutral`／`unknown`），只描述該則事件對公司的方向，與 `materiality`、整檔 `event_outlook` 分開。與第四版計畫「新報告版本」的差異：**不新增報告 `schema_version`**，而是比照 `counter_evidence`／`corroborated_by` 的做法，`AnalystReportValidator` 對 `direction` 採「有就必須是列舉值、沒有照舊有效」，只有每日 pipeline 產出時傳 `require_event_direction=True` 才必填；理由是辯論驗證（`stance.py`）等下游驗證看不到 Policy，新增版本字串會牽動全部 `MARKET_SCOPED_VERSIONS` 的使用處。Finalizer 在閘門開啟時另要求事件報告每則事件都有合法 `direction`。
- **新壞事事件面的判準隨閘門切換**：沒有 `event_direction` 時仍是第 2.2 節的弱判準（整檔 `event_outlook` 為 negative 且視窗內有 high 事件，項目 `high_event_negative_outlook`）；有閘門時改為「視窗內新可得、`materiality` 為 high 且 `direction` 為 negative」（項目 `high_event_negative_direction`），high 事件缺 `direction` 列入 `unknown`（`EVENT_DIRECTION_MISSING`），不當成通過也不當成利空。`validate_dip_facts` 依 Policy 的 `event_direction` 閘門重算，所以兩種判準的封存 run 各自可重建。第二階段仍遵守第 9.1 節 (c)：分析師階段的 `fundamental_flags` 仍是 `event_check: not_applied`。
- 測試：`tests/decision/test_quality_dip_assessment.py`、`tests/decision/test_event_direction.py`，以及 `tests/research/test_analysts.py` 的 `direction` 驗證。Skill：`trader`（`references/data-rules.md`「dip_assessment」節與新壞事事件面說明）、`event-analyst`（「事件方向」節）。
- 兩者開啟前的建議：先開第一階段 `quality_dip_facts` 並看過幾個決策日的簡報與重算結果；`event_direction` 開啟後事件分析師輸出變長、驗證失敗率要觀察。

### 10.2 估值 V1（已實作、已掛進資料排程；只收集與稽核，不進決策、簡報或報告）

只做上市股（`BWIBBU_ALL`）與 FinMind `TaiwanStockPER` 的逐檔核對與不可變保存，細節見 [資料來源清單](data_sources_inventory.md#估值資料v1)與 [來源核對紀錄](source_audit/2026-11-10_valuation_sources.md)。指令：`cli/valuation_collect.py collect`、`validate <run_id>`。**已於 2026-11-10 由使用者下令掛進每日資料排程**（`start.sh` 的資料階段，20:00、06:00 各一次，`VALUATION_COLLECT=0` 關閉，失敗只記錄、不影響資料狀態與決策）。真實端點驗證：官方端點已實測（2026-11-10 回 1087 檔、100 檔交易池上市股都找得到）；FinMind 端因執行環境沒有 `FINMIND_TOKEN` 尚未實測，其餘靠 mock 測試。V2 的進入條件（連續核對相符的天數由人指定、EPS 與殖利率基準待釐清、上櫃端點待確認）都未滿足，所以**沒有做 V2、V3**。

### 10.3 第三階段修訂：改走換股提名（使用者 2026-11-10 選 A；尚未實作）

第 4.2 節的 `quality_dip_swap`（程式依條件配對換股）**不再建**。理由：[Agent 主導曝險計畫](agent_led_exposure_plan.md)與[自主權計畫](agent_autonomy_plan.md)已決定「換誰」改由交易 Agent 在 `agent_swap` 第二輪提名，不另開程式配對路徑。新的做法（設計，未實作）：

- 優質股換股是提名配對的一種用途：沿用 `agent_swap` 的有序配對清單；與曝險階段 2 的 `purpose` 欄位合用同一個列舉（例如新增 `quality_dip`），**不另開欄位**，兩個計畫的列舉要一起定。
- 買進側依據仍須是新股已採納的多頭論點；候選標示與 `dip_assessment` 只是簡報背景，不是依據。第 4.2 節第 4～7 項（無新壞事、非價格多頭 claim、`turn_assessment` 至少 `turn_unconfirmed`、可交易且不在冷卻）改成提名 Validator 與配置前置的結構檢查（程式只驗結構，不判斷理由好壞）；超跌候選預設排除的規則與 `oversold` 區塊照第 4.2 節。
- 現金下限、換手上限、檔數、單股上限、回撤下限、減碼日略過一律不放寬；買進金額不超過賣款。
- **前提與阻礙**：`agent_swap` 目前只有 v1 影子（配置不讀提名），v2 未開啟；曝險階段 2（`purpose`）正由另一個工作階段實作中（同一批檔案：`swap_nomination.py`、`allocation.py`）。所以本階段**不在這次寫程式**，避免與它衝突；等曝險階段 2 落地、`purpose` 列舉定案，且第 1、2 階段累積數個決策日的紀錄後，由人決定開工。

### 10.4 第三階段實作紀錄（2026-11-10，第七版）

狀態：**已實作、閘門預設不存在＝關閉、尚未開啟**（`config/decision_policy.json` 未改）。程式在 `src/etf_agent/decision/quality_dip_swap.py`，接線在 `swap_nomination.py`（5c 事實、簡報、Validator）、`allocation.py`（`_agent_swap` 與股數上限）、`trader.py`（綁定 `turn_assessment`）、`finalization.py`（比對綁定）、`automation/team_stages.py`／`daily_pipeline.py`（5c 另讀 `dip_facts`、Schema `SWAP_NOMINATION_QUALITY_DIP_SCHEMA`）。測試：`tests/decision/test_quality_dip_swap.py`。Skill：`tradeagent/trading/trader/references/swap-nomination.md`「purpose=quality_dip」節、`data-rules.md` 的 dip_assessment 一句。

**閘門形狀**：`position_sizing.agent_swap.quality_dip = {"version": "v1"|"v2", "max_swaps_per_run", "max_entry_weight", "min_new_conviction", "min_holding_days", "min_turn_assessment"}`，全部必填、程式不給預設（數字由人填）；選填 `oversold: {"max_swaps_per_run", "max_entry_weight"}`（都不得高於上一層）；v2 另有 pipeline 綁定的 `turn_assessment`。`max_swaps_per_run` 為 0～`agent_swap.max_swaps_per_run`、`max_entry_weight` 為 (0,1) 的數字字串、`min_turn_assessment` 只允許 `turn_unconfirmed`／`turn_confirmed`。前提（`GATE_PREREQUISITES`）：`agent_swap`、`agent_swap.exposure`、`quality_dip_facts`、`quality_dip_assessment`、`cash_stance_turn_facts`；v2 另需 `agent_swap` v2。只由人設定（`agent_swap` 前綴已在策略反思禁區）；回放隨 `agent_swap` 一起略過，notes 另標 `agent_swap.quality_dip: ignored_in_replay`；`team_inputs` 仍是 2.9、不升版。

**行為**：

- 5c 結構檢查（Validator，失敗走修正循環、回饋並列「改用 thesis」與「刪除該對」兩種修法）：換入者除 thesis 的全部資格外，信心不低於 `min_new_conviction`；`dip_facts` 整份可用、該股 `price_pattern` 為下跌、`new_adverse_facts` 為空；`candidate` 為 `market_dragged` 時 `dip_assessment` 必須是 `market_driven`，`oversold` 只有寫了 `oversold` 區塊才可、且必須是 `oversold_cause_unclear`；`buy_basis_ids` 至少一個是該股已採納、`claim_kind` 不是 `price` 的多頭 claim（沒填 `claim_kind` 不算）。換出者：thesis 的換出依據規則不變，另需第一輪 `hold`／`low`、持有天數不少於本閘門的 `min_holding_days`。提名理由寫出 `dip_background`／`dip_facts` 或事實識別碼即拒絕。
- 簡報（v1／v2 相同，不告知影子期）：新股與持股多 `quality_dip_nominable`／`quality_dip_blocked_reasons`，新股另附 `dip` 背景（候選標示、價格型態、交易 Agent 的 `dip_assessment`，`citable=false`），頂層 `quality_dip_swap` 區塊；數字上限不給 Agent。
- v1（影子）：配置完全略過 quality_dip 配對、不留旗標——**不會被當成 thesis 配對成交**（曝險 v1 的過濾原本只濾 `exposure`，已改為依原清單對齊 purpose 再分別處理）。
- v2：配置時另檢查（任一不成立即取消全部 quality_dip 配對、換出者不賣）：回撤下限 `stance_floor` 不是 defensive 且帳本歷史可用（`quality_dip_stance_floor`／`quality_dip_drawdown_unavailable`）、風險 Agent 的 `turn_assessment` 不低於 `min_turn_assessment`（`quality_dip_turn`）、`reuse_sell_proceeds` 為 true（`quality_dip_proceeds`）；對數超過上限從清單尾端取消（`quality_dip_cap`，超跌另有自己的上限、合計仍受本閘門封頂）；`dip_assessment` 與候選類別不符即取消（`quality_dip_assessment`）。成交的換入者股數另以「權重不超過 `max_entry_weight`（超跌用 `oversold.max_entry_weight`）且買進金額含費不超過換出者全部賣出的淨額」為上限（只縮減，旗標 `RISK_QUALITY_DIP_ENTRY`）；縮到不足一張就沒有買單，復原迴圈取消該對（`AGENT_SWAP_CANCELLED:quality_dip_entry`）、換出者不賣。減碼日由 `_agent_swap` 整個略過；現金下限、`max_turnover_rate`、單股、產業、Active Share、檔數照舊是後段上限。換出者賣單由程式標 `exit`＋`AGENT_SWAP`（說明寫明目的與上限），交易 Agent 意圖不改寫。

**第 7 節 fail-closed 清單對應的測試**：不只賣不買（權重過小、換手不足、轉向不足、stance_floor 時換出者都不賣）、減碼日與 `stance_floor` 防守略過、買進金額不超過賣款（逐筆比對 `net_cash_impact`）、不突破 `max_turnover_rate` 與現金下限、不改寫交易 Agent 意圖、缺資料不通過也不否決（同一檔改用 thesis 照樣通過）；另有 v1 影子逐位元不變、v2 未綁 `turn_assessment` 拒絕、沒有閘門時配置與 purpose 允許值不變、正式樣板回歸。

**未決事項的保守取捨（第 4.2、10.3 節沒寫死、也沒有人決定的部分；理由一併記錄）**：

1. **列舉加不加 `quality_dip`**：加，但只在本子閘門存在時才是合法值；所以前提要求 `agent_swap.exposure`（`purpose` 的宿主），不另開欄位、也不讓沒有閘門的 Policy 多一個值。理由：第 10.3 節要求合用同一個列舉；只在閘門存在時才合法，舊 Policy 逐位元不變。
2. **換出依據**：沿用 thesis 規則（換出股自己已採納的空頭論點、持股檢視警示或持股事實），**沒有**照第 4.2 節的程式路徑放寬成「hold／low 即可、不需空頭依據」，另外再加上第 4.2 節的 hold／low 與持有天數。理由：第 10.3 節沒有對換出側開例外（曝險配對有明文例外，這裡沒有），放寬屬於規則變更、應由人決定。**代價（如實說明）**：quality_dip 配對的每一條都比 thesis 配對嚴格（多結構條件、多買進權重上限、買進不超過賣款），所以它不會讓任何原本 thesis 換不成的股票換成；它目前的作用是讓交易 Agent 標示目的、讓程式在這類配對上加防線並留下紀錄。若要它真的「多給一個名額來源」，需要人決定下列兩項之一：換出側是否改用 hold／low 不需空頭依據，或換入側是否允許低於 `rotation.min_new_conviction`。
3. **對數上限檢查放在哪裡**：只在配置時從清單尾端取消，5c 不因提名對數超過本閘門上限而退回（與曝險配對相同；清單是「有序候選」）。
4. **`turn_assessment` 何時檢查**：5c 時風險 Agent 還沒跑，所以只在配置時檢查（由 `apply_trade_decision` 綁進 Policy，Finalizer 以現金姿態比對）。
5. **回撤歷史不可用時**：視同不能確認不在強制防守，整批取消；`drawdown_control` 未綁定時拋錯。
6. **買進金額上限的口徑**：換入者「成交價含滑價的金額＋手續費」不超過換出者全部股數「成交價含滑價的金額－手續費－證交稅」；`reuse_sell_proceeds` 為 false 時賣款不能用，整批取消。

**開啟前需要人決定**：`max_swaps_per_run`（計畫建議 1）、`max_entry_weight`（計畫建議不高於 low 信心份額）、`min_new_conviction`、`min_holding_days`、`min_turn_assessment` 的數字；是否另開 `oversold`；先開 v1 影子還是直接 v2；以及上面第 2 項是否要放寬。開啟仍須先存樣板到 `config/policy_versions/`、改 `policy_id` 與真實 `available_at`。

### 10.5 估值 V2 實作紀錄（2026-11-10，第七版）

狀態：**已實作、閘門預設不存在＝關閉、尚未開啟**。程式在 `src/etf_agent/decision/valuation_facts.py`，接線在 `automation/daily_pipeline.py`（分析師階段前建事實並存 `valuation_facts.json`）、`automation/team_stages.py`（基本面分析師與交易 Agent 簡報）、`decision/trader.py`（文字欄位不得寫背景識別碼）、`decision/finalization.py`（`team_inputs.valuation_facts` 選配鍵與重算）、`evaluation/replay.py`（略過，notes 標 `valuation_facts: ignored_in_replay`）。測試：`tests/decision/test_valuation_facts.py`、`tests/automation/test_valuation_facts_pipeline.py`。Skill：`fundamental-analyst`「估值位置」節、`trader` 的 `data-rules.md`「估值位置」節。

- **閘門**：`position_sizing.valuation_facts = {"version": "v1"}`，沒有 Policy 層前提；只由人設定（策略反思禁區 `valuation_facts`）；`GATE_PREREQUISITES` 已登記。沒有欄位的舊 Policy／舊 run：簡報、Schema、`team_inputs` 與重建逐位元不變。
- **資料**：只讀估值 V1 已不可變保存的 run（資料夾由決策庫位置推得：`artifacts/decisions` 旁的 `artifacts/valuation_collection`，pipeline 與 Finalizer 用同一個推法）。取 run 時間不晚於 `decision_cutoff` 的最新一個，先以 `validate_valuation_run` 依原始回應整份重建；不合格、`fetched_at` 晚於 cutoff、資料日晚於 Snapshot 最近交易日、或沒有 run 時整份 `unavailable` 並記 `data_gaps`，不往前找舊 run、不補值、不讓當天決策失敗。
- **內容**：只算 PER 與 PBR（殖利率的股利基準未確認，不進簡報）；上櫃股一律 `OTC_NOT_COLLECTED`；核對不符沿用 V1 的拒絕原因；PER 非正或未提供時該指標 `null` 並附原因、PBR 獨立可用。三個寫死的視窗並列：`rows_250`、`rows_750`（資料列，不是日曆日）、`all`（全部可得歷史）；固定視窗資料列不足時 `insufficient_history`、不算數字；`all` 至少 2 筆。每個視窗給起訖日、樣本數、最小／中位／最大與百分位（不高於當日值的筆數 ÷ 樣本數，含當日，小數 4 位）。只有資料日當天與官方核對過，之前的 FinMind 歷史一律標未逐日核對。不提供交易池橫斷面名次（跨產業比較沒有意義，金融股不得與其他產業並列）。
- **簡報**：基本面分析師簡報頂層 `valuation_background`（每批只附本批股票）、交易 Agent 每批 `valuation_background`；都 `citable=false`、沒有 evidence_id、facts_id 與 run_id。交易 Agent 文字欄位寫出 `valuation_background`、`valuation_facts`、facts_id 或 run_id 即拒絕；分析師的 finding 本來就只能引用 evidence_id，背景沒有可引用的編號（不另加文字檢查，見下方限制）。多空研究員與風險 Agent 不直接拿；研究員讀得到基本面分析師的簡報檔（與 `dip_facts` 相同的已知性質）。
- **重算**：Finalizer 以同一個資料夾與 `source.run_id` 整份重算 `team_inputs.valuation_facts`；欄位被改、run 事後被改（manifest 或重建不符）、讀不到資料夾、或閘門與鍵不雙向同時存在一律拒絕。

**與計畫不符或保守之處（如實說明）**：

1. **授權範圍擋住實際使用**：V1 收集器記錄的 `license_scope` 是 `valuation_v1_collection_and_audit_only`（只收集與稽核）。V2 只接受允許「背景進簡報」的範圍（程式常數 `APPROVED_LICENSE_SCOPES = ("valuation_v2_background_brief",)`），所以**目前的紀錄即使開了閘門也全部是 `LICENSE_SCOPE_NOT_APPROVED`、整份 unavailable**。要讓它有內容，須由人核准新的用途範圍，**而且要改 V1 的契約**：不能只改收集器的 `LICENSE_SCOPE` 常數——`build_symbol_record` 把當下的常數寫進每筆紀錄且納入 `content_sha256`，`validate_valuation_run` 以原始回應重建時也用當下的常數，所以一改常數，**所有既有 run 的重建都會失敗**（已實測：「records 與原始回應重建結果不一致」），連帶讓 `cli/valuation_collect.py validate` 與引用舊 run 的決策重建失敗。可行的做法（由人選）：(a) 把核准的範圍寫進每次收集保存的輸入（例如 `official_raw`／收集參數），重建時從資料讀而不是讀常數；(b) 依 `fetched_at` 查一張有日期的核准表；(c) 升 `MAPPING_VERSION`，每個版本對映自己的範圍。本次沒有替使用者做這件事，也沒有改 V1 程式。
2. **進入條件未滿足**：第 2.6 節 (5) 的「V1 連續核對相符達人指定天數」「EPS 基準釐清」仍未成立（天數沒有指定、全部相符 0 天）；本次依使用者下令實作程式，但閘門沒有開，開不開仍由人依這些條件決定。
3. **視窗長度**：V1 預設每次只補約 14 天歷史，`rows_250`／`rows_750` 在建立長歷史（`--history-start`）之前都是 `insufficient_history`；只有 `all` 有數字，且樣本很小。
4. **已知限制（重建）**：事實為 unavailable 且沒有選到 run（例如 `NO_VALUATION_RUN`）時，Finalizer 不釘住 run、重新挑選；若事後才存進一個時間不晚於 cutoff 的 run，之後重新驗證這份決策會被拒絕。另外，決策重建從此依賴估值資料夾：刪掉或搬走舊 run，引用它的決策重建會被拒絕。驗證端（`competition/dplan.py`、`decision_delivery/provider.py` 等）沒有傳決策庫路徑時推不出估值資料夾，閘門開啟後會一律拒絕；正式流程 `scripts/run_daily_pipeline.py` 傳的是 `artifacts/decisions`，推得的位置就是 `cli/valuation_collect.py` 寫入的 `artifacts/valuation_collection`。
5. **分析師文字沒有加識別碼檢查**：分析報告的驗證（含辯論驗證）看不到 Policy，加閘門依賴的檢查會牽動舊報告驗證；因為背景沒有任何可引用的編號，這裡維持不加，靠 Skill 規則。

> **第四版後續（2026-11-11）**：組數與份量改由交易 Agent 提、風險 Agent 裁決的設計與實作紀錄見 [`quality_dip_agent_sizing_plan.md`](quality_dip_agent_sizing_plan.md)（`agent_swap.quality_dip` v3，已實作、尚未開啟）。

### 10.6 V3 需要人決定的事（2026-11-10，第七版；本次未實作）

第 2.6 節 (5) 與第 7 節把 V3 標為「未定、V2 開啟後有即時紀錄再由人決定」，而且它會改證據政策（`typed_evidence_v1` 的種類對映）的既有行為，所以本次**沒有寫程式**。開工前需要人逐項決定：

1. **前提**：V2 要先開啟並累積幾個決策日的即時紀錄（目前 0 天；且 V2 的授權範圍見第 10.5 節第 1 項）。選項：(a) 沿用 `calibration.MIN_DECISION_DAYS` 10 天；(b) 人另指定天數；(c) 比賽結束前不做 V3。
2. **證據政策版本**：可引用就必須出新版本、舊 bundle 不變。選項：(a) 新增 `typed_evidence_v2`＝v1 全部規則＋估值文件種類；(b) 不出新版本，V3 只在 bundle 帶新欄位時生效（不建議：等於讓同一個版本有兩種對映）。
3. **種類對映**：(a) 對映既有的 `price`（第 2.6 節 (4) 的建議：不改 `CLAIM_KINDS`、自動適用「單獨價格論點不構成買進依據」）；(b) 新增 `valuation` 種類（要改 `CLAIM_KINDS`、全部 Schema 與 Skill，並決定它能否單獨支持買進）。
4. **估值證據算不算「屬於該股票」**：現行規則要求至少一個證據屬於該股，市場層級與帳戶證據只能補充。選項：(a) 算（它是該股的每日價格衍生值）；(b) 不算、只能補充。
5. **未逐日核對的歷史能不能被引用**：(a) 只有資料日當天（與官方核對過）的值可被引用，位置百分位不能；(b) 位置百分位也可引用，但證據要標「歷史未核對」；(c) 等官方可指定日期的查詢確認後再談。
6. **證據的形狀**：(a) 每檔每資料日一個文件、附 run_id 與紀錄雜湊、`available_at`＝抓取時間；(b) 每個位置事實一個文件。要不要進 Snapshot／DecisionInputBundle（現行證據都在 bundle 內，V2 的資料在 bundle 外，Finalizer 從資料夾讀）也需決定：進 bundle 會改 bundle 契約與雜湊，不進則證據來源不在 bundle 內、與現行規則不一致。
7. **授權範圍**：V3 的引用屬於正式計算的一部分，需要比 V2「背景進簡報」更明確的用途登記（核准人、核准日、用途）。
8. **殖利率與上櫃股**：殖利率的股利基準與上櫃端點確認前，V3 是否只做 PER／PBR 與上市股（建議是）。
9. **誰可以引用**：只有基本面分析師的 finding，或多空研究員的 claim 也可直接引用；後者會改共同輸入與 `shared_input_sha256` 的組成。

在這些決定之前，V2 維持背景、不可引用；任何 Skill、簡報與報告仍不得寫便宜、低估。
