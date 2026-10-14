# ETF-AGENTS｜AI CUP 2026 台股 ETF 決策系統

AI CUP 2026「Agent 基金經理人」的台股 ETF Agent 系統。系統收集並驗證資料，建立同一截止時間的研究快照，經分析、多空研究、交易整合與風險審查後，產生供人工檢視的決策報告。**不自動下單或送件。**

## 快速啟動

先啟動 Docker Desktop，在專案根目錄執行：

```bash
./start.sh
```

這會按需建立 Docker 映像，驗證既有虛擬帳戶；帳戶不存在時依 `config/competition_rules.json` 建立一次空倉帳戶，再啟動唯讀儀表板。開啟 <http://127.0.0.1:8000>，按 `Ctrl+C` 關閉。已有帳戶不會重新注資；帳本損壞時停止啟動。此指令**不抓資料、不執行每日交易決策**。

要更新行情並查看資料狀態：

```bash
./start.sh auto
```

要跑完整每日決策（預設使用已登入的 Claude CLI，且虛擬帳戶已開帳）：

```bash
.venv/bin/python scripts/run_daily_pipeline.py
```

若要改用已登入的 Codex CLI：

```bash
.venv/bin/python scripts/run_daily_pipeline.py --agent-cli codex
```

兩種 CLI 都使用 `tradeagent/` 的角色說明、同一套 Python 契約與 Validator；`--agent-cli` 只選擇子 Agent 的執行器。Codex 子 Agent 以唯讀 sandbox 和 JSON Schema 輸出，Claude 子 Agent 以 `claude -p` 執行。可先加 `--preflight-only` 檢查所選 CLI 的登入與必要檔案。這條 Codex 自動執行路徑已有模擬 CLI 測試，尚未以真實每日資料完整驗收；不能將此視為正式交易成果。

若尚未建立本機 `.venv`，先執行 `python3 -m venv .venv` 與 `.venv/bin/python -m pip install -r requirements.txt`。

上述啟動指令適用能直接執行 `start.sh` 的環境。完整每日決策目前由 Python 直接呼叫該腳本；PowerShell 原生啟動尚未接線。在 Windows PowerShell 查詢既有資料可執行 `.\.venv\Scripts\python.exe scripts\data_status.py`。首次安裝與容器設定見 [Docker 使用說明](docs/docker.md)。

## 系統流程

```text
資料來源 → Data Agent／ResearchSnapshot → 三位分析師
         → 多頭與空頭研究 → 交易 Agent → 風險 Agent
         → Python 配置與 CompetitionGuard → 封存決策與報告 → 人工檢視
```

確定性工作（指標、財務比率、權重、股數、費稅、現金、競賽限制與重建驗證）由 Python 執行；Agent 只提出有證據的解讀與有限選項。來源、抓取時間、當時可得時間、版本與原始回應識別隨資料保存。資料不足或風控失敗時停止或明確降級。

## 常用操作

在專案根目錄、可執行 `start.sh` 的 shell 與 Docker 環境中操作。Docker Desktop 必須已啟動；安裝與容器設定見 [Docker 使用說明](docs/docker.md)。

| 目的 | 指令 | 會做什麼 |
| --- | --- | --- |
| 開帳並看儀表板 | `./start.sh`／`./start.sh dashboard` | 首次開帳，之後驗證並沿用既有帳本；啟動唯讀儀表板 |
| 快速更新行情 | `./start.sh auto` | 原本無參數的資料模式：依交易池選正式或開發行情來源 |
| 只更新研究資料 | `./start.sh data` | 更新 150 檔 yfinance 日線、官方公司資料與選配的 FinMind 候選；不建快照、不推進帳本或執行決策 |
| 更新資料與帳本 | `./start.sh daily` | 驗證交易池、抓行情與公司資料、建立 Snapshot，並結算／準備虛擬帳本；**不執行分析與交易 Agent** |
| 完整每日決策 | `.venv/bin/python scripts/run_daily_pipeline.py [--agent-cli claude\|codex]` | 執行前檢查後接續資料、帳本、交易狀態、分析團隊、多空研究、交易、風險、封存與中文報告 |
| 查看資料狀態 | `.venv/bin/python scripts/data_status.py` | 顯示本機 SQLite 的行情、文件、Snapshot 與來源驗證狀態 |

Windows PowerShell 的本機 Python 路徑是 `.venv\Scripts\python.exe`；`start.sh` 仍需可執行 shell 與 Docker，完整每日決策的原生 PowerShell 啟動尚未接線。所選 Agent CLI 須先登入。首次建立本機環境及 Docker 映像的步驟見 [Docker 使用說明](docs/docker.md)。預設帳戶 ID 是 `ai-cup-2026`；手動帳務命令見 [CLI 入口](cli/README.md)。

每日流程常用選項：

- `--agent-cli claude|codex`：選擇子 Agent CLI；預設 Claude。續跑時須沿用原本的 CLI。
- `--model`：指定所選 CLI 的模型；省略時沿用該 CLI 設定。`--max-budget-usd` 僅適用 Claude CLI。
- `--preflight-only`：只檢查 Docker、所選 CLI 登入、帳戶、檔案與磁碟。
- `--skip-data`：沿用既有 Snapshot 與帳戶快照。
- `--resume --skip-data`：沿用未完成 run 中已驗證的 Agent 輸出。
- `--force`：休市日或當日已完成時強制執行；後續資料與風控仍須驗證。

資料更新不等於當天可做決策。yfinance 當日日線缺檔、官方帳本價量尚未公布、交易狀態或日曆不可用時，流程依各自閘門停止。完整參數可執行 `.venv/bin/python scripts/run_daily_pipeline.py --help`。

## 目前可用與限制

| 範圍 | 目前狀態 |
| --- | --- |
| 研究與公司資料 | 150 檔交易池、yfinance 研究日線、TWSE／TPEx 月營收／重大訊息與官方財報已接線；SQLite 保存原始回應和版本，Snapshot 以含時區 cutoff 與可得時間選資料 |
| 帳本與交易限制 | TWSE／TPEx 官方價量用於虛擬帳本；已核准的交易狀態來源與官方開休市日曆供 Guard、結算及 T+2 使用 |
| 每日決策 | 三位分析師 → 多空研究 → 交易 Agent → 風險 Agent → 確定性配置／Guard 已接入每日流程；封存結果可重建驗證 |
| 情緒與共識 | 全市場正式情緒 Provider、合法且歷史化的分析師共識尚未接入，正式通道維持 `unavailable`；市場交易背景不能充當情緒 |
| 候選資料 | FinMind 三大報表、個股新聞與市場新聞可保存或診斷；未完成授權、時間與覆蓋核對的資料不進正式 Snapshot／決策 |
| 交付與回測 | 中文決策報告已接入；D-Plan 自動映射、主辦方伺服器驗證及正式歷史 Agent 回測尚未完成。現有 B0～B3 回測屬 fixture／離線驗證 |

實際來源、用途與限制見 [資料來源清單](docs/data_sources_inventory.md)；目前分工與重建規則見 [每日決策架構](docs/architecture.md)。MoM／YoY 是歷史比較，不是市場預期或交易訊號。

## 專案目錄

| 路徑 | 用途 |
| --- | --- |
| `src/etf_agent/` | 資料、研究、決策、帳務與風控的核心 Python 程式 |
| `cli/`、`scripts/` | 單步命令與每日流程入口 |
| `tradeagent/` | Codex／Claude 共用的角色 Skill；計算與驗證仍由 Python 執行 |
| `config/`、`data/` | 規則、來源核准設定與交易池輸入 |
| `tests/`、`docs/` | 確定性測試與設計、來源稽核文件 |

## 輸入與產出

| 路徑 | 內容 |
| --- | --- |
| `data/official_universe.csv` | 主辦方 150 檔交易池，版本化輸入 |
| `var/etf_agent.db` | 行情、公司文件、來源原文、時間與版本的本機 SQLite |
| `artifacts/research_snapshot_latest.json` | 最近建立的 ResearchSnapshot；使用前仍須核對 cutoff 與品質 |
| `artifacts/daily_runs/<run_id>/pipeline.json` | 每日執行狀態、阻擋原因與 Agent 呼叫紀錄 |
| `artifacts/portfolio_decisions/<decision_run_id>/` | 不可變決策封存及重建所需輸入 |
| `artifacts/reports/latest.json`、`latest.md` | 交付索引與中文閱讀副本；失敗時顯示診斷 |
| `artifacts/virtual_accounts/` | 虛擬帳本與帳戶快照 |

`var/` 和 `artifacts/` 不納入 Git。歷史快照不得因今天補抓資料而回填新的可得時間；所有下游輸入必須與 Snapshot 的 `snapshot_id`、cutoff 及版本一致。

## 延伸文件

- [CLI 入口與單步操作](cli/README.md)；詳細參數可使用各 CLI 的 `--help`。
- [Docker 與環境設定](docs/docker.md)、[資料來源清單](docs/data_sources_inventory.md)、[每日決策架構](docs/architecture.md)。
- [事件與市場情緒邊界](docs/event_market_scope.md)、[事件研究整合](docs/integrated_event_analysis.md)、[技術分析歷史輸入](docs/technical_trend_history.md)。

核心程式在 `src/etf_agent/`，穩定命令入口在 `cli/`，日常腳本在 `scripts/`，Agent Skill 在 `tradeagent/`。開發規則、測試要求與 fail-closed 原則見 [AGENTS.md](AGENTS.md)。
