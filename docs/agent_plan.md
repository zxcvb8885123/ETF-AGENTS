# ETF Agent 四層開發架構

本文件為目標架構。以既有 SQLite 與行情管線為基礎，先完成 Data Agent M0～M3 與事件研究；依 2026-09-21 的開發決定，市場情緒與分析師研究 Agent 的契約、工具及 Skill 提前建立，再接投資組合買賣決策與風控多子 Agent、回測與自動化排程。Data Agent M4 補齊的歷史時間點資料仍是正式回測的前置條件。

> 計畫狀態（2026-09-22）：Data Agent M0 已完成，M1 進行中；事件研究多子 Agent、市場情緒／分析師研究 MVP、Research Report V0、Portfolio Decision P0～P6、回測 Agent B0～B2，以及 DailyReport／FailureReport A0～A1 fixture／離線版已完成。真實 Perception Provider、正式帳戶／規則接入、正式歷史回測、D-Plan 與正式排程尚未完成。第一版仍不接 LLM API、LangChain、LangGraph 或 CLIProxyAPI。

## 整體流程

```text
第一層｜數據與市場判讀
  行情、公告、基本面 → 資料庫 → 研究快照與市場狀態
                       ↓
第二層｜研究與分析
  事件研究＋市場情緒與分析師研究（次級訊號）＋動能分析（後續）
  → 進攻／防守候選
  └→ Research Report V0（研究整合，不含交易決策）
                       ↓
第三層｜策略、風控與回測
  Portfolio Decision 主控＋五個子 Agent
  候選＋目前持倉 → 獨立買／賣意圖 → 裁決 → 確定性配置與訂單 → 風控
                                      └→ 回測 Agent：歷史時鐘 → 重播 → 模擬成交
                                                                 → 績效比較 → 前向驗證
                       ↓
第四層｜排程與報告
  自動化排程與報告 Agent
  → 定時啟動正式日常流程 → 保存執行結果 → D-Plan Builder／Validator → 每日報告與候選檔
  → 交付人工檢視（系統邊界）
```

回測 Agent 是上線前與版本變更時使用的驗證分支，不是每日產生交易決策時都要重跑完整歷史回測。

架構沒有「主辦平台送件／交易執行 Agent」。平台送件與交易由人工在系統外處理，不列為待開發 Agent；人工檢視紀錄不構成自動送件或下單授權。

## 各層分工

| 層級 | 回答的問題 | 工作內容 | 輸出 |
| --- | --- | --- | --- |
| 數據與市場判讀 | 有哪些可用資料？市場目前如何？ | 更新行情與事件、檢查缺漏與時間、保存來源；計算市場寬度與趨勢 | `ResearchSnapshot`、市場狀態 |
| 研究與分析 | 哪些股票值得關注？理由是什麼？ | LLM 抽取事件、影響對象與證據；市場認知作次級訊號；Research Report V0 整合研究結果 | `ResearchResult`、`MarketPerceptionResult`、`ResearchReport` |
| 策略、風控與回測 | 要買進、續抱、減碼或退出？如何配置？策略是否有效？ | 多子 Agent 獨立提出買賣意圖並裁決；確定性工具產生配置、訂單及風控；回測 Agent 以相同契約執行歷史重播與前向驗證 | `TradeIntentResult`、`DecisionResult`、`BacktestReport` |
| 排程與報告 | 何時執行？如何呈現並交付人工檢視？ | 自動化排程與報告 Agent 啟動流程、記錄成功或失敗；以確定性 Builder 組裝並驗證 D-Plan 候選檔 | `PipelineRun`、`D-Plan.json`、`DailyReport`、`FailureReport` |

第一層先做市場狀態判讀，預測模型保留為後續擴充。圖片中的工具名稱是參考，不是本版指定依賴。

## Agent、Skill 與程式

由一個流程控制器串接各層，LLM 在需要理解事件與補查證據時使用工具。

| 工作角色 | 技能文件（規劃） | 可使用的工具 |
| --- | --- | --- |
| 資料研究 | `skills/event-data/SKILL.md` | 查詢公告、讀取資料庫、驗證及保存事件 |
| 事件分析 | `skills/event-analysis/SKILL.md` | 查詢事件原文、公司基本面與行情特徵 |
| 市場情緒與分析師研究 | `skills/sentiment-analyst/SKILL.md` | 標記情緒、聚合分歧與熱度、計算分析師共識修正及事件預期差 |
| 研究報告整合（不是決策 Agent） | `skills/research-report/SKILL.md` | 驗證同一 Snapshot 的研究 artifact，建立同源 JSON／Markdown |
| 投資組合買賣決策主控 | `skills/portfolio-decision/SKILL.md` | 固定子 Agent 順序、限制修正次數、重播完整修正鏈並保存 `DecisionResult` |
| 動能與市場狀態 | `skills/momentum-regime/SKILL.md`（規劃） | 解讀確定性動能、波動、流動性與市場寬度結果 |
| 獨立買進／退出研究 | `skills/buy-candidate/`、`skills/sell-exit/`（規劃） | 使用相同輸入且互相隔離，分別提出買進／加碼及續抱／減碼／退出意圖 |
| 買賣裁決 | `skills/trade-adjudication/SKILL.md`（規劃） | 驗證獨立 packets、裁決衝突，不新增事實或計算權重 |
| 配置後風險挑戰 | `skills/portfolio-risk-review/SKILL.md`（規劃） | 檢查情境與集中風險，只提出 allowlist 內的結構化修正 |
| 回測驗證 | `skills/strategy-backtest/SKILL.md` | 鎖定 fixture 版本、歷史重播、整張成交、交割與帳務驗收；策略比較、績效與前向驗證待後續完成 |
| 自動化排程與報告 | `skills/daily-report/SKILL.md`（規劃） | 檢查各階段結果、建立報告、說明失敗與要求人工處理 |
| D-Plan Builder／Validator（確定性程式，不是新 Agent） | 不需要獨立 Skill | 合併 Snapshot、研究、決策與風控輸出；配置引用 ID，執行 JSON Schema 與語意驗證 |

Skill 文件定義任務流程、證據要求與輸出格式，由控制器載入給 LLM；`cli/` 是 Skill、人工與排程共用的穩定命令入口；`src/etf_agent/` 則保存實際 runtime、資料契約與確定性計算。各角色先共用一個應用程式，無須各自部署成服務。動能計算、交易數量、費稅與風控限制由程式執行；LLM 負責事件理解及有來源的文字說明。

## 市場情緒與分析師研究 Agent

社群情緒與分析師目標價不放入 Data Agent。目前已建立獨立的「市場情緒與分析師研究 Agent」契約、fixture 工具鏈與 Skill；真實 Provider 只有在確認資料取得、授權、台股覆蓋與歷史時間一致性後才接入：

- 社群情緒部分辨識來源、熱度、方向、異常擴散與操縱風險。
- 分析師部分追蹤評等、目標價、預估值及共識修正，不把單一目標價視為事實價格。
- 輸出帶來源、時間、分歧與不確定性的研究訊號，不能直接修改核心財務數字、產生訂單或繞過風控。
- 此 Agent 只提供次級研究訊號，不阻擋 Data Agent M1～M3、事件研究、策略與風控主線；未接真實 Provider 時明確輸出 `unavailable`。

## 排程與報告範圍

Research Report V0 已完成研究層的 JSON／Markdown 整合，但不包含配置、訂單、風控、排程、FailureReport 或官方 D-Plan。以下仍是正式自動化排程與報告 Agent 的後續範圍：

- 排程時間可設定；先規劃盤後更新資料、盤前完成研究與決策，並在提交截止前留出人工檢視時間。
- 每次執行保存 `run_id`、資料截止時間、設定與模型版本；失敗時記錄原因並輸出失敗報告。
- 每日報告包含市場狀態、事件證據、進攻／防守候選、建議買賣、現金配置及風控結果。
- 報告引用同一次執行的已驗證數字；未通過風控的提案清楚標記為不可執行。
- D-Plan Builder 只組裝已驗證輸出，不重新推論；必須建立 `sources → observations → market_view／inferences → decisions → orders` 的完整引用鏈。
- D-Plan 必須通過官方 schema 4.0 與語意規則才可成為送件候選；失敗時保存錯誤並 fail closed。
- 本版自動化到產出報告為止，交易與競賽送件由人工處理。自動調參、自主改程式與自動下單不在本版範圍。

## 開發順序與目前狀態

[本地 Agent 真實資料研究演練](local_research_dry_run_plan.md)已完成：同一可用 Snapshot 的 525 件 45 日候選中，三件事件以獨立多空研究通過雙重驗證並建立 Research Report V0。報告因沒有合法、歷史化的市場認知資料而明確降級，三件均為 `pending`，不產生交易候選。這是有限範圍的整合驗收，不改變 M1～M4 與正式決策／回測的前置要求。

| 順序 | 階段 | 狀態 | 下一個明確成果 |
| ---: | --- | --- | --- |
| 0 | Data Agent 基礎版 | 已完成 | SQLite、TWSE／TPEx 月營收與重大訊息、歷史行情、不可變 Snapshot、`event-data` Skill 與 CLI |
| 1 | Data Agent M0 | 已完成 | 可重跑的來源探測、150 檔交易池狀態、`5371`／`3718` 回歸案例及 Snapshot fail-closed 閘門 |
| 2 | Data Agent M1 | **進行中** | 最新行情與 Yahoo 兩年增量刷新已完成；下一成果是官方歷史行情 CLI，再做財報彙總、交易狀態與細粒度工具 |
| 3 | Data Agent M2 | 待 M1 通過 | TWSE RSS 與 Google News RSS 候選層、別名、去重及誤配檢查 |
| 4 | Data Agent M3 | 待 M2 通過 | `DataAgentRequest`／`DataAgentResult`、工具軌跡、Codex／Claude 共用 Skill 工具循環 |
| 5 | 事件研究 Agent | **多子 Agent／ResearchResult 2.1 已完成**；待 M2／M3 完整驗收 | 主控加 Fact／Bull／Bear／Adjudicator Skills、獨立多空 DebateBundle、財務傳導鏈與雙重 validator 已完成；下一步接新聞候選、工具軌跡及人工事件測試集 |
| 6 | 市場情緒與分析師研究 Agent | **契約／工具／Skill MVP 已完成**；真實 Provider 待審查 | 接入通過授權與歷史時間驗證的來源，建立人工標註集與消融評估 |
| 7 | Research Report V0 | **已完成；已通過一次三事件真實資料演練（降級）** | 接入合法、歷史化的市場認知資料後再驗證完整報告 |
| 8 | 投資組合買賣決策與風控多子 Agent | **P0～P6 fixture 驗收已完成** | 整張配置／費稅、部分成交情境重建、必備基準 Guard、完整修正鏈、最終重建與磁碟封存驗證已完成；下一步 P7 回測 |
| 9 | Data Agent M4 | 回測前置 PoC | 補齊可證明 `published_at`／`available_at` 的歷史資料、公司行動與時間點 Snapshot |
| 10 | 回測 Agent | **B0～B2 fixture MVP 已完成**；正式資料待 M4 | 歷史時鐘、時間點研究版本、整張成交、交割、公司行動、封存與帳務驗收已完成；策略比較、Agent 評估及未見資料驗證待後續 |
| 11 | 自動化排程與報告 Agent | **A0／A1 fixture／離線版已完成**；正式排程待前置驗收 | A2 D-Plan → A3 排程 → A4 Skill 整合 → A5 演練；交付人工檢視 |

目前 Data Agent 進入 M1；事件研究 Agent 已先完成可使用現有 Snapshot 的基礎版，但 Data Agent M2／M3 通過前不視為完整驗收。市場情緒與分析師研究 Agent 已提前完成不依賴真實來源的 MVP，Research Report V0 也可整合已保存結果；回測 Agent 已完成 B0～B2 fixture 帳務驗收，但正式歷史 Provider、策略有效性與前向驗證仍待完成。自動化排程與報告 Agent 的 A0／A1 已可用封存 fixture 建立、重建與驗證 DailyReport／FailureReport，但不代表正式日常排程已啟用。正式決策主線仍依 `研究層驗收 → 多子 Agent 買賣裁決與確定性風控 → 回測 → 自動化排程／報告` 通過驗收。

詳細規則見 [Data Agent 計畫](data_agent_plan.md)、[資料來源可行性測試](source_feasibility_2026-09-17.md)、[事件研究 Agent 計畫](event_strategy_v1.md)、[Research Report V0 計畫](research_report_plan.md)、[投資組合買賣決策與風控多子 Agent 計畫](momentum_portfolio_risk_agent_plan.md)、[回測 Agent 計畫](backtest_agent_plan.md)、[回測與驗證方法規格](backtest_plan_v1.md)及[自動化排程／報告 Agent 計畫](automation_reporting_agent_plan.md)。
