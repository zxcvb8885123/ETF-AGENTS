# Docker 使用說明

本專案以 CLI Agent 為主；Docker 提供一致的執行環境，預設啟動唯讀績效儀表板。

## 前置條件

安裝並開啟 Docker Desktop。確認 Docker 可用：

```bash
docker info
docker compose version
```

## 啟動與入口

在專案根目錄執行 `./start.sh`。它會依序：

1. 首次建置映像。之後只在映像不存在，或 `requirements.txt`、Dockerfile 變更時重建。
2. 驗證預設虛擬帳戶 `ai-cup-2026`。
   - 帳戶不存在：依 `config/competition_rules.json` 建立唯一的 genesis。
   - 帳戶已存在：只驗證並沿用。帳本不完整時停止，不重新注資。
3. 啟動唯讀儀表板 <http://127.0.0.1:8000>。

此模式不抓資料、不執行決策。

`src/`、`cli/`、`scripts/`、`tradeagent/`、`tests/` 從工作區唯讀掛載，程式碼修改後可直接執行。

```bash
./start.sh                       # 開帳／驗證帳戶，啟動儀表板（同 dashboard）
./start.sh auto                  # 更新行情與研究資料，不建快照、不推進帳本
./start.sh daily                 # 更新資料、建立 Snapshot、準備虛擬帳本，不跑 Agent
./start.sh run                   # 完整每日決策（本機 .venv，資料階段用 Docker）
./start.sh run --agent-cli codex # 改用已登入的 Codex CLI
./start.sh run --agent-cli opencode # 改用已設定憑證的 OpenCode CLI
./start.sh status                # 查看既有資料狀態（本機 .venv，不需 Docker）
./start.sh check                 # 建置映像並執行設定檢查與測試
./start.sh official              # 只抓官方交易池行情；空白時停止
./start.sh all                   # 開發模式：抓全部 TWSE 最新行情
./start.sh data                  # auto 的相容別名
./start.sh --help                # 查看所有入口
```

### 資料更新（auto／data）

**有官方交易池時**，會抓取：

- TWSE／TPEx 最新官方價量
- 150 檔 yfinance 日線
- 官方月營收、重大訊息、已到期的季度財報
- 公司基本資料與產業分類
- 選配的 FinMind 財報候選

不建立 Snapshot，不動帳本，不跑決策。

**交易池空白時**，只抓 TWSE 全部可解析的證券，研究資料不更新，並以 exit 2 回報。

**失敗處理**

- Yahoo 當日缺檔，不會跳過其他來源，最後以非零狀態回報。
- 缺 FinMind 憑證，或候選來源失敗，也會回報「降級」。

**macOS 排程**（樣板在 `scripts/launchd/`，日誌在 `artifacts/daily/`）

| 排程 | 時間（台北） | 作用 |
| --- | --- | --- |
| `com.etf-agents.data` | 平日 20:00 | 執行 `data` |
| `com.etf-agents.finmind-news` | 22:00 | 擷取個股新聞候選 |

**FinMind Token** 依序讀取：執行環境 → 專案根目錄 `.env`（`FINMIND_TOKEN=...`，權限 600，Git 忽略）。財報流程還可回退到 macOS 鑰匙圈服務 `etf-agent-finmind`。

### 每日流程（daily／run）

#### `daily`

依序執行：

1. 驗證官方來源與交易池。
2. 更新官方帳本價量、yfinance 日線、月營收、重大訊息與財報。yfinance 截止日沒有覆蓋 150 檔就停止。
3. 以 `DAILY_CUTOFF`（未設定時為 Asia/Taipei 現在時間）建立 Snapshot，並推進虛擬帳本。帳本推進失敗時，以非零狀態結束。

補充：

- 官方個股月行情由 `scripts/collect_official_history.py` 補抓。上市用 TWSE `STOCK_DAY`（來源 `TWSE_STOCK_DAY`），上櫃用 TPEx `tradingStock`（來源 `TPEX_TRADING_STOCK`）。
- 兩個市場共用同一個節流器。全新環境首次回看兩年，約需數十分鐘。
- 資料專用排程（`data`）不補這部分。
- 個股新聞由 22:00 的 FinMind 排程擷取，來源授權與時間尚未核准。

#### `run`

- 使用主機 `.venv/bin/python` 執行 `scripts/run_daily_pipeline.py`，參數原樣傳遞。資料階段仍走 Docker 的 `daily`。
- 缺少 `.venv` 時會停止，並提示安裝方式。
- 用 `--agent-cli claude|codex|opencode` 選擇子 Agent CLI。三種執行器都交給同一套 Python Validator。
- 不會自動產生或送出訂單。

**各執行器的注意事項**

| 執行器 | 說明 |
| --- | --- |
| Codex | 已完成真實資料端到端執行。ChatGPT.app 內附 Codex CLI 的 macOS 路徑，`start.sh` 會在非互動 shell 自動加入 PATH（互動 zsh 亦已設定） |
| OpenCode | 目前只有小型唯讀煙霧測試與模擬 CLI 測試。執行時禁用插件，唯讀，不允許 `.env` 與外部目錄。預檢只回報是否有憑證，不保存帳號 |
| 共通 | 重試與續跑仍檢查 Snapshot、cutoff、執行器與來源。CLI 回報的用量不是訂閱帳單 |

**`run` 的其他參數**

| 參數 | 說明 |
| --- | --- |
| `--analyst-model MODEL` | 只用於技術／基本面／事件分析師批次。Claude CLI 預設 `sonnet`，傳 `inherit` 沿用 `--model`。其他 CLI 未指定時沿用 `--model` |
| `--max-workers N` | 分析師與多空研究階段同時執行的子 Agent 數（1～8，預設 3；1 為串行）。交易與風險階段仍串行 |
| `--technical-gate`／`--no-technical-gate` | 預設開啟，見[降 token 計劃](token_reduction_plan.md) |

Windows PowerShell 可執行單步 Python CLI。`start.sh` 的 PowerShell 對應入口尚未實作。

### 依賴與重建

Dockerfile 與本機 `.venv` 共用 `requirements.txt`（含 Skill 驗證用的 PyYAML）。修改依賴後執行 `./start.sh check` 會自動重建；強制重建用 `FORCE_DOCKER_BUILD=1 ./start.sh check`，可用 `docker compose run --rm agent python3 -c 'import yaml; print(yaml.__version__)'` 確認。

## 手動 Docker 指令

先建置映像：

```bash
docker compose build
```

執行專案設定檢查：

```bash
docker compose run --rm agent
```

初始化資料庫、抓取開發資料並查看狀態：

```bash
docker compose run --rm agent python3 scripts/init_db.py
docker compose run --rm agent python3 scripts/collect_twse.py --all-listed
docker compose run --rm agent python3 scripts/data_status.py
```

官方 150 檔名單填入 `data/official_universe.csv` 後，正式抓取上市與上櫃最新行情：

```bash
docker compose run --rm agent python3 scripts/collect_latest_prices.py
docker compose run --rm agent python3 scripts/collect_history.py
```

執行測試：

```bash
docker compose run --rm agent python3 -m pytest
```

## 掛載資料夾

| 本機路徑 | 容器路徑 | 用途 |
| --- | --- | --- |
| `config/` | `/app/config` | 唯讀：競賽與資料來源設定 |
| `data/` | `/app/data` | 唯讀：官方交易池與 ETF 基準資料 |
| `var/` | `/app/var` | 可寫：SQLite 資料庫 |
| `artifacts/` | `/app/artifacts` | 可寫：日後的報告與交易書 |

`compose.yaml` 預設用 macOS 常見的 UID/GID `501:20` 寫入本機資料夾。帳號不同時，在專案根目錄建立 `.env`：

```text
HOST_UID=你的使用者 UID
HOST_GID=你的群組 GID
```

也可以直接使用 `./start.sh`，它會自動設定目前帳號的 UID/GID。

## 疑難排解

| 情況 | 處理方式 |
| --- | --- |
| 找不到 Docker | 安裝並啟動 Docker Desktop 後重試 |
| `Docker 尚未啟動` | 開啟 Docker Desktop，等引擎就緒後重試 |
| 容器顯示 `No module named yaml` | 執行 `docker compose build` 重建映像，再檢查容器內的 PyYAML 版本 |
| 無法寫入 `var/` 或 `artifacts/` | 使用 `./start.sh`，或檢查 `.env` 的 UID/GID |
| 抓取失敗 | 檢查網路，再執行 `./start.sh all` 重試 |
| 正式模式提示交易池空白 | 先把官方名單填入 `data/official_universe.csv` |
