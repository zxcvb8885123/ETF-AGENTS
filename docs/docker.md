# Docker 使用說明

本專案是 CLI Agent，Docker 只負責一致的執行環境，不啟動網頁或 API 服務。

## 前置條件

安裝並開啟 Docker Desktop。確認 Docker 可用：

```bash
docker info
docker compose version
```

## 最快使用方式

在專案根目錄執行：

```bash
./start.sh
```

首次執行會建置映像；之後僅在映像不存在、`requirements.txt` 或 Dockerfile 變更時重建。`src/`、`cli/`、`scripts/`、`skills/`、`tests/` 從工作區唯讀掛載，程式碼修改後可直接執行。正式模式會抓 TWSE／TPEx 帳本所需的最新官方價量，再以 yfinance 將 150 檔研究日線增量更新至台北當日；資料專用排程不補官方個股月行情，`daily` 決策只補上市個股價量，上櫃個股月擷取器已移除。官方交易池還空著時，則使用開發模式抓取 TWSE 最新行情端點的全部可解析證券。

Dockerfile 與本機 `.venv` 共用 `requirements.txt`，其中包含 Skill 驗證使用的 PyYAML。修改依賴後重新執行 `./start.sh check` 就會自動重建；可用 `docker compose run --rm agent python3 -c 'import yaml; print(yaml.__version__)'` 確認新映像已安裝。
手動要求重建可執行 `FORCE_DOCKER_BUILD=1 ./start.sh check`。Dockerfile 以 pip 下載快取加快後續依賴更新；首次下載仍取決於網路速度。

```bash
./start.sh official  # 只抓官方交易池；空白時停止
./start.sh all       # 開發模式：抓全部 TWSE 最新行情
./start.sh check     # 建置映像並執行設定檢查與測試
./start.sh data      # 每日資料擷取，不呼叫決策 Agent 或推進帳本
./start.sh daily     # 一鍵驗證來源、更新行情／事件、建立 Snapshot 並推進虛擬帳本
```

`daily` 會先驗證官方來源與交易池，再更新官方帳本價量、yfinance 研究日線、月營收／重大訊息與財報；公司分類及 FinMind 三大報表每日擷取為候選資料。個股新聞另由平日 22:00 的 FinMind 排程擷取，來源授權與時間尚未核准。yfinance 截止日若未覆蓋 150 檔即停止。最後以 `DAILY_CUTOFF`（未設定時使用 Asia/Taipei 現在時間）建立 Snapshot 並推進虛擬帳本；帳本推進失敗時以非零狀態結束。決策鏈由 `scripts/run_daily_pipeline.py` 接續執行。不會自動產生或送出訂單。

`data` 執行 yfinance 150 檔日線、官方財報與公司資料、FinMind 三大報表候選的擷取及狀態檢查，略過 TWSE／TPEx 價格 OpenAPI、Snapshot、帳本與決策。Yahoo 當日缺檔不會跳過其他來源，最後以非零狀態回報；缺 FinMind 憑證或候選來源失敗也回報降級。macOS `com.etf-agents.data` 於台北時間平日 20:00 執行 `data`；`com.etf-agents.finmind-news` 於 22:00 執行個股新聞候選擷取。樣板在 `scripts/launchd/`，日誌在 `artifacts/daily_runs/`。FinMind Token 依序讀取執行環境、專案根目錄的 `.env`（`FINMIND_TOKEN=...`、權限 600、Git 忽略）；財報流程還可回退至 macOS 鑰匙圈服務 `etf-agent-finmind`。

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
