# ETF Agent CLI

這個目錄是人工操作、排程器與 Skill 共用的穩定命令入口。每個檔案只解析參數並呼叫 `src/etf_agent/` 的核心服務，不含資料蒐集、投資判斷或數值計算邏輯。

| 入口 | 核心領域 |
| --- | --- |
| `data_agent.py` | 資料狀態、收集與 ResearchSnapshot |
| `trading_status.py` | 交易狀態 Bundle／TradabilityAssessment 建立與驗證 |
| `event_research.py` | 事件研究與 Debate／ResearchResult 驗證 |
| `sentiment_research.py` | 市場情緒、分析師共識與驗證 |
| `research_report.py` | Research Report V0 建立與重建驗證 |
| `portfolio_decision.py` | 動能、配置、風控、最終決策與封存 |
| `backtest.py` | 歷史重播、帳務驗證與回測報告 |
| `daily_report.py` | 離線 DailyReport／FailureReport pipeline run |

從專案根目錄執行，例如：

```bash
.venv/bin/python cli/data_agent.py status
# 已保存的官方狀態回應需先轉成 records／coverage，再固定 cutoff 建立 bundle
PYTHONPATH=src python3 cli/trading_status.py validate --input artifacts/trading-status/bundle.json
```

`skills/` 只定義 Codex／Claude 的工作流程、可讀輸入、停止條件與輸出格式；Skill 與人工操作都使用本目錄相同的 CLI，避免複製邏輯。
