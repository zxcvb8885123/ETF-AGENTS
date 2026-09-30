# ETF Agent Manager

AI CUP 2026「Agent 基金經理人」的台股 ETF Agent 系統。每天完成資料蒐集、研究、買賣決策、組合與資金風控，最後產生供人工檢視的決策報告與 D-Plan 候選檔。

```text
資料蒐集 → 分析團隊 → 重大事件研究 → 多空研究 → 交易 → 風險 → 確定性配置／Guard → 報告與 D-Plan 候選檔 → 人工檢視
```

設計原則是「確定性歸程式，不確定性歸 LLM」：指標、權重、張數、費稅、現金與競賽規則全部由 Python 計算並由 Validator 重算；子 Agent 只輸出有限選項的判斷與引用證據。系統**不自動下單、不送件**，平台送件與交易由人工在系統外處理。

## 快速開始

```bash
./start.sh
```

首次執行會建立 Docker 映像；之後只有映像不存在、依賴或 Dockerfile 變更才重建。腳本會初始化 SQLite、抓取 TWSE／TPEx 最新行情、增量更新兩年歷史行情，再顯示資料狀態。程式碼直接從工作區掛載進容器，修改 Python 不需重建映像。

本機開發或驗證 Skill 時使用與 Docker 相同的依賴：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

`requirements.txt` 同時包含執行依賴與 Skill 驗證所需的 PyYAML。修改依賴後執行 `docker compose build` 更新映像。完整驗證指令見 [AGENTS.md](AGENTS.md#測試與驗證)，Docker 細節見 [Docker 使用說明](docs/docker.md)。

## 每日操作

### 1. 第一次使用：建立虛擬帳戶

每個帳戶只能開一次，績效從開帳時間起算（`ai-cup-2026` 已於 2026-09-28 16:31:51 +08:00 以 10 億 TWD 開帳）：

```bash
PYTHONPATH=src python3 cli/virtual_account.py --account-id ai-cup-2026 init --started-at 2026-09-28T16:31:51+08:00
```

### 2. 每日一鍵流程

```bash
.venv/bin/python scripts/run_daily_pipeline.py
```

依序執行：執行前檢查（Docker、`claude` 登入、帳戶、必要檔案、磁碟，任一失敗即在呼叫 Agent 前停止）→ 封存交易狀態 CSV → `./start.sh daily`（資料、Snapshot、虛擬帳本）→ 交易狀態包 → 分析團隊 → 重大事件研究 → 多空研究 → 交易 → 風險（子 Agent 經 `claude -p`，結構化輸出並由 Validator 驗證，一般日約 20～40 次）→ 封存 Decision run（含 `team_inputs`）→ macOS 通知。每日流程不產生 DailyReport；對外交付由 `cli/dplan.py` 從封存 Decision run 匯出 D-Plan。

| 選項 | 用途 |
| --- | --- |
| `--force` | 休市日（週末或官方開休市日期表列出者）或當日已完成仍執行；找不到封存日曆時也須以此執行一次以建立封存 |
| `--skip-data` | 沿用既有 Snapshot 與帳戶快照，不重抓資料 |
| `--resume` | 沿用今天最近一個未完成 run 目錄中已驗證的 Agent 輸出與同一個 decision run ID（例如撞到用量上限後），建議搭配 `--skip-data` |
| `--preflight-only` | 只執行執行前檢查並列出每一項結果；安裝排程前先跑一次 |
| `--min-free-gb` | 執行前檢查的最低可用磁碟空間，預設 2 GiB |
| `--model` | 指定子 Agent 模型；預設沿用 `claude` CLI 設定 |
| `--max-budget-usd` | 單次子 Agent 估算用量上限（防失控；claude.ai 訂閱登入不另計費） |

結果位置：

- 執行摘要：`artifacts/daily_runs/<run_id>/pipeline.json`（含交易狀態 allowed／blocked／unknown 數、決策狀態、委託數與 Agent 呼叫）
- 報告：`artifacts/reports/latest.md`
- 封存決策：`artifacts/portfolio_decisions/<decision_run_id>/`

排程樣板見 `scripts/launchd/`（尚未正式啟用）。

### 3. 收盤後結算

```bash
PYTHONPATH=src python3 cli/virtual_account.py --account-id ai-cup-2026 settle
```

依比賽帳務口徑，於 cutoff 後第一個交易日**全部成交**，成交價為 TWSE／TPEx 官方**當日成交均價**（成交金額 ÷ 成交股數，四捨五入到 0.01 元），不設成交量上限；日終以官方收盤價估值。當日無成交的股票沒有均價，視為不可成交；現金不足時仍依買力減少張數。依官方開休市日曆計算 T+2 交割，找不到封存日曆即停止；當日行情未公布回 `waiting_for_close_data`。`./start.sh daily` 會先 `settle` 再 `prepare-day`。

### 4. 開啟績效儀表板（網頁）

```bash
./start.sh dashboard
```

瀏覽器開啟 <http://127.0.0.1:8000>，`Ctrl+C` 關閉。儀表板唯讀，顯示起始本金、目前 NAV、今日／累積報酬率、現金、持倉與每日紀錄；`/api/performance` 提供同一份 JSON。只讀已驗證的虛擬帳本，驗證失敗回 409。選股與研究內容不在儀表板，請看 `artifacts/reports/latest.md` 與 run 目錄。

### `start.sh` 模式

| 指令 | 用途 |
| --- | --- |
| `./start.sh` | 自動模式：有官方交易池時抓正式資料，否則抓開發資料 |
| `./start.sh official` | 只抓官方交易池；名單空白時停止 |
| `./start.sh all` | 開發模式：抓 TWSE 最新行情端點的全部可解析證券 |
| `./start.sh check` | 只建置、檢查與執行測試 |
| `./start.sh daily` | 驗證來源、更新行情／事件／最近到期季度財報（覆蓋不完整不阻擋）、推進虛擬帳本並建立 `artifacts/research_snapshot_latest.json` |
| `./start.sh dashboard` | 啟動唯讀績效儀表板 |

## 目前進度

### 已完成

- **資料層**：SQLite 保存行情、原始回應、抓取時間與執行紀錄；TWSE／TPEx 最新行情與官方歷史行情增量 CLI（Yahoo 日線為備援）；月營收、重大訊息與官方財報彙總（24 個端點，2026 Q2 實測 298/300）；150 檔交易池逐檔驗證與 Snapshot fail-closed 閘門；產業分類 150／150。
- **交易狀態與交易日曆**：TS1～TS4 契約、Parser、Validator、SQLite、CLI 與 Guard；政府開放 CSV 確定性映射。官方開休市日曆（`TradingCalendar`）統一用於每日是否執行、目標交易時段與帳本 T+2 交割，區分休市日與「僅辦理結算交割」日；日曆缺漏或未涵蓋年度時停止。**2026-09-28 已核准七個政府開放來源**（時效 72 小時），TWSE「管理股票」依證交所營業細則第 52 條列為不適用；9/28 實跑結果 148 檔 allowed、2 檔 blocked（處置）、unknown 0。
- **研究層**：事件研究 Agent（Fact／Bull／Bear／Adjudicator、雙重 validator）；市場情緒與分析師 Agent MVP（無核准來源時 `unavailable`）；基本面 FR0～FR3 fixture MVP；Research Report V0。
- **每日情緒／共識與帳戶接入**：已支援授權資料包逐筆標註、全池聚合、決策與報告接線；帳戶補上原價估值、cutoff、缺行情等待與重跑重用。真實情緒／共識資料商尚未接入，見[操作與限制](docs/daily_perception_account_integration.md)。
- **決策層新鏈（R1～R6）**：分析團隊（技術／基本面／事件／情緒）→ 重大事件研究 → 多空研究員 → 交易 Agent → 風險 Agent 現金姿態與分級 → 確定性配置、情境、CompetitionGuard → 封存與重建驗證。舊版 Portfolio Decision 1.0 鏈（P0～P6）只保留供封存 run 重建與 fixture 測試。前一交易日未成交缺口由帳本整理後逐檔交給交易 Agent（不自動補單，是否再 buy／add 由 Agent 決定）；風險審查另收到程式計算的各產業實際權重。
- **帳務與回測**：虛擬帳本 VA1～VA3（唯一開帳、決策前帳戶快照、模擬成交、日終封存）；回測 B0～B2 fixture（歷史時鐘、整張成交、交割、公司行動）；B3 fixture 策略比較第一版（同一 `BacktestRequest` 重播多組逐日輸入，計算報酬、回撤、成本與 24 交易日視窗，可重建驗證，結果固定標示 `evidence_status=insufficient`）；外部帳戶匯入 AC1～AC4 fixture 工具鏈。
- **交付**：D-Plan v4.0 候選匯出與本地結構／引用鏈檢查（匯出前核對 Decision run 的帳戶快照與帳戶目前 latest 的 prepare-day 封存一致）、唯讀績效儀表板。DailyReport／FailureReport 與報告工作流已於 2026-09-29 移除（比賽只需 D-Plan），舊封存仍留在 `artifacts/report_runs/` 供稽核。

### 第一次真實資料決策（2026-09-28，目標交易日 9/29）

整條新鏈以真實資料跑完，Decision run `decision-20260928T084128Z` **approved、30 筆買進委託**（現金 11.5%、周轉率 88%、現金姿態 neutral）。

第一次執行曾被拒絕：提案通過全部規則，但 `liquidity_stress` 壓力情境（成交率 50%、滑價 2 倍）下現金比例 60% 超過 25% 上限；從全現金建倉時這個情境結構上無法通過。現已改為：現金上限只在頂層 `CASH_WEIGHT` 與 `base` 情境為硬性規則，壓力情境現金超標只記警告，其餘情境檢查仍為硬性。被拒絕的封存與舊報告移至各 repository 的 `.superseded/` 保留稽核，未刪除。風險 Agent 留下兩項未解風險：產業實際權重未提供給審查、部分成交後的補單規則尚未納入下一交易日流程。

### 尚未完成

- 虛擬帳本 VA4／VA5 自動化與跨日真實資料驗收；正式排程啟用。步驟見[每日自動化與回測就緒計畫](docs/automation_backtest_readiness_plan.md)。
- D-Plan：Decision run 到「來源→事實→市場姿態→全持股決策」的完整映射、真實資料端到端演練；主辦方伺服器語意驗證（`verify_dplan.py`）未取得，本地檢查不等同平台驗證。
- 策略說明書（ETF 名稱、投資主題、投資理念）：繳交期間 2026-10-21 至 10-26。
- 競賽規則待釐清：現金上限 `<25%` 與 Schema `≤25%` 的邊界、提交時間（設定 19:30 與 Schema 05:00–08:55）、min successful days。
- 回測 B3 的六組正式策略、績效貢獻、不確定性與樣本外分析；B4 Agent 評估；真實歷史回測（缺版本化歷史交易日曆與歷史交易狀態）。
- 基本面 FR4 真實演練、FR5 下游契約升版，毛利率、現金流、估值與金融業公式。
- 合法且歷史化的市場情緒／分析師資料來源；FinMind 等多來源 Provider（見[多來源更新計畫](docs/data_agent_multisource_update_plan.md)）。
- 選配：`data/active_etf_top10.csv` 主動式 ETF 持股權重（目前空白；D-Plan 指南未將 Active Share 列為每日硬性上限，不阻擋決策）。

MoM／YoY 只是歷史基準，不能等同市場預期或單獨形成方向。fixture 驗收結果不能視為正式交易驗收或策略績效。

## 指令參考

以下是各 Agent 的單步 CLI；每日流程已串接，除錯或重建時才需要單獨呼叫。

### 資料

| 指令 | 用途 |
| --- | --- |
| `PYTHONPATH=src python3 scripts/probe_data_sources.py` | 探測 TWSE／TPEx 最新行情並驗證 150 檔交易池 |
| `PYTHONPATH=src python3 scripts/collect_latest_prices.py` | 抓取官方交易池的 TWSE／TPEx 最新行情 |
| `PYTHONPATH=src python3 scripts/collect_official_history.py` | 以官方月行情增量更新日線；先驗證最近完整交易日，輸出逐檔覆蓋 JSON |
| `.venv/bin/python scripts/collect_history.py` | 透過 yfinance 增量更新最近兩年日線（備援） |
| `.venv/bin/python scripts/collect_sector_classification.py` | 抓取官方公司基本資料（t187ap03），輸出 150 檔 `industry:<代碼>` 至 `data/sector_classification.json`；缺漏時 exit 2 |
| `.venv/bin/python cli/data_agent.py collect` | 抓取官方月營收與重大訊息 |
| `.venv/bin/python cli/data_agent.py snapshot --decision-cutoff ISO --output PATH` | 建立指定 cutoff 的研究快照 |

### 交易狀態

| 指令／檔案 | 用途 |
| --- | --- |
| `config/trading_status_approvals.json` | 來源核准清單與不適用政策；封存超過 `max_age_hours` 時 coverage 退為 `STALE_CAPTURE`，Guard 仍 fail-closed |
| `.venv/bin/python scripts/capture_trading_status_candidates.py` | 封存八份政府開放 CSV 及 HTTP／內容證據（每日流程自動執行） |
| `.venv/bin/python scripts/normalize_trading_status_candidates.py --manifest M --output O` | 由封存原檔重建固定 150 檔的候選事實 |
| `PYTHONPATH=src python3 cli/trading_status.py build-bundle --snapshot S --session-start ISO --session-end ISO --output O` | 建立交易狀態包；未核准來源逐檔 `unknown` |

### 研究

| 指令 | 用途 |
| --- | --- |
| `.venv/bin/python cli/event_research.py status`／`list-events --lookback-days 45` | 確認 Snapshot 證據、列出 cutoff 前事件候選 |
| `.venv/bin/python cli/event_research.py analyze-event-context --evidence-id ID` | 建立比較基準、新穎性、相關事件與事件相對行情資料包 |
| `.venv/bin/python cli/event_research.py validate-debate --input B.json`／`validate-result --input R.json` | 驗證子 Agent 輸出、獨立依賴、正式引用與 cutoff |
| `.venv/bin/python cli/sentiment_research.py status`／`validate-result --input R.json` | 市場情緒與分析師資料包檢查與重算驗證 |
| `.venv/bin/python cli/fundamental_research.py --snapshot S status`／`build-bundle`／`compute-metrics`／`validate-result` | 基本面資料包、確定性比率與研究草稿驗證 |
| `.venv/bin/python cli/research_report.py build` | 將已驗證研究結果建立成 ResearchReport JSON 與 Markdown |

### 決策與風控

| 指令 | 用途 |
| --- | --- |
| `cli/portfolio_decision.py --bundle T.json build-input --research R.json [--trading-status S.json]` | 由 Snapshot、SQLite 歷史行情（預設 120 根）與 `config/decision_rules.json` 建立 DecisionInputBundle 樣板 |
| `cli/portfolio_decision.py --bundle I.json validate-input`／`compute-momentum` | 驗證共用輸入；計算動能、市場寬度與 regime |
| `cli/portfolio_decision.py --bundle I.json build-policy --output P.json` | 由策略樣板、硬性規則與產業分類建立 DecisionPolicy |
| `cli/portfolio_decision.py --bundle I.json validate-sizing ...`／`apply-sizing ...` | 驗證風險 Agent 的等級與現金姿態（暫定 aggressive／neutral／defensive 對應 3%／10%／20%）；權重依等級乘數 ÷ ATR14% 分配 |
| `cli/portfolio_decision.py --bundle I.json compute-proposal ...` | 以一張（1,000 股）為單位計算配置、訂單、費稅與現金 |
| `cli/portfolio_decision.py --bundle I.json compute-scenarios ...`／`compute-guard ...` | 價格與流動性壓力情境；交易池、可交易性、現金與曝險 Guard（基準 Active Share 選配） |
| `cli/portfolio_decision.py build-role-brief --role-input R.json --output B.json` | 產生子 Agent 可讀的精簡角色摘要 |

上表指令皆以 `.venv/bin/python` 執行。

### 帳戶

| 指令 | 用途 |
| --- | --- |
| `cli/virtual_account.py --account-id ID init --started-at ISO` | 一次性建立 10 億 TWD 虛擬帳戶 |
| `cli/virtual_account.py --account-id ID prepare-day --snapshot S --run-id R --account-output A` | 續接前帳本、結算到期款項並建立決策前帳戶快照 |
| `cli/virtual_account.py --account-id ID attach-account --template B --account-snapshot A --snapshot S --output O` | 綁定虛擬帳戶並驗證決策輸入包 |
| `cli/virtual_account.py --account-id ID apply-decision ...`／`settle`／`daily ...` | 模擬成交並保存日終帳本；`daily` 先 settle 再 prepare-day，待結算決策未收盤時不覆蓋 |
| `cli/account_data.py import`／`reconcile`／`export-decision-account` | 外部帳戶結算檔匯入、對帳與匯出（正式來源核准清單仍空） |
| `cli/dashboard.py --account-id ai-cup-2026 --port 8000` | 直接啟動 FastAPI 儀表板 |

上表指令皆以 `PYTHONPATH=src python3` 執行。

### 報告與 D-Plan

| 指令 | 用途 |
| --- | --- |
| `.venv/bin/python cli/dplan.py build ... --virtual-account-account-id ID --virtual-account-run-id PREPARE_RUN`／`validate --input D-Plan.json` | D-Plan v4.0 候選匯出（須指定決策使用的 prepare-day 帳本 run）與本地結構／引用鏈檢查（不等同主辦方伺服器驗證） |
| `.venv/bin/python cli/backtest.py --request REQ.json compare-strategies --strategies S.json --baseline ID --output C.json` | B3 fixture 策略比較；`validate-comparison` 加 `--input C.json` 重播檢查。介面見[回測 Agent 計畫](docs/backtest_agent_plan.md) |
| `python3 scripts/run_strategy.py --input snapshot.json` | 原型事件策略 V1（僅供對照，不在正式決策路徑） |

## 資料位置

| 路徑 | 用途 |
| --- | --- |
| `var/etf_agent.db` | SQLite 資料庫（不納入 Git） |
| `data/official_universe.csv` | 主辦方 150 檔股票交易池（上市 100、上櫃 50；2026-09-14 版已將 5371 更新為 3718） |
| `data/sector_classification.json` | 官方產業代碼分類（2026-09-25 版 150／150） |
| `data/active_etf_top10.csv` | 選配 Active Share 比較用 ETF 前十大持股（目前空白） |
| `config/trading_status_approvals.json` | 交易狀態來源核准清單 |
| `artifacts/daily_runs/` | 每日流程 run 目錄與 Agent 原始輸出 |
| `artifacts/portfolio_decisions/` | 封存的 Decision run |
| `artifacts/virtual_accounts/` | 虛擬帳本 |
| `artifacts/reports/latest.md` | 最新報告 |
| `artifacts/source-audit/` | 交易狀態 CSV 等來源稽核封存 |

`artifacts/` 不納入 Git。

## 文件

| 文件 | 內容 |
| --- | --- |
| [決策層 Agent 團隊重構計畫](docs/agent_team_refactor_plan.md) | **R1～R6 已完成**：分析團隊 → 重大事件研究 → 多空研究員 → 交易 Agent → 風險 Agent |
| [M1 官方交易狀態接入](docs/trading_status_m1_plan.md) | 契約、Parser、Validator、SQLite、CLI 與 Guard |
| [TS0 來源核准行動計畫](docs/source_audit/2026-09-25_ts0_approval_plan.md) | 政府開放 CSV 查證、確定性映射與 2026-09-28 核准紀錄 |
| [來源稽核 9/24](docs/source_audit/2026-09-24_findings.md)／[9/25](docs/source_audit/2026-09-25_findings.md) | 官方端點回應、樣本與就緒判定 |
| [Agent 開發架構](docs/agent_plan.md) | 資料庫、策略、風控買賣及報告流程 |
| [第一版技術架構](docs/architecture_v1.md) | 模組職責、資料契約、流程及里程碑 |
| [Data Agent 計畫](docs/data_agent_plan.md) | 資料收集、驗證、版本保存與研究快照 |
| [Data Agent 多來源更新計畫](docs/data_agent_multisource_update_plan.md) | FinMind 等 Provider（待實作） |
| [事件研究 Agent 計畫](docs/event_strategy_v1.md) | 事件證據、補查、引用與研究結果 |
| [市場情緒與分析師研究 Agent 計畫](docs/sentiment_analyst_agent_plan.md) | 情緒、共識修正、預期差與資料授權 |
| [基本面研究 Agent 計畫](docs/fundamental_research_agent_plan.md) | FR0～FR3 fixture MVP |
| [Research Report V0 計畫](docs/research_report_plan.md) | 研究層整合為可稽核 JSON／Markdown |
| [投資組合買賣決策與風控計畫](docs/momentum_portfolio_risk_agent_plan.md) | 舊版 1.0 鏈、確定性配置／訂單及競賽風控 |
| [P6 決策驗收與風控補強](docs/decision_acceptance_plan.md) | 決策驗收紀錄 |
| [回測 Agent 計畫](docs/backtest_agent_plan.md)／[P7 第一批](docs/backtest_mvp_plan.md)／[方法規格](docs/backtest_plan_v1.md) | 歷史重播、模擬成交與驗證方法 |
| [十億虛擬帳戶與每日買賣決策計畫](docs/virtual_account_daily_decision_plan.md) | VA1～VA5 |
| [外部帳戶結算檔匯入與對帳計畫](docs/account_data_integration_plan.md) | 選配支線 AC1～AC4 |
| [正式競賽決策報告與 D-Plan 交付](docs/competition_report_delivery_plan.md) | 主辦方規格盤點、D-Plan 候選匯出與待辦 |
| [Docker 使用說明](docs/docker.md) | 建置、容器指令、掛載與疑難排解 |

## 專案結構

```text
config/                 競賽、資料來源與交易狀態核准設定
data/                   官方交易池、產業分類與選配 ETF 比較資料
docs/                   規劃、架構、來源稽核與操作文件
cli/                    Agent、人工與排程共用的穩定 CLI 入口
scripts/                初始化、收集、每日流程與狀態查詢維運指令
skills/                 Codex／Claude 工作流程、契約參考與 UI metadata
src/etf_agent/          Agent 核心程式、runtime 與報告 Builder
src/etf_agent/core/     共用 canonical hash、含時區時間、有限 Decimal 解析與不可變 run store
src/etf_agent/ledger/   回測與虛擬帳戶共用的成交、費稅與帳本
src/etf_agent/prototype/ 早期原型（float Guard、事件策略 V1），不在正式決策路徑
tests/                  單元測試與測試資料
var/                    SQLite 資料庫（不納入 Git）
artifacts/              每日輸出檔案（不納入 Git）
```
