---
name: daily-report
description: 執行或續跑 ETF Agent 的按需研究與報告交付工作流，封存事件候選、交接 Fact／Bull／Bear／Adjudicator，並產生可驗證的 Research Report 或等待／失敗狀態報告。不得捏造研究結果、放寬時間或風控，也不負責平台送件與下單。
---

# 自動化報告 Agent

使用 `cli/report_workflow.py` 作為固定入口。確定性工作流負責 Snapshot、事件候選、輸入版本、結果驗證、JSON／Markdown 產物與 manifest；需要研究推論時，才交給隔離的 Codex／Claude 工作階段。

## 執行

```bash
.venv/bin/python cli/report_workflow.py run \
  --snapshot artifacts/research_snapshot_latest.json \
  --database var/etf_agent.db \
  --research artifacts/event_research_validated.json \
  --repository artifacts/report_runs \
  --reports artifacts/reports \
  --execution-mode official
```

回傳 `succeeded` 時，報告在 `artifacts/report_runs/<workflow_run_id>/`，入口索引在 `artifacts/reports/latest.md`。`waiting_for_agent` 表示候選已封存、等待研究輸出；`waiting_for_decision` 表示 Research Report 已完成但尚未提供同一 Snapshot／cutoff 的 Portfolio Decision／Risk run；`blocked` 表示必要資料不存在；`failed` 表示驗證或輸入錯誤。等待與阻擋都必須保留執行紀錄，不得回填舊結果。

## 研究交接

1. 讀取封存的 `agent_request.json`、`input_snapshot.json` 與 `event_candidates.json`。
2. Fact 只整理引用文件中的可驗證事實。
3. Bull 與 Bear 只讀同一份 FactPacket，使用隔離工作階段，彼此不得讀取對方輸出。
4. Adjudicator 只能裁決既有事實與推論，不新增事實。
5. 以 `cli/event_research.py validate-debate` 與 `validate-result` 驗證後，將 ResearchResult 保存至工作流要求的路徑。
6. 使用 `report_workflow.py resume --from-run-id WAITING_RUN_ID` 續跑，產生新的 attempt 並保留父執行紀錄。

## 接入決策與 DailyReport

Research Report 通過後，提供同一 Snapshot／cutoff 的已驗證 Decision run，工作流才會呼叫既有 DailyReport／FailureReport Builder：

```bash
.venv/bin/python cli/report_workflow.py resume \
  --from-run-id RESEARCH_RUN_ID \
  --research artifacts/event_research_validated.json \
  --decision-repository artifacts/portfolio_decisions \
  --decision-run-id DECISION_RUN_ID \
  --virtual-account-repository artifacts/virtual_accounts \
  --virtual-account-account-id ai-cup-2026 \
  --virtual-account-run-id PREPARED_ACCOUNT_RUN_ID \
  --daily-report-repository artifacts/pipeline_runs
```

正式模式的帳戶 run 必須是目前 latest 的已封存 `prepare-day` 狀態；其 Snapshot 全文、ID、cutoff 與 DecisionInputBundle 內的 AccountSnapshot 都必須逐項吻合，且 run manifest 必須通過雜湊驗證。缺少或不一致時在呼叫報告產生器前停止。Fixture 模式可省略虛擬帳戶參數，但不得作正式競賽交付。

成功時同一 workflow run 會封存 `daily_report.json` 與 `daily_report_markdown.md`；前置資料或風控失敗時封存 `failure_report.json`。這個入口只交付報告，不送件、不下單。

## 邊界

- `Research Report V0` 不包含配置、權重、股數、費稅、訂單或 D-Plan。
- 缺少合法市場情緒／分析師資料時允許降級，但不能以摘要、記憶或公司財測補造共識。
- 零事件文件只代表資料輸入為零，不能推論市場沒有事件。
- 正式 DailyReport 仍需通過 Portfolio Decision／Risk 與正式帳戶、交易狀態及規則前置驗收。
- 本 Skill 不會登入券商、送件、下單、修改程式或自動通知外部服務。
