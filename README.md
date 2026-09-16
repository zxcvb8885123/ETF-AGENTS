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
| `python3 scripts/run_strategy.py --input snapshot.json` | 以研究快照執行事件策略 V1 |
| `.venv/bin/python scripts/collect_history.py` | 透過 yfinance 抓取官方 150 檔最近兩年日線 |
| `.venv/bin/python skills/event-data/scripts/data_agent.py collect` | 抓取官方月營收與重大訊息 |
| `.venv/bin/python skills/event-data/scripts/data_agent.py snapshot --output artifacts/research_snapshot_latest.json` | 建立目前時間的研究快照 |

## 目前完成

- SQLite：保存行情、原始 TWSE 回應、抓取時間及執行紀錄。
- TWSE 最新交易日行情收集器。
- 最近兩年歷史行情收集與 TWSE／TPEx 缺漏備援。
- TWSE／TPEx 月營收與重大訊息收集、原始回應保存及版本去重。
- 指定截止時間的不可變研究快照與資料品質旗標。
- 官方交易池 CSV 讀取與篩選。
- 事件策略 V1：事件評分、價格確認及進攻／防守配置。
- 競賽基本風控：持股檔數、現金、個股權重、交易池與 Active Share。
- Docker 與快速啟動流程。

目前尚未接上季報、法說、新聞、LLM 公告分類、完整回測、每日報告和主辦平台送件。

目前開發優先級是 Data Agent M0：把來源可行性測試做成可重跑探測、每日驗證 150 檔交易池，並在代號或可交易狀態未確認時阻止正式 Snapshot。完成 M0 後才依序進行 TPEx／財報／交易狀態（M1）、新聞候選（M2）與 Codex／Claude Skill 工具循環（M3）。

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
| [Agent 開發架構](docs/agent_plan.md) | 資料庫、事件與動能策略、進攻／防守分類、風控買賣及報告流程 |
| [Data Agent 計畫](docs/data_agent_plan.md) | 資料收集、補查、驗證、版本保存與研究快照 |
| [資料來源可行性測試](docs/source_feasibility_2026-09-17.md) | 官方行情、財報、事件與新聞來源的實測結果及接入判定 |
| [第一版技術架構](docs/architecture_v1.md) | 模組職責、資料契約、流程及實作里程碑 |
| [事件策略 V1](docs/event_strategy_v1.md) | 策略層目前優先實作的事件資料、價格確認與候選清單 |
| [回測計畫 V1](docs/backtest_plan_v1.md) | 歷史重播、成交模擬、策略比較與有效性驗證 |
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
