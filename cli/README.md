# ETF Agent CLI

人工、排程器與 Skill 共用的單步命令入口：只解析參數並呼叫 `src/etf_agent/` 的核心服務，不複製資料蒐集、投資判斷或數值計算邏輯。

## 與 `start.sh` 的關係

| 項目 | 說明 |
| --- | --- |
| 日常入口 | 根目錄 `./start.sh`，見 [Docker 使用說明](../docs/docker.md) |
| `auto` | 更新資料 |
| `daily` | 準備帳本 |
| `run` | 完整每日決策，呼叫 `scripts/run_daily_pipeline.py` |
| `status` | 查資料狀態 |
| 無參數 | 開儀表板 |
| 子 Agent CLI | `run --agent-cli claude\|codex\|opencode` |
| 驗證 OpenCode | `./start.sh run --agent-cli opencode --preflight-only --skip-data` |

下表的單步入口供進階操作使用。

## 入口清單

| 入口 | 核心領域 |
| --- | --- |
| `data_agent.py` | 資料狀態、收集與 ResearchSnapshot |
| `trading_status.py` | 交易狀態 Bundle／TradabilityAssessment 建立與驗證 |
| `event_research.py` | 事件研究與 Debate／ResearchResult 驗證 |
| `sentiment_research.py` | 市場情緒、分析師共識與驗證 |
| `research_report.py` | Research Report V0 建立與重建驗證 |
| `portfolio_decision.py` | 動能、配置、風控、最終決策與封存 |
| `virtual_account.py` | `ensure-init` 首次開帳或驗證既有帳本；其餘命令處理決策前快照與模擬成交 |
| `dplan.py` | 從封存決策自動整理 D-Plan context、匯出 v4.3 候選（核對 prepare-day 帳本）與本地結構檢查 |
| `decision_delivery.py` | 從封存 Decision run 建立風控後中文 JSON／Markdown；重建驗證及拒絕診斷，不產 D-Plan |
| `fundamental_research.py` | 基本面研究的唯讀資料包、指標、結果驗證與封存 |
| `fundamental_repair.py` | 資料不足時補抓官方當期與 FinMind 去年同期累計財報；不執行分析或交易 Agent |
| `market_news.py` | 全市場新聞補抓（`capture`／`backfill`）與已判讀輸入接入（`integrate`） |
| `market_fear_greed.py` | 台股恐懼貪婪指標來源補抓（`capture`）與截止前分數檢視（`show`） |
| `news_sentiment.py` | 核對資料庫新聞候選、保存原頁版本並驗證全量情緒標註 |
| `market_context_probe.py` | 補抓公開大盤成交、指數與法人資料（交易背景），不產生情緒結論 |
| `calibration.py` | 校準旁觀者：`snapshot` 記錄封存決策、`report` 以官方成交均價對答案（少於 20 個決策日不給結論）、`gap-audit` 審計決策價與成交均價的偏離；不影響決策 |
| `dashboard.py` | 啟動唯讀虛擬帳戶績效儀表板（FastAPI），`./start.sh` 預設模式使用 |

從專案根目錄執行，例如：

```bash
.venv/bin/python cli/data_agent.py status
# 已保存的官方狀態回應需先轉成 records／coverage，再固定 cutoff 建立 bundle
PYTHONPATH=src python3 cli/trading_status.py validate --input artifacts/trading-status/bundle.json

# 完整每日決策會自動匯出 D-Plan；此入口只重試最新 approved/no_trade 決策，不重跑 Agent。
# competition_profile.json 的策略與模型資訊必須先由人工確認，否則命令 fail-closed。
docker compose run --rm agent python3 cli/dplan.py build-latest

# 以下為歷史重建或除錯用的進階入口。
.venv/bin/python cli/dplan.py build-context \
  --decision-repository artifacts/decisions --decision-run-id DECISION_RUN_ID \
  --daily-run artifacts/daily/YYYY-MM-DD/DAILY_RUN_ID/pipeline.json \
  --output artifacts/competition_d_plan/contexts/context_DECISION_RUN_ID.json
```

## 規則

- `tradeagent/` 的 Skill 與人工操作使用本目錄相同的 CLI。
- 部分單步 CLI 供離線驗證或 fixture 研究使用，可執行不代表已接入每日正式決策。
- 各來源的核准狀態見 [資料來源清單](../docs/data_sources_inventory.md)。
