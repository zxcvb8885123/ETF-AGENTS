# Market Perception 契約 1.0

`PerceptionDataBundle` 是已保存、已授權且受 `decision_cutoff` 限制的輸入。`MarketPerceptionResult` 是 Agent 的研究輸出。兩者的 `snapshot_id` 與 `decision_cutoff` 必須完全一致。

## PerceptionDataBundle

必要欄位：

- `schema_version="1.0"`、`bundle_id`、`snapshot_id`、`decision_cutoff`、`generated_at`。
- `source_coverage`：每個 provider 的 `channel`、`status` 與 `license_status`。
- `source_evidence`：穩定 `evidence_id`、股票、來源、URL、發布／可得時間及內容雜湊。
- `sentiment_items`：原始項目、公司映射、canonical content ID 與證據引用。
- `analyst_estimates`：逐位貢獻者、指標、預估期間、數值、單位、幣別及證據引用。

Bundle 不得包含 cutoff 後才可得的資料。`generated_at` 可以晚於 cutoff，表示在何時封裝這份歷史資料；內容本身仍以 `available_at` 受限。

## Sentiment label

```json
{
  "item_id": "sentiment-1",
  "symbol": "2330.TW",
  "evidence_id": "sentiment-evidence-1",
  "relevance": "relevant",
  "stance": "positive",
  "rationale": "內容明確討論該公司且對事件持正向看法。",
  "model_version": "codex-current"
}
```

`relevance` 只能是 `relevant`、`ambiguous`、`irrelevant`；`stance` 只能是 `positive`、`negative`、`neutral`、`mixed`。標籤必須完整覆蓋查詢視窗內的項目，避免選擇性取樣；聚合器只使用 `relevant`，並依 `canonical_content_id` 去重。

## MarketPerceptionResult

```json
{
  "schema_version": "1.0",
  "run_id": "perception-run-1",
  "snapshot_id": "snapshot-1",
  "decision_cutoff": "2026-09-20T00:55:00+00:00",
  "skill_version": "1.0.0",
  "status": "completed",
  "items": [
    {
      "symbol": "2330.TW",
      "event_result_ids": ["research-run-1"],
      "sentiment_labels": [],
      "sentiment": {},
      "consensus_metrics": [],
      "expectation_gaps": [],
      "priced_in_assessment": "unknown",
      "rationale": "可核對的簡短判讀。",
      "evidence_ids": [],
      "risk_flags": [],
      "research_status": "pending",
      "status_reason": "資料不足。"
    }
  ],
  "errors": []
}
```

### 固定列舉

- `status`：`completed`、`degraded`、`failed`。
- `research_status`：`usable_secondary`、`pending`、`unavailable`、`excluded`。
- `priced_in_assessment`：`underappreciated`、`aligned`、`crowded`、`contradicted`、`unknown`。
- 情緒通道狀態：`available`、`insufficient`、`unavailable`。
- 預期差：`above`、`below`、`in_line`、`unavailable`。

## 數值不變量

- `sentiment` 必須逐欄等於 `aggregate-sentiment` 的輸出。
- 每個 `consensus_metrics` 項目必須逐欄等於 `compute-consensus-revision` 的輸出。
- 每個 `expectation_gaps` 項目必須逐欄等於 `compare-event-expectations` 的輸出，而且驗證時要提供原始、已驗證的 `ResearchResult`。
- `evidence_ids` 必須包含所有被情緒與共識工具採用的來源。
- `usable_secondary` 至少需要一個 `available` 的情緒或共識通道。

這個結果只代表市場認知層的次級輸入。下游必須同時保留事件研究、資料品質及風控結果，不得由本結果直接產生交易。
