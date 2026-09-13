# ETF Agent Manager

AI CUP 2026「Agent 基金經理人」的自動化 Agent。目標是每天完成資料蒐集、投資決策、組合／資金風控，最後產生可提交的決策報告與交易書。

```text
資料蒐集 → 策略決策 → 組合／資金風控 → 報告與交易提交
```

## 快速開始

```bash
./start.sh
```

這會建立 Docker 映像、初始化 SQLite、抓取 TWSE 最新行情並顯示資料狀態。

| 指令 | 用途 |
| --- | --- |
| `./start.sh` | 自動模式：有官方交易池時抓正式資料，否則抓開發資料 |
| `./start.sh official` | 只抓官方交易池；名單空白時停止 |
| `./start.sh all` | 開發模式：抓 TWSE 最新行情端點的全部可解析證券 |
| `./start.sh check` | 只建置、檢查與執行測試 |

## 目前完成

- SQLite：保存行情、原始 TWSE 回應、抓取時間及執行紀錄。
- TWSE 最新交易日行情收集器。
- 官方交易池 CSV 讀取與篩選。
- 競賽基本風控：持股檔數、現金、個股權重、交易池與 Active Share。
- Docker 與快速啟動流程。

目前尚未接上歷史行情、上櫃行情、基本面、新聞、策略模型、每日報告和主辦平台送件。

## 資料位置

| 路徑 | 用途 |
| --- | --- |
| `var/etf_agent.db` | SQLite 資料庫 |
| `data/official_universe.csv` | 官方 150 檔交易池，公布後填入 |
| `data/active_etf_top10.csv` | Active Share 的 ETF 前十大持股資料 |
| `artifacts/` | 後續每日報告、交易書與稽核檔案 |

資料庫查詢、Docker 指令與容器設定請參閱下方的 Docker 使用說明。

## 文件

| 文件 | 內容 |
| --- | --- |
| [Agent 開發規劃](docs/agent_plan.md) | 三個核心模組、開發順序與第一版驗收標準 |
| [第一版技術架構](docs/architecture_v1.md) | 模組職責、資料契約、流程及實作里程碑 |
| [Docker 使用說明](docs/docker.md) | 建置、容器指令、掛載與疑難排解 |

## 專案結構

```text
config/                 競賽與資料來源設定
data/                   官方交易池與 ETF 基準資料
docs/                   規劃、架構與操作文件
scripts/                初始化、收集與狀態查詢指令
src/etf_agent/          Agent 核心程式
tests/                  單元測試與測試資料
var/                    SQLite 資料庫（不納入 Git）
artifacts/              每日輸出檔案（不納入 Git）
```
