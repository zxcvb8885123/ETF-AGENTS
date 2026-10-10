# ETF Agent Manager

AI CUP 2026「Agent 基金經理人」的台股 ETF Agent 系統。每天完成資料蒐集、研究、買賣決策、組合與資金風控，最後產生供人工檢視的決策報告與 D-Plan 候選檔。

```text
資料蒐集 → 分析團隊 → 多空研究 → 交易整合 → 風險 → 確定性配置／Guard → 報告與 D-Plan 候選檔 → 人工檢視
```

設計原則是「確定性歸程式，不確定性歸 LLM」：指標、權重、張數、費稅、現金與競賽規則全部由 Python 計算並由 Validator 重算；子 Agent 只輸出有限選項的判斷與引用證據。系統**不自動下單、不送件**，平台送件與交易由人工在系統外處理。

## 快速開始

```bash
./start.sh
```

首次執行會建立 Docker 映像；之後只有映像不存在、依賴或 Dockerfile 變更才重建。腳本會初始化 SQLite、抓取帳本所需 TWSE／TPEx 當日價量、以 yfinance 增量更新 150 檔研究日線，再顯示資料狀態。資料專用排程不補抓官方個股月歷史行情；`daily` 決策流程在帳本結算時才補官方個股價量。程式碼直接從工作區掛載進容器，修改 Python 不需重建映像。

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

依序執行：執行前檢查（Docker、`claude` 登入、帳戶、必要檔案、磁碟，任一失敗即在呼叫 Agent 前停止）→ 封存交易狀態 CSV → `./start.sh daily`（資料、Snapshot、虛擬帳本）→ 交易狀態包 → 分析團隊 → 多空研究 → 交易整合 → 風險（子 Agent 經 `claude -p`，結構化輸出並由 Validator 驗證，一般日約 20 次，不再額外跑事件四子 Agent）→ 封存 Decision run（含 `team_inputs`）→ macOS 通知。每日流程不產生 DailyReport；對外交付由 `cli/dplan.py` 從封存 Decision run 匯出 D-Plan。

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
| `./start.sh data` | 每日以 yfinance 擷取 150 檔研究日線，並收集官方財報／公司資料及 FinMind 三大報表候選；不抓官方價量、不啟動決策 Agent 或帳本 |
| `./start.sh daily` | 每日更新 yfinance 150 檔研究日線（當日缺檔即停止）、官方帳本價量、事件與財報；結算需要時補官方個股價量，另擷取公司資料與 FinMind 三大報表候選，推進帳本並建立 Snapshot |
| `./start.sh dashboard` | 啟動唯讀績效儀表板 |

資料擷取的 macOS `launchd` 工作 `com.etf-agents.data` 於台北時間平日 20:00 執行 `./start.sh data`；個股新聞另由 `com.etf-agents.finmind-news` 於平日 22:00 擷取 FinMind `TaiwanStockNews`，避開同小時配額。新聞只保存未核准候選，不進正式研究。兩個排程都不自動執行決策、下單或送件。FinMind Token 優先由執行環境的 `FINMIND_TOKEN` 讀取，其次讀取專案根目錄的 `.env` 中 `FINMIND_TOKEN=...`（本機檔案、權限 600、Git 忽略）；財報流程最後才嘗試 macOS 鑰匙圈服務 `etf-agent-finmind`。排程樣板在 `scripts/launchd/`。

## 目前進度

### 已完成

- **資料層**：SQLite 保存行情、原始回應、抓取時間與執行紀錄；每日以 yfinance 更新 150 檔研究日線並檢查當日覆蓋，TWSE／TPEx 當日官方價量仍供帳本結算，`daily` 決策只補抓延遲的上市個股官方價量；月營收、重大訊息與官方財報彙總（24 個端點，2026 Q2 實測 298/300）；FinMind 三大報表與個股新聞線索分時擷取，新聞授權與時間未核准，不進 Snapshot／每日決策；150 檔交易池逐檔驗證與 Snapshot fail-closed 閘門；正式產業分類 150／150。
- **交易狀態與交易日曆**：TS1～TS4 契約、Parser、Validator、SQLite、CLI 與 Guard；政府開放 CSV 確定性映射。官方開休市日曆（`TradingCalendar`）統一用於每日是否執行、目標交易時段與帳本 T+2 交割，區分休市日與「僅辦理結算交割」日；日曆缺漏或未涵蓋年度時停止。**2026-09-28 已核准七個政府開放來源**（時效 72 小時），TWSE「管理股票」依證交所營業細則第 52 條列為不適用；9/28 實跑結果 148 檔 allowed、2 檔 blocked（處置）、unknown 0。
- **研究層**：獨立事件研究工具（Fact／Bull／Bear／Adjudicator、雙重 validator；保留舊封存驗證及單獨研究，不在每日決策鏈）；市場情緒與分析師 Agent MVP（無核准來源時 `unavailable`）；基本面 FR0～FR3 fixture MVP；Research Report V0。
- **每日情緒／共識與帳戶接入**：已支援授權資料包逐筆標註、全池聚合、決策與報告接線；帳戶補上原價估值、cutoff、缺行情等待與重跑重用。真實情緒／共識資料商尚未接入，見[操作與限制](docs/daily_perception_account_integration.md)。
- **決策層新鏈（R1～R6）**：分析團隊（技術／基本面／事件與市場情緒，共三位）→ 多空研究員 → 交易 Agent → 風險 Agent 現金姿態與分級 → 確定性配置、情境、CompetitionGuard → 封存與重建驗證。舊版 Portfolio Decision 1.0 鏈（Buy／Sell／Trade Adjudicator，P0～P6）已於 2026-09-30 移除，不再支援重建。前一交易日未成交缺口由帳本整理後逐檔交給交易 Agent（不自動補單，是否再 buy／add 由 Agent 決定）；風險審查另收到程式計算的各產業實際權重。
- **帳務與回測**：虛擬帳本 VA1～VA3（唯一開帳、決策前帳戶快照、模擬成交、日終封存）；回測 B0～B2 fixture（歷史時鐘、整張成交、交割、公司行動）；B3 fixture 策略比較第一版（同一 `BacktestRequest` 重播多組逐日輸入，計算報酬、回撤、成本與 24 交易日視窗，可重建驗證，結果固定標示 `evidence_status=insufficient`）；外部帳戶匯入 AC1～AC4 fixture 工具鏈。
- **交付**：D-Plan v4.0 候選匯出與本地結構／引用鏈檢查（匯出前核對 Decision run 的帳戶快照與帳戶目前 latest 的 prepare-day 封存一致）、唯讀績效儀表板。DailyReport／FailureReport 與報告工作流已於 2026-09-29 移除（比賽只需 D-Plan），舊封存仍留在 `artifacts/report_runs/` 供稽核。

### 第一次真實資料決策（2026-09-28，目標交易日 9/29）

整條新鏈以真實資料跑完，Decision run `decision-20260928T084128Z` **approved、30 筆買進委託**（現金 11.5%、周轉率 88%、現金姿態 neutral）。

第一次執行曾被拒絕：提案通過全部規則，但 `liquidity_stress` 壓力情境（成交率 50%、滑價 2 倍）下現金比例 60% 超過 25% 上限；從全現金建倉時這個情境結構上無法通過。現已改為：現金上限只在頂層 `CASH_WEIGHT` 與 `base` 情境為硬性規則，壓力情境現金超標只記警告，其餘情境檢查仍為硬性。被拒絕的封存與舊報告移至各 repository 的 `.superseded/` 保留稽核，未刪除。風險 Agent 留下兩項未解風險：產業實際權重未提供給審查、部分成交後的補單規則尚未納入下一交易日流程。

2026-10-09 已完成事件研究併入分析團隊：high 事件保留在事件分析師報告，直接交給股票層級多空研究員，不再另跑事件四子 Agent。新 `team_inputs` 2.1 只保存三份分析報告與現金姿態，舊 2.0 沿用原始 `ResearchResult` 重建。詳細契約見 [事件研究併入分析團隊](docs/integrated_event_analysis.md)。

### 尚未完成

- 虛擬帳本 VA4／VA5 自動化與跨日真實資料驗收；正式排程啟用。步驟見[每日自動化與回測就緒計畫](docs/automation_backtest_readiness_plan.md)。
- D-Plan：Decision run 到「來源→事實→市場姿態→全持股決策」的完整映射、真實資料端到端演練；主辦方伺服器語意驗證（`verify_dplan.py`）未取得，本地檢查不等同平台驗證。
- 策略說明書（ETF 名稱、投資主題、投資理念）：繳交期間 2026-10-21 至 10-26。
- 競賽規則待釐清：現金上限（規則原文為每日「小於」NAV 25%，Schema 目標區間 `≤25%` 的差異待釐清）、提交時間（設定 19:30 與 Schema 05:00–08:55）、min successful days。
- 回測重放（8/21 單日 → 8 月逐日 → 重跑穩定度，見[回測重放計畫](docs/backtest_replay_plan.md)）；回測 B3 的六組正式策略、績效貢獻、不確定性與樣本外分析；B4 Agent 評估；真實歷史回測（缺版本化歷史交易日曆與歷史交易狀態）。
- 基本面 FR4 真實演練、FR5 下游契約升版，毛利率、現金流、估值與金融業公式。
- 合法且歷史化的市場情緒／分析師資料來源；FinMind 財報候選與新聞候選的覆蓋、發布時間證據及 Snapshot 接線（見[多來源更新計畫](docs/data_agent_multisource_update_plan.md)）。
- 選配：`data/active_etf_top10.csv` 主動式 ETF 持股權重（目前空白；D-Plan 指南未將 Active Share 列為每日硬性上限，不阻擋決策）。

MoM／YoY 只是歷史基準，不能等同市場預期或單獨形成方向。fixture 驗收結果不能視為正式交易驗收或策略績效。

## 指令參考

以下是各 Agent 的單步 CLI；每日流程已串接，除錯或重建時才需要單獨呼叫。

### 資料

| 指令 | 用途 |
| --- | --- |
| `PYTHONPATH=src python3 scripts/probe_data_sources.py` | 探測 TWSE／TPEx 最新行情並驗證 150 檔交易池 |
| `PYTHONPATH=src python3 scripts/collect_latest_prices.py` | 抓取官方交易池的 TWSE／TPEx 最新行情 |
| `PYTHONPATH=src python3 scripts/collect_official_history.py` | 只補上市個股官方價量供帳本；上櫃個股月擷取器已移除，舊資料仍可讀取 |
| `.venv/bin/python scripts/collect_history.py` | 透過 yfinance 增量更新最近兩年研究日線，預設到台北當日；截止日 150 檔不完整時 exit 2 |
| `.venv/bin/python scripts/collect_supplemental_sources.py finmind --dataset TaiwanStockFinancialStatements --stock-id 2330 --start 2025-01-01 --end 2026-09-29` | 從 `FINMIND_TOKEN` 讀取憑證，保存逐股財報候選；也可改用 `--universe data/official_universe.csv` |
| `./scripts/run_finmind_news.sh YYYY-MM-DD` | 逐股擷取 FinMind 新聞線索；來源時間無時區、原媒體授權未核准，不能進正式研究 |
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
| `cli/portfolio_decision.py --bundle I.json validate-intent ...` | 驗證交易 Agent 的 TradeDecision（等級與 claim 採納）；現金姿態 aggressive／neutral／defensive 暫定對應 3%／10%／20%，權重依等級乘數 ÷ ATR14% 分配 |
| `cli/portfolio_decision.py --bundle I.json compute-proposal ...` | 以一張（1,000 股）為單位計算配置、訂單、費稅與現金 |
| `cli/portfolio_decision.py --bundle I.json compute-scenarios ...`／`compute-guard ...` | 價格與流動性壓力情境；交易池、可交易性、現金與曝險 Guard（基準 Active Share 選配） |
| `cli/portfolio_decision.py --bundle I.json finalize／validate-decision／save-run ... --team-inputs T.json` | 完整重建、驗證並封存 Decision run（必須附 `team_inputs`） |

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
| [分析團隊 prompt 五輪比較](docs/analyst_prompt_evaluation.md) | 五輪比較及後續技術試跑分版保存；技術已接入整段歷史路徑，區分回檔與趨勢轉變；未證實交易績效優勢 |
| [技術分析整段歷史輸入](docs/technical_trend_history.md) | 完整封存價格視窗、逐日MA20／MA60、來源與cutoff驗證；已接入正式技術brief builder |
| [決策層 Agent 團隊重構計畫](docs/agent_team_refactor_plan.md) | **R1～R6 已完成**：分析團隊 → 多空研究員 → 交易 Agent → 風險 Agent；§11 的 S2 已於 2026-10-09 完成，其他項目依個別進度 |
| [M1 官方交易狀態接入](docs/trading_status_m1_plan.md) | 契約、Parser、Validator、SQLite、CLI 與 Guard |
| [TS0 來源核准行動計畫](docs/source_audit/2026-09-25_ts0_approval_plan.md) | 政府開放 CSV 查證、確定性映射與 2026-09-28 核准紀錄 |
| [Agent 開發架構](docs/agent_plan.md) | 資料庫、策略、風控買賣及報告流程 |
| [Data Agent 計畫](docs/data_agent_plan.md) | 資料收集、驗證、版本保存與研究快照 |
| [Data Agent 多來源更新計畫](docs/data_agent_multisource_update_plan.md)／[來源盤點](docs/data_sources_inventory.md) | FinMind／開放新聞候選已實作，正式 Snapshot 接線待驗證 |
| [市場情緒與分析師研究 Agent 計畫](docs/sentiment_analyst_agent_plan.md) | 情緒、共識修正、預期差與資料授權 |
| [基本面研究 Agent 計畫](docs/fundamental_research_agent_plan.md) | FR0～FR3 fixture MVP |
| [Research Report V0 計畫](docs/research_report_plan.md) | 研究層整合為可稽核 JSON／Markdown |
| [P6 決策驗收與風控補強](docs/decision_acceptance_plan.md) | 決策驗收紀錄 |
| [回測重放計畫](docs/backtest_replay_plan.md)（歷史時點重跑完整決策鏈）／[回測 Agent 計畫](docs/backtest_agent_plan.md) | 歷史重播、模擬成交與驗證方法 |
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
tradeagent/             依角色分組的 Codex／Claude Skill、契約參考與 UI metadata
  analysis/             三位主要分析師各自一組，相關 Skill 放在同組
    technical/          technical-analyst、momentum-regime
    fundamental/        fundamental-analyst、fundamental-research
    event/              event-analyst、sentiment-analyst
  research/             每日股票研究與獨立事件研究分組
    stock-research/     bull-researcher、bear-researcher
    event-research/     event-analysis；subagents/ 保存四個事件子 Agent
  trading/              交易整合
  risk/                 現金姿態與配置後風險審查
  data/                 資料蒐集與驗證
  reporting/            Research Report 整合
  backtest/             歷史回測驗證
src/etf_agent/          Agent 核心程式、runtime 與報告 Builder
src/etf_agent/core/     共用 canonical hash、含時區時間、有限 Decimal 解析與不可變 run store
src/etf_agent/ledger/   回測與虛擬帳戶共用的成交、費稅與帳本
src/etf_agent/prototype/ 早期原型（float Guard、事件策略 V1），不在正式決策路徑
tests/                  單元測試與測試資料
var/                    SQLite 資料庫（不納入 Git）
artifacts/              每日輸出檔案（不納入 Git）
```
## 基本面資料補抓更新（2026-10-03）

基本面報告改用正面／中性／負面及短句。缺口以中文列出期間、報表與欄位；英文代碼保留在內部紀錄。無資料仍保留內部 unknown，對外必須另註「資料不足，暫不判斷」。

缺指標時先查資料狀態。資料已取得但未接入、口徑未確認與來源抓取失敗分開說明；不得把分析摘要缺欄位寫成來源沒有資料。金融業稅後淨利欄位與淨收益原值已接入，別名衝突會拒絕；跨年比較及金融業專用比率仍未完整接入。

互動研究的基本面缺口可交回 Data Agent，使用 `cli/fundamental_repair.py` 補抓 TWSE／FinMind 並另建快照。單季合計須通過官方當期金額核對；今天補抓資料不回填舊回測。公司產業代碼已接入摘要；Yahoo 已實抓，但尚未接入正式指標；每日管線尚未自動呼叫補抓。詳見 [基本面缺口補抓](docs/fundamental_data_repair.md)。

### 事件與市場情緒合併（2026-10-03）

最新第三位分析師使用 **2.2**：一個整體台股情緒結果，加上逐家公司事件結果，兩者不合成分數。每日批次與驗證已更新；目前全市場情緒 Provider 尚未接入，因此市場通道為無法判斷。可用 `cli/market_context_probe.py` 補抓公開大盤成交、指數與法人交易背景，但不將交易事實冒充情緒。舊 2.1 保留原始驗證。見[分工、試跑與資料缺口](docs/event_market_scope.md)。

三位分析師的 prompt 已整理為自然中文：技術判斷「上升／盤整／下跌」，基本面與事件情緒判斷「正面／中性／負面」。主報告逐檔呈現一個結果與簡短理由；缺少資料另列「資料不足，無法判斷」，不當成中性。程式欄位與補抓、引用規則移至各 Skill 的 `references/data-rules.md`，執行前仍須讀取。本次為指令文字整理，未更改報告資料格式、驗證器或資料接入狀態。

兩檔新聞候選已完成原頁核對與全量標題判讀測試，補抓版本存回資料庫；新增 `cli/news_sentiment.py` 可重跑驗證與封存。測試結果與正式情緒分開，來源未核准前仍不交給交易。見 [新聞準備與限制](docs/news_sentiment_preparation.md)。

事件摘要已接入同一快照的財報背景（原值、單位、期間、引用），每日 pipeline 另唯讀盤點資料庫的現金流／新聞候選，避免把「已取得但尚未核對接入」誤寫成來源沒有資料。候選只提供取得狀態，不產生方向；超過歷史 cutoff 的候選不得回填。見 [事件資料接線](docs/event_data_availability.md)。

每日團隊交付三份報告。`event-analyst` 已改為「事件與市場情緒分析師」，同一位 Agent 讀事件與已驗證情緒通道，輸出 AnalystReport(event) 2.1。情緒聚合由程式重建；無核准來源時明確標示無法判斷。`sentiment-analyst` 保留為資料標註與聚合流程，不再是另一個交付席位。high 事件保留在事件分析師報告，直接交股票層級多空研究，不另跑四子 Agent。舊四份報告只沿用原始封存重建路徑，不轉寫成新格式。見[合併契約與限制](docs/event_sentiment_merge.md)。

### 全市場新聞接入（2026-10-05）

已新增中央社與自由時報財經 RSS 收集、資料庫版本保存、完整標籤核對與第三位分析師的市場資料包接線。本次實際取得 60 則標題與前言，11 則屬整體市場，新聞診斷綜合看法為正面；來源比賽使用權未確認，正式情緒仍降級，不交給交易。RSS 不保證歷史期間所有新聞已補齊，也尚未新增每日自動抓取／標籤排程。詳見 [全市場新聞接入](docs/market_news_integration.md)。

### 市場新聞歷史補抓（2026-10-05）

已補入10/1～10/3的79篇原頁及10/4的13篇原頁，連同RSS共152個輸入項目、151則去重新聞，全量判讀並接入第三位分析師。新增可重複執行的日期補抓入口與1.1成對資料包；來源規範已查明允許非商業RSS使用，與原頁或比賽用途的實際使用權分開說明。補抓時間不回填到過去時鐘。詳見 [補抓結果與規範核對](docs/market_news_history.md)。

### 市場新聞來源使用紀錄接入（2026-10-05）

已補上來源使用核對及原始規範的追加式資料庫保存。市場新聞接入可透過 `integrate --database` 讀取截止時間前的核對版本；不把新聞存在視為核准。若僅剩來源使用範圍未確認，中文報告會直接說明這個原因。現有資料庫未查到三個新聞供應來源的比賽研究核准紀錄，正式結果仍拒絕使用未確認來源。詳見 [接入與目前限制](docs/market_news_integration.md)。

### 多空研究指令整理（2026-10-09）

多頭／空頭 Skill 改用中文說明研究目的、證據與推論、強度和何時需重估；欄位及引用規則移至各自的 references/data-rules.md。逐家公司事件與一次全市場情緒分開閱讀，正式市場通道不可用時保留 unknown／unavailable；市場背景不能替代個股依據。多空 brief 已補入 event 2.2 頂層的 market_sentiment，兩方讀同一份通道，不改輸出契約與舊封存雜湊。

### 交易 Agent 的多空整合定位（2026-10-09）

交易 Agent 是多頭與空頭研究的整合者，讀取兩方既有論點後，說明採納與否決的理由，再形成每檔單一交易意圖與 buy／add 信心。它不按論點數量投票、不新增事實或 claim；現金姿態由風險 Agent 判斷，配置及交易數量由 Python 計算。Skill 主文件說明整合主線，正式欄位與限制移到 [交付規則](tradeagent/trading/trader/references/data-rules.md)。程式識別字 trader 與 TradeDecision 2.0 不變。

交易 Agent 的閱讀版 `trader_report.md` 只顯示整合後的逐檔交易意圖、信心、理由、未解問題與重估條件，不附多空報告或論點全文。原始研究與採納／否決 ID 仍保存在 JSON 供驗證與稽核。
