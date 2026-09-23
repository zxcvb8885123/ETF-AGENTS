# ResearchReport 1.0 契約

ResearchReport V0 只整理研究層輸出，不包含配置、訂單或正式送件內容。

## 頂層

```json
{
  "schema_version": "1.0",
  "report_id": "research-report-001",
  "generated_at": "2026-09-21T01:00:00+00:00",
  "snapshot_id": "snapshot-001",
  "decision_cutoff": "2026-09-21T00:55:00+00:00",
  "status": "completed",
  "title": "每日研究報告",
  "input_refs": {
    "research_run_id": "research-run-001",
    "perception_run_id": null
  },
  "data_quality": {},
  "coverage": {},
  "companies": [],
  "sources": [],
  "missing_data": [],
  "limitations": []
}
```

`status` 只能是：

- `completed`：所有提供的上游結果完成，且有 Market Perception。
- `degraded`：未提供 Perception、上游標記 degraded 或 Snapshot 有品質旗標。

## Company

每家公司包含：

- `symbol`
- `latest_price`：日期、分析價格及行情 evidence ID；缺少時為 `null`。
- `events`：直接整理 Event Research 的事實、多空 thesis、裁決、行情確認與失效條件。
- `perception`：情緒、共識、預期差與已反映判讀；未提供時固定為 `unavailable`。
- `risk_flags`、`missing_data`、`source_evidence_ids`

## Sources

只保留報告實際使用的來源。Snapshot 與 Perception 的來源會被正規化為：

```json
{
  "evidence_id": "evidence-1",
  "domain": "snapshot",
  "symbol": "2330.TW",
  "source": "TWSE_MOPS_MONTHLY_REVENUE",
  "authority": "mops",
  "data_type": "monthly_revenue",
  "url": "https://example.invalid/source",
  "published_at": "2026-09-14T16:00:00+00:00",
  "available_at": "2026-09-15T00:00:00+00:00",
  "content_as_of": "2026-08"
}
```

跨資料域出現相同 `evidence_id` 但內容不同時必須拒絕。

## 不變量

- Snapshot、ResearchResult、PerceptionResult 與 PerceptionBundle 使用相同 snapshot 和 cutoff。
- Event evidence 必須存在於 Snapshot；Perception evidence 必須存在於 Bundle。
- JSON 的敘述與數字都來自上游固定欄位。
- Validator 使用原始輸入與報告本身的 `report_id`／`generated_at` 重建完整 JSON；任何差異都拒絕。
- Markdown 只能從已驗證 JSON 渲染，而且必須逸出 Markdown 控制字元。
- 不得加入 `decision`、`orders`、`target_weight`、`shares` 或等價交易欄位。
