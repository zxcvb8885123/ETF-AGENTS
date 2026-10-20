# Docker 使用說明

本專案以 CLI Agent 為主；Docker 提供一致的執行環境，預設啟動唯讀績效儀表板。

## 前置條件

安裝並開啟 Docker Desktop。確認 Docker 可用：

```bash
docker info
docker compose version
```

## 啟動與入口

在專案根目錄執行 `./start.sh`。首次會建置映像（之後僅在映像不存在、`requirements.txt` 或 Dockerfile 變更時重建），驗證預設虛擬帳戶 `ai-cup-2026`；不存在時依 `config/competition_rules.json` 建立唯一 genesis，然後啟動唯讀儀表板 <http://127.0.0.1:8000>。已有帳戶只驗證並沿用，帳本不完整時停止，不重新注資。此模式不抓資料、不執行決策。

`src/`、`cli/`、`scripts/`、`tradeagent/`、`tests/` 從工作區唯讀掛載，程式碼修改後可直接執行。

```bash
./start.sh                       # 開帳／驗證帳戶，啟動儀表板（同 dashboard）
./start.sh auto                  # 更新行情與研究資料，不建快照、不推進帳本
./start.sh daily                 # 更新資料、建立 Snapshot、準備虛擬帳本，不跑 Agent
./start.sh run                   # 完整每日決策（本機 .venv，資料階段用 Docker）
./start.sh run --agent-cli codex # 改用已登入的 Codex CLI
./start.sh status                # 查看既有資料狀態（本機 .venv，不需 Docker）
./start.sh check                 # 建置映像並執行設定檢查與測試
./start.sh official              # 只抓官方交易池行情；空白時停止
./start.sh all                   # 開發模式：抓全部 TWSE 最新行情
./start.sh data                  # auto 的相容別名
./start.sh --help                # 查看所有入口
```

### 資料更新（auto／data）

有官方交易池時抓 TWSE／TPEx 最新官方價量、150 檔 yfinance 日線、官方月營收／重大訊息／已到期季度財報、公司基本資料與產業分類，以及選配的 FinMind 財報候選；略過 Snapshot、帳本與決策。交易池空白時只抓 TWSE 全部可解析證券，研究資料不更新並以 exit 2 回報。Yahoo 當日缺檔不會跳過其他來源，最後以非零狀態回報；缺 FinMind 憑證或候選來源失敗也回報降級。

macOS 排程：`com.etf-agents.data` 於台北時間平日 20:00 執行 `data`，`com.etf-agents.finmind-news` 於 22:00 擷取個股新聞候選。樣板在 `scripts/launchd/`，日誌在 `artifacts/daily_runs/`。FinMind Token 依序讀取執行環境、專案根目錄 `.env`（`FINMIND_TOKEN=...`、權限 600、Git 忽略），財報流程還可回退至 macOS 鑰匙圈服務 `etf-agent-finmind`。

### 每日流程（daily／run）

`daily` 先驗證官方來源與交易池，再更新官方帳本價量、yfinance 日線、月營收／重大訊息與財報（yfinance 截止日未覆蓋 150 檔即停止），最後以 `DAILY_CUTOFF`（未設定時為 Asia/Taipei 現在時間）建立 Snapshot 並推進虛擬帳本；帳本推進失敗時以非零狀態結束。官方個股月行情由 `scripts/collect_official_history.py` 補上市（TWSE `STOCK_DAY`，來源 `TWSE_STOCK_DAY`）與上櫃（TPEx `tradingStock`，來源 `TPEX_TRADING_STOCK`），兩市場共用同一個節流器，全新環境首次回看兩年約需數十分鐘；資料專用排程（`data`）不補。個股新聞由 22:00 的 FinMind 排程擷取，來源授權與時間尚未核准。

`run` 使用主機 `.venv/bin/python` 執行 `scripts/run_daily_pipeline.py` 並原樣傳遞參數，以 `--agent-cli claude|codex` 選擇已登入的 CLI，資料階段仍走 Docker 的 `daily`。缺少 `.venv` 時會停止並提示安裝方式。兩種執行器都交給同一套 Python Validator；Codex 執行器目前只通過模擬 CLI 測試，尚未做真實資料端到端驗收。不會自動產生或送出訂單。

Windows PowerShell 可執行單步 Python CLI；`start.sh` 的 PowerShell 對應入口尚未實作。

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
docker compose run --rm agent python3 -m unittest discover -s tests -v
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
