# ETF Agent CLI

這個目錄提供人工操作、排程器與 Skill 共用的單步命令入口，只解析參數並呼叫 `src/etf_agent/` 的核心服務，不複製資料蒐集、投資判斷或數值計算邏輯。

日常操作請用專案根目錄的 `./start.sh`（`auto` 更新資料、`daily` 準備帳本、`run` 完整每日決策、`status` 查資料狀態，無參數開儀表板；`./start.sh --help` 查看全部），見 [Docker 使用說明](../docs/docker.md)。`run` 會呼叫 `scripts/run_daily_pipeline.py`，以 `--agent-cli claude|codex|opencode` 選擇子 Agent CLI；可先執行 `./start.sh run --agent-cli opencode --preflight-only --skip-data` 驗證 OpenCode 已登入與必要檔案。下列單步入口供進階操作使用。

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

從專案根目錄執行，例如：

```bash
.venv/bin/python cli/data_agent.py status
# 已保存的官方狀態回應需先轉成 records／coverage，再固定 cutoff 建立 bundle
PYTHONPATH=src python3 cli/trading_status.py validate --input artifacts/trading-status/bundle.json

# 從封存 Decision run 自動整理 context 並匯出 D-Plan；帳本 run 必須是決策使用、且仍為 latest 的 prepare-day 封存。
# competition_profile.json 的策略與模型資訊必須先由人工確認，否則命令 fail-closed。
.venv/bin/python cli/dplan.py build --trade-date YYYY-MM-DD \
  --daily-run artifacts/daily_runs/YYYY-MM-DD/DAILY_RUN_ID/pipeline.json \
  --decision-repository artifacts/decision_runs --decision-run-id DECISION_RUN_ID \
  --virtual-account-account-id ai-cup-2026 --virtual-account-run-id PREPARE_RUN_ID

# 只測試自動 context 整理，不建立正式 D-Plan
.venv/bin/python cli/dplan.py build-context \
  --decision-repository artifacts/decision_runs --decision-run-id DECISION_RUN_ID \
  --daily-run artifacts/daily_runs/YYYY-MM-DD/DAILY_RUN_ID/pipeline.json \
  --output artifacts/competition/contexts/context_DECISION_RUN_ID.json
```

`tradeagent/` 定義 Codex／Claude／OpenCode 共用的工作流程、可讀輸入、停止條件與輸出格式；Skill 與人工操作都使用本目錄相同的 CLI，避免複製邏輯。

單步 CLI 的部分功能供離線驗證或 fixture 研究使用；不能因命令可執行就視為已接入每日正式決策。各來源的核准與限制見 [資料來源清單](../docs/data_sources_inventory.md)。
