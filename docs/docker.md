# Docker 使用說明

本專案的 Agent 在本機 `.venv`（Python 3.12）執行；Docker 提供一致的資料抓取環境，並預設啟動唯讀績效儀表板。Docker 本身不會下單、不會送件。

## 前置條件

安裝並開啟 Docker Desktop，確認可用：

```bash
docker info
docker compose version
```

## 平常怎麼用

在專案根目錄執行 `./start.sh`。它會：

1. 建置映像。只在映像不存在，或 `requirements.txt`、`requirements.lock`、Dockerfile 變更時才重建。
2. 驗證使用中的虛擬帳戶（`config/active_account.json`）。帳戶不存在就依 `config/competition_rules.json` 建立唯一的開帳紀錄；已存在就只驗證並沿用，帳本不完整時停止，不重新注資。
3. 啟動唯讀儀表板 <http://127.0.0.1:8000>（埠號可用環境變數 `DASHBOARD_PORT` 改）。不抓資料、不跑決策。

`src/`、`cli/`、`scripts/`、`tradeagent/`、`tests/` 從工作區唯讀掛載，改了程式碼可直接執行。

| 指令 | 作用 |
| --- | --- |
| `./start.sh` | 開帳或驗證帳戶，啟動儀表板（同 `dashboard`） |
| `./start.sh run` | 完整每日決策。在本機 `.venv` 執行，資料階段會用到 Docker。參數見[參數說明](run-options.md) |
| `./start.sh status` | 查看資料狀態（本機 `.venv`，不需 Docker） |
| `./start.sh benchmark collect`／`benchmark compare` | 補抓 0050 基準行情／比較帳戶與基準（本機 `.venv`，不需 Docker） |
| `./start.sh backtest`（`grid`／`replay`／`report`） | 回測與評測報告，產生 HTML（本機 `.venv`，不需 Docker、不呼叫 Agent） |
| `./start.sh auto` | 更新行情與研究資料，不建 Snapshot、不動帳本（`data` 是同義別名） |
| `./start.sh daily` | 更新資料、建 Snapshot、準備虛擬帳本，不跑 Agent |
| `./start.sh official` | 只抓官方交易池行情；交易池空白時停止 |
| `./start.sh all` | 開發用：抓全部 TWSE 最新行情 |
| `./start.sh check` | 建置映像並執行設定檢查與測試 |
| `./start.sh --help` | 看所有入口 |

除了 `run`、`status`、`benchmark` 與 `backtest`，其他入口不接受額外參數。缺少 `.venv` 時 `run`、`status`、`benchmark`、`backtest` 會停止並提示建立方式。

## 資料更新（auto／data）

交易池（`data/official_universe.csv`）有內容時，會抓：

- TWSE／TPEx 最新官方價量與 150 檔 yfinance 日線
- 官方月營收、重大訊息、已到期的季度財報
- 公司基本資料與產業分類
- 選配的 FinMind 財報候選（財報只在公布區間內變動，首次補約 3 年歷史，之後每天只更新一次；`FORCE_FETCH=1` 可強制重抓行情與財報）
- 去年同期補抓（`cli/fundamental_repair.py --scheduled`，沿用 `FINMIND_TOKEN`，與候選擷取同一組金鑰取得順序並以 `docker compose run -e FINMIND_TOKEN` 傳入）：只補尚無同期文件的股票；`PRIOR_REPAIR=0` 關閉、`PRIOR_REPAIR_MAX` 調整每輪上限（預設 60）；沒有金鑰時與候選擷取一起略過

交易池空白時，只抓 TWSE 全部可解析的證券，研究資料不更新，以結束碼 2 回報。

失敗處理：Yahoo 當日缺檔不會跳過其他來源，最後以非零狀態回報；缺 FinMind 憑證或候選來源失敗，則回報「降級」。已有收盤後抓到的完整當日行情時不重抓。

**FinMind Token** 依序讀取：執行環境 → 專案根目錄 `.env`（`FINMIND_TOKEN=...`，權限 600，Git 忽略）。財報流程還可回退到 macOS 鑰匙圈服務 `etf-agent-finmind`。

排程（20:00／06:00 資料、20:40 個股新聞、21:00 與 07:00 決策等）的時間表與安裝方式見[日常操作](operations.md#系統每天自己會做的事)。

## 每日流程（daily／run）

`daily` 依序做：

1. 驗證官方來源與交易池。
2. 更新官方帳本價量、yfinance 日線、月營收、重大訊息與財報。yfinance 截止日沒覆蓋 150 檔就停止。
3. 補抓恐懼貪婪指標、隔夜市場背景、全市場新聞與基準 0050 行情，失敗只讓對應項目降級。
4. 以截止時間（`DAILY_CUTOFF`，未設定時為台北現在時間）建立 Snapshot，並推進虛擬帳本。帳本推進失敗時以非零狀態結束。

補充：

- 官方個股月行情由 `scripts/collect_official_history.py` 補抓：上市走 TWSE `STOCK_DAY`，上櫃走 TPEx `tradingStock`，兩者共用同一個節流器。全新環境首次回看兩年，約需數十分鐘。`data` 排程不補這部分。
- 個股新聞由平日 20:40 的 FinMind 排程擷取。來源經 `cli/company_news.py review --status approved ...` 核准後（已於 2026-10-27 執行），`run` 才把截止前最後一批、白名單媒體的標題交給事件分析師。21:00 備案用當晚 20:40 那批，隔天早上重跑也用這批。

`run` 用 `.venv/bin/python` 執行 `scripts/run_daily_pipeline.py`，不會自動產生或送出訂單。

### 各執行器的注意事項

| 執行器 | 說明 |
| --- | --- |
| Claude | 預設。 |
| Codex | 可跑全部子 Agent。唯讀沙箱擋寫、不擋讀，所以多空研究員的隔離改由「研究階段輸出暫存」保證（Codex 仍可讀整個專案含舊 run，隔離較弱）；預檢另要求 `codex exec` 支援 `--ephemeral`；隔離任務的回覆只走 `--json` 標準輸出（記憶體），不寫 `--output-last-message` 檔，避免並行的同儕任務在系統暫存區讀到。 |
| OpenCode | 目前只有小型唯讀煙霧測試與模擬 CLI 測試。執行時禁用插件、唯讀，不允許讀 `.env` 與外部目錄。預檢只回報是否有憑證，不保存帳號。 |
| 共通 | 重試與續跑仍檢查 Snapshot、截止時間、執行器與來源。所有執行器的輸出都不得含 `.env*`、`.envrc` 的值；CLI 回報的用量不是訂閱帳單。 |

Windows PowerShell 可執行單步 Python CLI；`start.sh` 的 PowerShell 對應入口尚未實作。

## 依賴與重建

依賴分兩份檔案，本機 `.venv` 與 Docker 都只安裝鎖定檔：

| 檔案 | 角色 | 誰改 |
| --- | --- | --- |
| `requirements.txt` | 人維護的「想要的版本範圍」（直接依賴，含 Skill 驗證用的 PyYAML 與 pytest） | 人 |
| `requirements.lock` | 由 `uv pip compile` 從 `requirements.txt` 解出的完整釘版（直接＋間接依賴、每個套件的 sha256），Dockerfile 以 `pip install --require-hashes` 安裝 | 只由指令重產，不手改 |

鎖定檔用 `--universal` 產生，同一份同時涵蓋 macOS 的 `.venv` 與 Linux 容器（x86_64／aarch64）；安裝只需要 pip，不需要 uv。

安裝或同步本機環境（與 Docker 完全相同的版本）：

```bash
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip check
```

### 更新依賴的流程

1. 改 `requirements.txt` 的版本範圍（新增、移除或放寬／收緊）。
2. 重產鎖定檔。預設只在範圍不再滿足時才改版本，其他套件維持原釘版；要刻意升級某個套件加 `--upgrade-package <套件>`，全部升級加 `--upgrade`：

   ```bash
   uv pip compile requirements.txt -o requirements.lock --generate-hashes --universal --python-version 3.12
   ```

3. 以鎖定檔重裝 `.venv` 並確認沒有衝突：`.venv/bin/python -m pip install --require-hashes -r requirements.lock && .venv/bin/python -m pip check`。
4. 跑完整測試與編譯檢查（`PYTHONPYCACHEPREFIX=/tmp/etf-agent-pycache .venv/bin/python -m pytest`、`compileall`、`git diff --check`）。
5. `./start.sh check` 會因 `requirements.lock` 變更自動重建映像並在容器內跑測試；強制重建用 `FORCE_DOCKER_BUILD=1 ./start.sh check`。
6. `requirements.txt` 與 `requirements.lock` 一起提交。

確認容器內版本：

```bash
docker compose run --rm agent python3 -c 'import yaml; print(yaml.__version__)'
```

## 手動 Docker 指令

```bash
docker compose build                                              # 建置映像
docker compose run --rm agent                                     # 專案設定檢查
docker compose run --rm agent python3 scripts/init_db.py          # 初始化資料庫
docker compose run --rm agent python3 scripts/collect_twse.py --all-listed   # 抓開發用全上市行情
docker compose run --rm agent python3 scripts/data_status.py      # 看資料狀態
docker compose run --rm agent python3 scripts/collect_latest_prices.py       # 官方交易池最新行情（需先填名單）
docker compose run --rm agent python3 scripts/collect_history.py             # yfinance 日線
docker compose run --rm agent python3 -m pytest                   # 測試
```

## 掛載資料夾

| 本機路徑 | 容器路徑 | 用途 |
| --- | --- | --- |
| `config/` | `/app/config` | 唯讀：競賽與資料來源設定 |
| `data/` | `/app/data` | 唯讀：官方交易池與 ETF 基準資料 |
| `var/` | `/app/var` | 可寫：SQLite 資料庫、資料包、日誌 |
| `artifacts/` | `/app/artifacts` | 可寫：報告、決策、帳本、D-Plan |

儀表板容器對 `var/`、`artifacts/` 都是唯讀。`compose.yaml` 預設用 macOS 常見的 UID/GID `501:20` 寫入本機資料夾；帳號不同時，在專案根目錄建立 `.env`：

```text
HOST_UID=你的使用者 UID
HOST_GID=你的群組 GID
```

直接用 `./start.sh` 會自動設定目前帳號的 UID/GID。

## 疑難排解

| 情況 | 處理方式 |
| --- | --- |
| 找不到 Docker | 安裝並啟動 Docker Desktop 後重試 |
| `Docker 尚未啟動` | 開啟 Docker Desktop，等引擎就緒後重試 |
| 容器顯示 `No module named yaml` | 執行 `docker compose build` 重建映像，再檢查 PyYAML 版本 |
| 無法寫入 `var/` 或 `artifacts/` | 使用 `./start.sh`，或檢查 `.env` 的 UID/GID |
| 抓取失敗 | 檢查網路，再執行 `./start.sh all` 重試 |
| 正式模式提示交易池空白 | 先把官方名單填入 `data/official_universe.csv` |
