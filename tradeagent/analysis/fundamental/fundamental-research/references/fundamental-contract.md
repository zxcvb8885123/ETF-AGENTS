# Fundamental Research 1.0 契約

首版由四個有內容雜湊的 JSON artifact 組成：

| Artifact | 產生者 | 用途 |
| --- | --- | --- |
| `FundamentalDataBundle 1.0` | Python | 從固定 Snapshot 選取財報、固定覆蓋分母、期間、來源與缺口 |
| `FundamentalMetrics 1.0` | Python | 從 Bundle 重算有公式版本的 Decimal 指標 |
| `FundamentalResearchResult 1.0` | 研究 Agent + Validator | 只引用 Bundle 證據與可用 Metrics 的研究解讀 |
| 封存結果 | Python CLI | 已驗證 Result 的不覆寫副本 |

## 請求與資料包

`FundamentalResearchRequest` 固定包含 `request_id`、已排序去重的 `symbols`、`fiscal_year`、`fiscal_quarter`、損益表及資產負債表、已排序的 `metric_keys` 與 `policy_version`。請求股票永遠是覆蓋分母，缺資料保留在 `missing_data`。

Bundle 的 `snapshot_id`、`snapshot_sha256`、`decision_cutoff`、財報版本、`source_evidence_id`、來源內容雜湊與 `available_at` 都可追溯。只接受 cutoff 前可得的財報。必要當期報表缺失或業別不支援為公司 `unavailable`；可研究當期但缺去年同期比較資料為 `degraded`。

## 指標

| metric_key | 計算 | 單位 |
| --- | --- | --- |
| `operating_margin_pct` | 營業利益／營收 × 100 | percent |
| `liabilities_to_assets_pct` | 總負債／總資產 × 100 | percent |
| `revenue_yoy_pct` | （本期營收－去年同期營收）／去年同期營收 × 100 | percent |
| `net_income_yoy_pct` | （本期淨利－去年同期淨利）／去年同期淨利 × 100 | percent |
| `operating_margin_pp_yoy` | 本期營業利益率－去年同期營業利益率 | percentage_points |

分母小於或等於零、事實未提供、幣別不一致或期間不相同時輸出 `status=unavailable` 與明確 `reason_code`。資料倍率由 Python 正規化，結果使用 Decimal 字串；不得將百分比與百分點混用。

## Result 草稿

草稿頂層必須包含：

```json
{
  "schema_version": "1.0",
  "run_id": "fundamental-run-001",
  "snapshot_id": "...",
  "decision_cutoff": "...",
  "bundle_id": "...",
  "bundle_sha256": "...",
  "metrics_id": "...",
  "metrics_sha256": "...",
  "skill_version": "1.0.0",
  "status": "completed",
  "items": [],
  "errors": []
}
```

每個 `items` 項目依 request 的股票順序，使用下列白名單欄位：

```json
{
  "symbol": "2330.TW",
  "research_status": "completed",
  "status_reason": "當期與去年同期資料可比。",
  "observations": [
    {
      "text": "營業利益率與去年同期比較時需注意累計期間口徑。",
      "evidence_ids": ["document:101"],
      "metric_ids": ["2330.TW:operating_margin_pct:2026Q2"]
    }
  ],
  "assumptions": ["官方欄位映射版本在本次比較期間一致。"],
  "invalidation_conditions": ["後續官方更正使既有財報版本失效。"],
  "limitations": [],
  "evidence_ids": ["document:101"],
  "metric_ids": ["2330.TW:operating_margin_pct:2026Q2"]
}
```

`research_status` 由 Python metrics 結果決定。item 頂層引用必須與 observations 的去重引用完全相同；`metric_ids` 只能指向同股票的可用指標，`evidence_ids` 只能指向同股票 Bundle 財報。Validator 會拒絕未知欄位與交易欄位，並補上或驗證 `content_sha256`。
