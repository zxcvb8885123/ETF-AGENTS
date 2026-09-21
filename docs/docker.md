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

腳本會自動建置映像、檢查設定、初始化 SQLite、抓行情，再顯示資料庫狀態。正式模式會先抓 TWSE／TPEx 最新行情，再把 150 檔兩年歷史行情增量更新至兩個官方市場都已完成的最近交易日；官方交易池還空著時，則使用開發模式抓取 TWSE 最新行情端點的全部可解析證券。

```bash
./start.sh official  # 只抓官方交易池；空白時停止
./start.sh all       # 開發模式：抓全部 TWSE 最新行情
./start.sh check     # 建置映像並執行設定檢查與測試
```

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
docker compose run --rm agent python3 scripts/collect_official_history.py
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
| 無法寫入 `var/` 或 `artifacts/` | 使用 `./start.sh`，或檢查 `.env` 的 UID/GID |
| 抓取失敗 | 檢查網路，再執行 `./start.sh all` 重試 |
| 正式模式提示交易池空白 | 先把官方名單填入 `data/official_universe.csv` |
