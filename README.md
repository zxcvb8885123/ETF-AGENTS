# ETF Agent Manager

AI CUP 2026「Agent 基金經理人」的自動化 Agent。目標是每天完成資料蒐集、投資決策、組合／資金風控，最後產生可提交的決策報告與交易書。

```text
資料蒐集 → 研究 → 多子 Agent 買賣裁決 → 確定性配置／風控 → 報告與 D-Plan 候選檔 → 人工檢視
```

## 快速開始

```bash
./start.sh
```

這會建立 Docker 映像、初始化 SQLite、抓取 TWSE／TPEx 最新行情、增量更新兩年歷史行情，再顯示資料狀態。

| 指令 | 用途 |
| --- | --- |
| `./start.sh` | 自動模式：有官方交易池時抓正式資料，否則抓開發資料 |
| `./start.sh official` | 只抓官方交易池；名單空白時停止 |
| `./start.sh all` | 開發模式：抓 TWSE 最新行情端點的全部可解析證券 |
| `./start.sh check` | 只建置、檢查與執行測試 |
| `./start.sh daily` | 一鍵驗證來源、更新行情／事件並建立 `artifacts/research_snapshot_latest.json` |
| `./start.sh report` | 使用既有 Snapshot 執行或續跑報告工作流，結果在 `artifacts/reports/latest.md` |
| `PYTHONPATH=src python3 scripts/probe_data_sources.py` | 探測 TWSE／TPEx 最新行情並驗證 150 檔交易池 |
| `PYTHONPATH=src python3 scripts/collect_latest_prices.py` | 抓取官方交易池的 TWSE／TPEx 最新行情 |
| `PYTHONPATH=src python3 scripts/collect_official_history.py` | 以官方 TWSE／TPEx 月行情增量更新日線；先驗證最近完整交易日，並輸出逐檔覆蓋 JSON |
| `python3 scripts/run_strategy.py --input snapshot.json` | 以研究快照執行事件策略 V1 |
| `.venv/bin/python scripts/collect_history.py` | 透過 yfinance 增量更新 150 檔最近兩年日線至最近完整官方交易日 |
| `.venv/bin/python cli/data_agent.py collect` | 抓取官方月營收與重大訊息 |
| `.venv/bin/python cli/data_agent.py snapshot --output artifacts/research_snapshot_latest.json` | 建立目前時間的研究快照 |
| `.venv/bin/python cli/event_research.py status` | 確認事件研究 Snapshot 與證據可用 |
| `.venv/bin/python cli/event_research.py list-events --lookback-days 45` | 列出截止時間前的事件候選 |
| `.venv/bin/python cli/event_research.py analyze-event-context --evidence-id ID` | 建立比較基準、新穎性、相關事件與事件相對行情資料包 |
| `.venv/bin/python cli/event_research.py validate-debate --input BUNDLE.json` | 驗證 Fact／Bull／Bear／Adjudicator 子 Agent 輸出與獨立依賴 |
| `.venv/bin/python cli/event_research.py validate-result --input RESULT.json` | 驗證事件研究結果、正式引用與 cutoff |
| `.venv/bin/python cli/sentiment_research.py status` | 檢查市場情緒與分析師資料包的來源及覆蓋 |
| `.venv/bin/python cli/sentiment_research.py validate-result --input RESULT.json` | 重算並驗證市場情緒、共識修正與引用 |
| `.venv/bin/python cli/fundamental_research.py --snapshot SNAPSHOT.json status` | 檢查財報 Snapshot、一般業範圍與資料缺口 |
| `.venv/bin/python cli/fundamental_research.py build-bundle ...` | 從固定 Snapshot 建立基本面資料包與覆蓋分母 |
| `.venv/bin/python cli/fundamental_research.py compute-metrics ...` | 重算基本面指標及其來源依賴 |
| `.venv/bin/python cli/fundamental_research.py validate-result ...` | 驗證基本面研究草稿、引用與內容雜湊 |
| `.venv/bin/python cli/research_report.py build` | 將已驗證研究結果建立成 ResearchReport JSON 與 Markdown |
| `.venv/bin/python cli/portfolio_decision.py --bundle INPUT.json validate-input` | 驗證 Portfolio Decision 共用輸入、cutoff 與版本雜湊 |
| `.venv/bin/python cli/portfolio_decision.py --bundle INPUT.json compute-momentum` | 確定性計算動能、市場寬度與 regime |
| `.venv/bin/python cli/portfolio_decision.py --bundle INPUT.json compute-proposal --momentum MOMENTUM.json --debate DEBATE.json --intent INTENT.json --policy POLICY.json` | 重新驗證裁決後，以一張（1,000 股）為單位計算配置、訂單、費稅與現金 |
| `.venv/bin/python cli/portfolio_decision.py --bundle INPUT.json compute-scenarios --policy POLICY.json --proposal PROPOSAL.json` | 建立價格與流動性壓力情境 |
| `.venv/bin/python cli/portfolio_decision.py --bundle INPUT.json compute-guard --policy POLICY.json --proposal PROPOSAL.json --scenario SCENARIO.json` | 檢查交易池、可交易性、現金、曝險及全部基準 Active Share |
| `PYTHONPATH=src python3 cli/account_data.py import --input ACCOUNT.json --cutoff ISO_TIME --run-id RUN_ID --mode fixture` | 匯入標準帳戶 JSON 並封存原始檔與雜湊；正式模式須先在 `config/account_sources.json` 核准 provider／版本 |
| `PYTHONPATH=src python3 cli/account_data.py reconcile --account ACCOUNT_BUNDLE.json --snapshot SNAPSHOT.json --max-nav-drift-rate RATE --output RECONCILIATION.json` | 以同一 cutoff Snapshot 重算 NAV、現金及價格覆蓋，另產 Markdown 對帳報告 |
| `PYTHONPATH=src python3 cli/account_data.py export-decision-account --account ACCOUNT_BUNDLE.json --reconciliation RECONCILIATION.json --snapshot SNAPSHOT.json --output DECISION_ACCOUNT.json` | 僅在正式來源且通過對帳、可無損轉換時匯出決策帳戶欄位 |
| `PYTHONPATH=src python3 cli/virtual_account.py --account-id ai-cup-2026 init --started-at ISO_TIME` | 只用一次設定本金建立 10 億 TWD 虛擬帳戶 |
| `PYTHONPATH=src python3 cli/virtual_account.py --account-id ai-cup-2026 prepare-day --snapshot SNAPSHOT.json --run-id RUN_ID --account-output ACCOUNT.json` | 續接前帳本、結算到期款項並建立決策前帳戶快照 |
| `PYTHONPATH=src python3 cli/virtual_account.py --account-id ai-cup-2026 attach-account --template BUNDLE.json --account-snapshot ACCOUNT.json --snapshot SNAPSHOT.json --output DECISION_INPUT.json` | 綁定虛擬帳戶並完整驗證決策輸入包 |
| `PYTHONPATH=src python3 cli/virtual_account.py --account-id ai-cup-2026 apply-decision --decision-run-id RUN_ID --execution-market EXECUTION.json --close-market CLOSE.json --settlement-date YYYY-MM-DD --run-id CLOSE_RUN` | 驗證已封存 Decision run，模擬成交並保存日終虛擬帳本 |
| `.venv/bin/python cli/daily_report.py run ...` | 驗證封存 Decision run，建立 DailyReport 或 FailureReport |
| `.venv/bin/python cli/report_workflow.py run` | 封存事件候選，等待／接收已驗證研究結果並交付 Research Report；提供 Decision run 後可接 DailyReport |

自動化報告 Agent 已完成 RPT0～RPT4 的第一版：`daily` 會建立 Snapshot 並封存報告工作流；`report` 可使用既有 Snapshot 續跑；結果固定交付至 `artifacts/reports/latest.md`。沒有研究 Agent 輸出時會留下 `waiting_for_agent`；Research Report 完成但沒有同一 Snapshot／cutoff 的 Decision／Risk 時會留下 `waiting_for_decision`；不會捏造報告或繞過風控。

## 目前完成

- SQLite：保存行情、原始 TWSE 回應、抓取時間及執行紀錄。
- TWSE／TPEx 最新交易日行情收集器。
- 官方 TWSE／TPEx 歷史行情增量 CLI：新標的補完整期間、既有標的回抓重疊區間、保留原始回應與逐檔覆蓋報告；Yahoo 日線維持備援。
- TWSE／TPEx 月營收與重大訊息收集、原始回應保存及版本去重。
- 指定截止時間的不可變研究快照與資料品質旗標。
- 可重跑的 TWSE／TPEx 來源健康探測與結構化可行性報告。
- 150 檔交易池逐檔驗證、代號承接候選及 Snapshot fail-closed 閘門。
- 官方交易池 CSV 讀取與篩選。
- 事件研究 Agent `ResearchResult` 2.1：主控加 Fact／Bull／Bear／Adjudicator 子 Agent Skills、獨立多空 DebateBundle、財務傳導鏈、事件相對行情及雙重 fail-closed validator。
- 市場情緒與分析師研究 Agent MVP：`PerceptionDataBundle`、逐筆情緒標籤、去重聚合、分析師共識修正、事件預期差、`MarketPerceptionResult` validator、Skill 與 CLI。
- 基本面研究 Agent FR0～FR3 fixture MVP：固定 Snapshot 的 `FundamentalDataBundle`、Decimal 指標重算、`FundamentalResearchResult` validator、CLI、Skill 與不覆寫封存；一般業目前支援營業利益率、負債占資產比率、營收／淨利同比及營業利益率年差。
- Research Report V0：整合 Snapshot、事件研究與選配市場認知結果，產生同源、可重建驗證且不含交易建議的 JSON／Markdown 報告。
- Portfolio Decision P0～P6 fixture 驗收：共用輸入、動能、獨立買賣裁決、確定性整張配置／訂單／費稅、部分成交情境重建、必備基準 Guard、完整修正鏈重播及磁碟封存驗證。
- 事件策略 V1：事件評分、價格確認及進攻／防守配置。
- 競賽基本風控：持股檔數、現金、個股權重、交易池與 Active Share。
- Docker 與快速啟動流程。

事件研究 Agent 可研究目前 Snapshot 中的月營收與重大訊息；MoM／YoY 只作歷史基準，不能直接等同市場預期或方向。市場情緒與分析師研究 Agent 已完成契約與 fixture 驗證，但真實社群／券商資料仍須通過授權、歷史版本與時間點可得性審查。目前可將已保存且已驗證的研究 artifact 建立成 Research Report V0；2026-09-22 已完成一次 3 件真實事件的可重建演練，因沒有合法、歷史化市場認知資料而降級，且三件均未成為交易候選。Portfolio Decision 已完成 P0～P6 fixture 驗收，可把已驗證裁決轉成整張配置、模擬訂單、依成交重建的情境、風控、完整修正歷程與最終結果。回測 Agent B0～B2 fixture MVP 已能以歷史時鐘重播決策、模擬整張成交、交割、公司行動與帳務，並封存可重建的帳務驗收結果；每日虛擬帳本 VA1～VA3 fixture 工具鏈現已具備 10 億 TWD 唯一開帳、決策前帳戶快照、完整 Decision run 驗證、模擬成交與日終封存。每日報告工作流尚未自動串接帳本；交易狀態官方來源核准、150 檔真實覆蓋、有效競賽規則、真實歷史／前向回測及正式排程仍未完成。架構不設「主辦平台送件／交易執行 Agent」；系統交付報告與已驗證候選檔，平台送件與交易由人工在系統外處理，人工確認也不會觸發自動送件或下單。

Data Agent M0 已完成；M1 的 TPEx 最新行情與官方歷史行情 CLI 已接入，目前接續財報彙總、交易狀態與細粒度工具。官方歷史 CLI 只驗證保存區間與終止日覆蓋；尚無版本化交易日曆，不能宣稱期間內每個交易日完整，也不能用於正式歷史回測。之後才依序進行新聞候選（M2）與    Codex／Claude Skill 工具循環（M3）。官方 2026-09-14 版交易池已將 `5371 中光電` 更新為 `3718 中光電投控`，設定檔同步完成。

基本面研究 Agent 的 FR0～FR3 fixture MVP 已完成；它從固定 Snapshot 整理一般業財報、重算確定性財務比率並驗證有引用的研究解讀，不產生交易候選、權重或訂單。尚未完成 FR4 真實資料演練、FR5 下游契約升版，以及毛利率、現金流品質、估值與金融業公式；原 M1 交易狀態與資料主線仍需完成。

## 資料位置

目前 M1 官方交易狀態已完成 TS1～TS4 的契約、固定 cutoff 重建、SQLite migration、CLI 與 Guard adapter；[M1 官方交易狀態接入](docs/trading_status_m1_plan.md) 的 TS0 來源核准與 TS5 150 檔真實覆蓋仍未完成。資料不足時阻擋正式決策，保留可用研究資料。

P3～P6 的 [實作紀錄與邊界](docs/momentum_portfolio_risk_agent_plan.md#p3p6-實作紀錄2026-09-21fixture-驗收已完成) 已更新；P7 B0～B2 fixture 回測帳務驗收也已完成。下一批是 B3 策略比較與 B4 Agent 評估；正式資料接入與規則版本仍需另外確認。

| 路徑 | 用途 |
| --- | --- |
| `var/etf_agent.db` | SQLite 資料庫 |
| `data/official_universe.csv` | 官方 150 檔交易池，公布後填入 |
| `data/active_etf_top10.csv` | Active Share 的 ETF 前十大持股資料 |
| `artifacts/` | 後續每日報告、交易書與稽核檔案 |

資料庫查詢、Docker 指令與容器設定請參閱下方的 Docker 使用說明。

## 文件

[P6 決策驗收與風控補強](docs/decision_acceptance_plan.md)與 P7 B0～B2 fixture 回測帳務驗收均已完成。正式 Provider 尚未接妥，不能把 fixture 結果視為正式交易驗收或策略績效。

| 文件 | 內容 |
| --- | --- |
| [Agent 開發架構](docs/agent_plan.md) | 資料庫、事件與動能策略、進攻／防守分類、風控買賣及報告流程 |
| [Data Agent 計畫](docs/data_agent_plan.md) | 資料收集、補查、驗證、版本保存與研究快照 |
| [M1 第二批：官方財報彙總接入](docs/financial_statements_m1_plan.md) | 已實作：24 個官方端點、業別契約、版本保存、Snapshot 與 CLI；2026 Q2 實測 298/300，3718.TWO 缺兩張報表而降級 |
| [M1 下一批：官方交易狀態接入](docs/trading_status_m1_plan.md) | **部分完成**：契約、Parser、Bundle／Assessment Validator、SQLite、CLI 與 Guard 已完成；官方來源核准與 150 檔實測待完成 |
| [資料來源可行性測試](docs/source_feasibility_2026-09-17.md) | 官方行情、財報、事件與新聞來源的實測結果及接入判定 |
| [第一版技術架構](docs/architecture_v1.md) | 模組職責、資料契約、流程及實作里程碑 |
| [事件研究 Agent 計畫](docs/event_strategy_v1.md) | 第一個下游 Agent；事件證據、補查、引用與研究結果 |
| [市場情緒與分析師研究 Agent 計畫](docs/sentiment_analyst_agent_plan.md) | 市場情緒、共識修正、預期差、資料授權與時間點驗證 |
| [基本面研究 Agent 計畫](docs/fundamental_research_agent_plan.md) | FR0～FR3 fixture MVP：財報研究、確定性比率、時間與引用驗證；有限演練及下游升版待完成 |
| [Research Report V0 計畫](docs/research_report_plan.md) | 將研究層輸出整合為可稽核 JSON／Markdown，不包含交易決策 |
| [本地 Agent 真實資料研究演練](docs/local_research_dry_run_plan.md) | 已完成：固定快照、3 件事件獨立研究、雙重驗證、降級 Research Report 與決策缺口清單 |
| [投資組合買賣決策與風控多子 Agent 計畫](docs/momentum_portfolio_risk_agent_plan.md) | Portfolio Decision 主控、五個子 Agent、確定性配置／訂單及競賽風控 |
| [回測 Agent 計畫](docs/backtest_agent_plan.md) | 第三個下游 Agent；歷史重播、模擬成交、Agent 評估與前向驗證 |
| [P7 回測 Agent 第一批計畫](docs/backtest_mvp_plan.md) | B0～B2 fixture MVP 已完成：歷史時鐘、時間點資料、整張成交、交割與多日帳務重播 |
| [回測與驗證方法規格](docs/backtest_plan_v1.md) | 資料切分、成交假設、策略比較與有效性判定方法 |
| [自動化排程／報告 Agent 計畫](docs/automation_reporting_agent_plan.md) | 第四層下游 Agent；執行紀錄、每日／失敗報告、D-Plan 候選檔與排程；交付人工檢視 |
| [一鍵研究與報告交付計畫](docs/report_delivery_agent_plan.md) | RPT0～RPT4：資料／cutoff、研究交接、續跑、固定格式交付與 DailyReport 接線 |
| [真實研究續跑與決策報告驗收](docs/report_workflow_acceptance_plan.md) | W0～W5 已完成一件真實事件驗收；降級 Research Report 可重建，DailyReport 等待正式決策輸入 |
| [十億虛擬帳戶與每日買賣決策計畫](docs/virtual_account_daily_decision_plan.md) | VA1～VA3 fixture 工具鏈已完成；VA4 報告工作流接線、VA5 真實資料與前向驗收待完成 |
| [外部帳戶結算檔匯入與對帳計畫](docs/account_data_integration_plan.md) | 選配支線；AC1～AC4 fixture 工具鏈已完成，正式來源核准清單仍為空 |
| [Docker 使用說明](docs/docker.md) | 建置、容器指令、掛載與疑難排解 |

自動化排程／報告 Agent 已完成 A0／A1 的 fixture／離線實作；按需研究交付 RPT0～RPT4 已接入 `start.sh daily`／`start.sh report`，可在提供同一 Snapshot／cutoff 的 Decision／Risk run 後呼叫既有 DailyReport／FailureReport。D-Plan、正式排程與平台送件仍未接入；入口與輸入要求見[自動化排程／報告 Agent 計畫](docs/automation_reporting_agent_plan.md)。

## 專案結構

```text
config/                 競賽與資料來源設定
data/                   官方交易池與 ETF 基準資料
docs/                   規劃、架構與操作文件
cli/                    Agent、人工與排程共用的穩定 CLI 入口
scripts/                初始化、收集與狀態查詢維運指令
skills/                 Codex／Claude 工作流程、契約參考與 UI metadata
src/etf_agent/          Agent 核心程式、runtime 與報告 Builder
tests/                  單元測試與測試資料
var/                    SQLite 資料庫（不納入 Git）
artifacts/              每日輸出檔案（不納入 Git）
```
