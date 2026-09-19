# ETF Agent 四層開發架構

本文件為目標架構。以既有 SQLite 與行情管線為基礎，先完成 Data Agent M0～M3，再依序實作事件研究 Agent、動能／配置／風控 Agent、回測 Agent，最後才接自動化排程與報告 Agent。Data Agent M4 補齊的歷史時間點資料是正式回測的前置條件。

> 計畫狀態（2026-09-19）：Data Agent M0 已完成；M1 的最新行情、Yahoo 兩年增量刷新與 D-Plan 來源證據交接已完成，目前接續官方歷史行情 CLI。第一版仍不接 LLM API、LangChain、LangGraph 或 CLIProxyAPI。

## 整體流程

```text
第一層｜數據與市場判讀
  行情、公告、基本面 → 資料庫 → 研究快照與市場狀態
                       ↓
第二層｜研究與分析
  事件研究（目前）＋動能分析（後續）→ 進攻／防守候選
  市場情緒與分析師研究 Agent（未來獨立擴充，不阻擋第一版）
                       ↓
第三層｜策略、風控與回測
  動能／配置／風控 Agent：候選＋目前持倉 → 配置與買賣計畫 → 風控檢查
                             └→ 回測 Agent：歷史時鐘 → 重播 → 模擬成交
                                                        → 績效比較 → 前向驗證
                       ↓
第四層｜排程與報告
  自動化排程與報告 Agent
  → 定時啟動正式日常流程 → 保存執行結果 → D-Plan Builder／Validator → 每日報告與送件檔
```

回測 Agent 是上線前與版本變更時使用的驗證分支，不是每日產生交易決策時都要重跑完整歷史回測。

## 各層分工

| 層級 | 回答的問題 | 工作內容 | 輸出 |
| --- | --- | --- | --- |
| 數據與市場判讀 | 有哪些可用資料？市場目前如何？ | 更新行情與事件、檢查缺漏與時間、保存來源；計算市場寬度與趨勢 | `ResearchSnapshot`、市場狀態 |
| 研究與分析 | 哪些股票值得關注？理由是什麼？ | LLM 抽取事件、影響對象與證據；程式計算動能及波動，形成進攻／防守候選 | `ResearchResult` |
| 策略、風控與回測 | 如何配置？買賣多少？策略是否有效？ | 動能／配置／風控 Agent 產生決策；回測 Agent 以相同契約執行歷史重播、模擬成交、策略比較與前向驗證 | `MomentumResult`、`DecisionResult`、`BacktestReport` |
| 排程與報告 | 何時執行？如何呈現與送件？ | 自動化排程與報告 Agent 啟動流程、記錄成功或失敗；以確定性 Builder 組裝並驗證 D-Plan | `PipelineRun`、`D-Plan.json`、`DailyReport` |

第一層先做市場狀態判讀，預測模型保留為後續擴充。圖片中的工具名稱是參考，不是本版指定依賴。

## Agent、Skill 與程式

由一個流程控制器串接各層，LLM 在需要理解事件與補查證據時使用工具。

| 工作角色 | 技能文件（規劃） | 可使用的工具 |
| --- | --- | --- |
| 資料研究 | `skills/event-data/SKILL.md` | 查詢公告、讀取資料庫、驗證及保存事件 |
| 事件分析 | `skills/event-analysis/SKILL.md` | 查詢事件原文、公司基本面與行情特徵 |
| 動能／配置／風控 | `skills/portfolio-risk/SKILL.md`（規劃） | 呼叫動能、候選合併、配置、訂單與競賽風控工具 |
| 回測驗證 | `skills/strategy-backtest/SKILL.md`（規劃） | 鎖定版本、啟動歷史重播、模擬成交、比較策略、分析績效與檢查前向驗證 |
| 自動化排程與報告 | `skills/daily-report/SKILL.md`（規劃） | 檢查各階段結果、建立報告、說明失敗與要求人工處理 |
| 市場情緒與分析師研究（未來） | `skills/sentiment-analyst/SKILL.md`（規劃） | 查詢已核准的社群與分析師資料，分析情緒、共識修正、分歧及反證 |
| D-Plan Builder／Validator（確定性程式，不是新 Agent） | 不需要獨立 Skill | 合併 Snapshot、研究、決策與風控輸出；配置引用 ID，執行 JSON Schema 與語意驗證 |

技能文件定義任務流程、證據要求與輸出格式，由控制器載入給 LLM。各角色先共用一個應用程式，無須各自部署成服務。動能計算、交易數量、費稅與風控限制由程式執行；LLM 負責事件理解及有來源的文字說明。

## 未來獨立研究 Agent

社群情緒與分析師目標價不放入 Data Agent。若後續確認資料取得、授權、台股覆蓋與歷史時間一致性，再建立獨立的「市場情緒與分析師研究 Agent」：

- 社群情緒部分辨識來源、熱度、方向、異常擴散與操縱風險。
- 分析師部分追蹤評等、目標價、預估值及共識修正，不把單一目標價視為事實價格。
- 輸出帶來源、時間、分歧與不確定性的研究訊號，不能直接修改核心財務數字、產生訂單或繞過風控。
- 此 Agent 是第一版之後的選配研究層，不阻擋 Data Agent M1～M3、事件研究、策略與風控主線。

## 排程與報告範圍

- 排程時間可設定；先規劃盤後更新資料、盤前完成研究與決策，並在提交截止前留出人工檢視時間。
- 每次執行保存 `run_id`、資料截止時間、設定與模型版本；失敗時記錄原因並輸出失敗報告。
- 每日報告包含市場狀態、事件證據、進攻／防守候選、建議買賣、現金配置及風控結果。
- 報告引用同一次執行的已驗證數字；未通過風控的提案清楚標記為不可執行。
- D-Plan Builder 只組裝已驗證輸出，不重新推論；必須建立 `sources → observations → market_view／inferences → decisions → orders` 的完整引用鏈。
- D-Plan 必須通過官方 schema 4.0 與語意規則才可成為送件候選；失敗時保存錯誤並 fail closed。
- 本版自動化到產出報告為止，交易與競賽送件由人工處理。自動調參、自主改程式與自動下單不在本版範圍。

## 開發順序與目前狀態

| 順序 | 階段 | 狀態 | 下一個明確成果 |
| ---: | --- | --- | --- |
| 0 | Data Agent 基礎版 | 已完成 | SQLite、TWSE／TPEx 月營收與重大訊息、歷史行情、不可變 Snapshot、`event-data` Skill 與 CLI |
| 1 | Data Agent M0 | 已完成 | 可重跑的來源探測、150 檔交易池狀態、`5371`／`3718` 回歸案例及 Snapshot fail-closed 閘門 |
| 2 | Data Agent M1 | **進行中** | 最新行情與 Yahoo 兩年增量刷新已完成；下一成果是官方歷史行情 CLI，再做財報彙總、交易狀態與細粒度工具 |
| 3 | Data Agent M2 | 待 M1 通過 | TWSE RSS 與 Google News RSS 候選層、別名、去重及誤配檢查 |
| 4 | Data Agent M3 | 待 M2 通過 | `DataAgentRequest`／`DataAgentResult`、工具軌跡、Codex／Claude 共用 Skill 工具循環 |
| 5 | 事件研究 Agent | **第一個下游 Agent**；待 M3 通過 | 固定 `ResearchResult`、引用驗證、工具預算與人工事件測試集 |
| 6 | 動能／配置／風控 Agent | **第二個下游 Agent**；待事件研究通過 | 動能特徵、進攻／防守候選、配置、交易數量、費稅、競賽限制及拒絕分支 |
| 7 | Data Agent M4 | 回測前置 PoC | 補齊可證明 `published_at`／`available_at` 的歷史資料、公司行動與時間點 Snapshot |
| 8 | 回測 Agent | **第三個下游 Agent**；待決策層與 M4 必要資料通過 | 歷史時鐘、事件與決策重播、成交與帳務、策略比較、Agent 評估及未見資料驗證 |
| 9 | 自動化排程與報告 Agent | **第四個、最後實作的下游 Agent**；待回測通過 | PipelineRun、D-Plan Builder／Validator、每日／失敗報告、通知與人工批准閘門 |
| 10 | 市場情緒與分析師研究 Agent | 第一版後選配 | 獨立來源評估、情緒與共識變化訊號，不改寫核心資料或直接產生交易 |

目前只進入 M1；M1 驗收未通過前，不開始事件研究 Agent。四個下游 Agent 必須依 `事件研究 → 動能／配置／風控 → 回測 → 自動化排程／報告` 順序開發，不平行跳過驗收。

詳細規則見 [Data Agent 計畫](data_agent_plan.md)、[資料來源可行性測試](source_feasibility_2026-09-17.md)、[事件研究 Agent 計畫](event_strategy_v1.md)、[動能／配置／風控 Agent 計畫](momentum_portfolio_risk_agent_plan.md)、[回測 Agent 計畫](backtest_agent_plan.md)、[回測與驗證方法規格](backtest_plan_v1.md)及[自動化排程／報告 Agent 計畫](automation_reporting_agent_plan.md)。
