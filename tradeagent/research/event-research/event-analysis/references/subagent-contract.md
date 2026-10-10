# Event Research 子 Agent 契約

一個事件使用一個 `DebateBundle`。四個 packet 都必須屬於同一 `run_id`、Snapshot、事件與股票。

```json
{
  "schema_version": "1.0",
  "run_id": "event-debate-001",
  "snapshot_id": "snapshot-id",
  "decision_cutoff": "2026-09-20T00:55:00+00:00",
  "event_id": "monthly-revenue:2330:2026-08",
  "symbol": "2330.TW",
  "packets": []
}
```

每個 packet 共用：

```json
{
  "packet_id": "event-debate-001:fact",
  "role": "fact",
  "event_id": "monthly-revenue:2330:2026-08",
  "symbol": "2330.TW",
  "input_packet_ids": [],
  "evidence_ids": ["document-evidence-id"],
  "output": {}
}
```

## 依賴規則

- `fact`：不讀其他 Agent packet，只讀 Snapshot 與工具結果。
- `bull`：`input_packet_ids` 只能是 `[fact_packet_id]`。
- `bear`：`input_packet_ids` 只能是 `[fact_packet_id]`；不得先讀 bull packet。
- `adjudicator`：必須讀 fact、bull、bear 三個 packet。
- 四個 `packet_id` 必須不同。任何角色缺漏、引用不存在、股票不一致或依賴順序錯誤，都由 `validate-debate` 拒絕。

## 各角色 output

`fact`：

```json
{
  "event_summary": "只含可核對事實的摘要",
  "verified_facts": [],
  "reference_frames": [],
  "open_questions": [],
  "price_features": {}
}
```

`bull`：

```json
{
  "thesis": "最強但不誇大的正面 thesis",
  "causal_chain": [],
  "assumptions": [],
  "catalysts": [],
  "failure_conditions": [],
  "unresolved_questions": []
}
```

`bear`：

```json
{
  "thesis": "最強反方 thesis",
  "causal_chain": [],
  "assumptions": [],
  "risks": [],
  "failure_conditions": [],
  "unresolved_questions": []
}
```

`adjudicator`：

```json
{
  "prevailing_case": "bull | bear | balanced | indeterminate",
  "direction": "positive | negative | mixed | neutral | uncertain",
  "research_status": "candidate | pending | excluded",
  "rationale": "逐項比較後的裁決",
  "status_reason": "交給下游的狀態理由",
  "surviving_claims": [],
  "rejected_claims": [],
  "unresolved_questions": []
}
```

## 執行與驗證

四個角色可以由 Codex／Claude 子 Agent 執行，不需要專案直接呼叫模型 API。保存 bundle 後執行：

```bash
.venv/bin/python cli/event_research.py validate-debate \
  --input artifacts/event_debate.json \
  --output artifacts/event_debate_validated.json
```

通過後，主控才可將裁決與 packet ID 寫入 `ResearchResult 2.1`；最終結果仍須再執行 `validate-result`。
