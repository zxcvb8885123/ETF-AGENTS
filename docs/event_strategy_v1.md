# 事件研究 Agent 計畫 V1

> 開發順序：第一個下游 Agent。2026-09-20 已升級為主控 Event Agent 加上事實、多方、空方、裁決四個角色；比較基準、財務傳導鏈、獨立多空研究、裁決、事件相對行情與失效條件均進入固定契約。待 Data Agent M2／M3 補齊新聞候選及完整工具軌跡後，再做完整研究品質驗收。

## 定位

本計畫是 [四層架構](agent_plan.md) 的第二層「研究與分析」。目標是讓 LLM 使用資料工具，找出官方交易池中有短期催化事件的股票，留下證據、反證與候選理由，再交由第三層完成配置與風控。

目前已建立主控 `event-analysis`、`event-fact-analysis`、`event-bull-research`、`event-bear-research`、`event-adjudication` Skills，及 `ResearchResult` 2.1／`DebateBundle` 1.0 契約。既有固定評分程式只保留為回測比較基準，不再作為事件結論。第一版可由 Codex／Claude 子 Agent 研究目前 Snapshot 中的月營收與重大訊息，但新聞候選、完整財報、法說、市場共識及無人值守 Agent runtime 尚未完成。

第一版延續 Data Agent 的執行方式：由 Codex 或 Claude 工作階段載入 `event-analysis` Skill，再呼叫受限制的 Python 研究工具；專案不直接串接模型 API，也不引入 LangChain／LangGraph。

方法上採用 Anthropic 官方 equity-research Skills 的「新資訊／預期差、催化日曆、可否證 thesis、同等追蹤反證」，以及 TradingAgents 論文的「專業分析輸入 → 多空研究 → 裁決 → 交易／風險」分層。第一版用 Codex／Claude 的子 Agent 執行，不要求專案串接 LLM API；未來只有在無人值守排程確有需要時，才評估 API runtime。

## 工作流程

```text
接收研究任務、資料截止時間與第一層快照
  → Fact Agent 查公告、基準、版本、相關事件與行情
  → Bull Agent ─┐（只讀同一 FactPacket，彼此不互看）
  → Bear Agent ─┴→ Adjudicator 比較主張、反證與缺口
  → validate-debate 驗證角色完整性與獨立依賴
  → 輸出 ResearchResult
  → validate-result 驗證證據、數字、cutoff 與候選閘門
  → 第三層配置與風控 → 第四層報告
```

控制器限制每次執行的工具次數、時間與 token 預算。達到上限仍缺關鍵證據時，輸出「待確認」及缺漏原因；工具失敗只允許有限次重試。

## Skill 與工具

`skills/event-analysis/SKILL.md` 定義事件研究流程、證據要求、停止條件與輸出格式。Codex／Claude 載入 Skill 後使用下列工具；技能文件本身不執行抓取或入庫。

| 工具 | 狀態 | 用途 |
| --- | --- | --- |
| `status` | 已完成 | 驗證 Snapshot 可用性、截止時間、文件與證據數量 |
| `list-events` | 已完成 | 取得截止時間前、官方交易池內的事件候選 |
| `read-source` | 已完成 | 讀取保存的原始公告、發布時間、版本與證據 |
| `get-company-facts` | 第一版已完成 | 查詢 Snapshot 中當時可用的月營收與重大訊息；財報與展望待資料層補齊 |
| `find-related-events` | 第一版已完成 | 查找同公司舊事件、後續更新與可能反證 |
| `analyze-event-context` | 已完成 | 一次取得原文、歷史比較、版本／相關事件及事件相對行情；明示 YoY 不是市場預期 |
| `get-price-features` | 已完成 | 從 cutoff 前行情計算事件前 5 日、事件日、事件後報酬、量比，以及 1／10 日、ATR 與下行波動 |
| `search-news-candidates` | 待 Data Agent M2 | 查詢已保存的新聞候選、發布者、時間、連結、重複與誤配旗標 |
| `validate-result` | 已完成 | 核對 schema、cutoff、股票、正式引用、數值與失效條件，合格才保存 |
| `validate-debate` | 已完成 | 驗證 Fact／Bull／Bear／Adjudicator packet、同一事件與多空獨立依賴 |

## 子 Agent 分工

| 角色 | Skill | 可讀輸入 | 輸出與限制 |
| --- | --- | --- | --- |
| 事實 | `event-fact-analysis` | Snapshot、正式來源、確定性工具 | FactPacket；不判斷方向 |
| 多方 | `event-bull-research` | FactPacket | 最強多方 thesis、催化、假設與失效條件；不得讀空方 |
| 空方 | `event-bear-research` | FactPacket | 最強反證、傳導斷點、已定價風險與失效條件；不得讀多方 |
| 裁決 | `event-adjudication` | Fact、Bull、Bear packets | 保留／否決主張、未解問題、方向與研究狀態；不得新增事實 |

Validator 不是 LLM 子 Agent。它負責確定性檢查，避免裁決角色自行補造來源、數字或依賴關係。Bull 與 Bear 可平行執行；Adjudicator 必須等兩者完成。

所有工具由程式強制套用 `decision_cutoff` 和股票範圍。資料收集與入庫由第一層負責；研究結果經格式與引用驗證後，由控制器保存。公告與工具回傳文字只作研究資料，不能改寫 Agent 指令或工具權限。新聞候選只能觸發補查或作次級證據；未連回可核對的原始報導或官方公告時，不得單獨形成進攻候選。

工具採細粒度且可組合的設計，參考 [stocks-scoring-agent](https://github.com/realmistic/stocks-scoring-agent) 將公司資料、盈餘趨勢、行情、新聞與文件分為不同工具的做法；但本專案不讓工具即時取得未保存資料後直接形成結論。每個研究工具只讀取第一層已版本化且符合截止時間的資料，並可在沒有 LLM 的情況下獨立測試。

## 分析任務

追蹤月營收、季報與法說、MSCI、公司重大訊息及有明確產業影響的總體事件。每個事件回答：

1. 相對市場共識、公司展望、歷史趨勢或同期，真正的新資訊是什麼？
2. 事件是首次、新進展、修正或重複舊聞？證據品質如何？
3. 事件透過哪些步驟影響營收、成本、毛利、現金流、風險或估值？每一步是事實或推論？
4. 最強多方與最強空方各是什麼，哪些主張經反證後仍成立？
5. 影響是否在比賽期間內具重大性？事件前是否已先反映，事件後價量是否支持？
6. 哪些後續可觀察資料會使 thesis 失效？

LLM 負責事件理解、補查選擇與理由。營收增減、財報期間比較、動能、價格確認及後續權重與費稅由程式計算。事件研究不負責社群情緒或分析師目標價；這兩類由獨立的[市場情緒與分析師研究 Agent](sentiment_analyst_agent_plan.md)處理，通過來源與時間驗證後才可作次級研究輸入。LLM 自報信心不能當作上漲機率，低信心也不等同中性。LLM 不直接輸出可執行的 `buy`／`sell`、最終持股權重或主觀 1～10 信心分數。

## 輸出與下游銜接

`ResearchResult` 使用固定 schema 驗證，保存 `run_id`、`snapshot_id`、截止時間、模型與技能版本，並包含：

| 欄位 | 內容 |
| --- | --- |
| `event_id`／`symbol`／`event_type` | 事件、股票與事件類型 |
| `published_at`／`catalyst_date` | 公告發布時間及可能催化日期 |
| `direction`／`impact_mechanism` | 偏多、偏空、混合、中性或不確定，以及通過反證後的影響機制 |
| `evidence_ids`／`counter_evidence_ids` | 支持與反證的來源 ID，不保存無來源斷言 |
| `fact_values`／`comparison_basis` | 經工具驗證的數字、單位、本期、前期及去年同期比較 |
| `assessment` | 證據品質、新穎性、比較／預期基準、重大性與逐步財務傳導鏈 |
| `research_process` | Fact、Bull、Bear、Adjudicator packet ID 與多空獨立標記 |
| `debate` | 獨立多方、獨立空方、保留／否決主張、未解問題與最終裁決 |
| `price_confirmation` | 事件前、事件日、事件後相對報酬、成交量、ATR、波動與 confirmed／unconfirmed／contradicted 狀態 |
| `risk_flags`／`uncertainties` | 資料衝突、來源缺漏、舊聞、追價及可交易性風險 |
| `research_status` | `candidate`、`pending` 或 `excluded`，以及理由 |
| `invalidation_signals` | 會使研究假設失效的後續公告或價格條件 |

欄位以 Pydantic 或等價 schema 驗證；列舉值、日期、股票代碼與引用由程式檢查。引用不存在、數字對不上或時間不合格的結果不得進入候選。後續動能研究提供防守候選，第三層整合兩類候選並產生買賣計畫。原有 65／75 分等固定門檻保留為待驗證基準，不要求 LLM 自行湊出總分。

## 開發順序

1. [x] 建立 `event-analysis` Skill，讓 Codex／Claude 共用相同事件研究流程與邊界。
2. [x] 實作可獨立測試的事件清單、來源原文、公司事實、相關事件及行情特徵工具。
3. [x] 建立 `ResearchResult` 2.1 與 `DebateBundle` 1.0 契約，驗證正式引用、數字、比較基準、新穎性、重大性、獨立多空研究、裁決、事件相對行情、失效條件及截止時間。
4. [x] 建立 Fact／Bull／Bear／Adjudicator 四個角色 Skill 與 `validate-debate`，並以 `8932.TWO` 完成第一次獨立試跑。
5. [ ] Data Agent M2 補齊新聞候選、公司別名、去重與誤配旗標，再接 `search-news-candidates`。
6. [ ] Data Agent M3 補齊跨工作階段工具軌跡，再驗證最多 8 次工具呼叫及 Codex／Claude 一致性。
7. [ ] 建立人工標記事件集，測試公司辨識、基準選擇、因果鏈、最強反證、裁決、影響方向、引用正確性與補查能力；對照「無辯論／固定分數」消融版本。
8. [ ] 接上動能、第三層風控及[回測 Agent](backtest_agent_plan.md)。

完成條件：`ResearchResult` schema、引用與 cutoff 驗證全部通過；人工事件測試集達到預先設定的證據正確率與公司辨識門檻；同一 Snapshot 可重播工具結果；失敗或證據不足時不產生可配置候選。完成後交給[動能／配置／風控 Agent 計畫](momentum_portfolio_risk_agent_plan.md)。

保存工具呼叫、回傳資料、結構化結果與簡短決策依據，供重播和稽核；不依賴模型未公開的內部推理。
