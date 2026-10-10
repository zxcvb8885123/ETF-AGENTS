# ResearchResult 2.1 契約

## 頂層

```json
{
  "schema_version": "2.1",
  "run_id": "event-research-20260920-001",
  "snapshot_id": "Data Agent Snapshot ID",
  "decision_cutoff": "2026-09-20T08:55:00+08:00",
  "skill_version": "2.1.0",
  "status": "completed",
  "items": [],
  "errors": []
}
```

`status`：`completed`、`degraded`、`failed`。`completed` 不得同時有 `errors`。

## 事件研究物件

```json
{
  "event_id": "monthly-revenue:2330:2026-08",
  "symbol": "2330.TW",
  "event_type": "monthly_revenue",
  "published_at": "2026-09-14T16:00:00+00:00",
  "catalyst_date": null,
  "event_summary": "公司公告八月營收；歷史比較改善，但沒有市場共識資料。",
  "research_process": {
    "fact_packet_id": "event-debate-001:fact",
    "bull_packet_id": "event-debate-001:bull",
    "bear_packet_id": "event-debate-001:bear",
    "adjudication_packet_id": "event-debate-001:adjudicator",
    "independence": "bull_bear_independent"
  },
  "direction": "positive",
  "impact_mechanism": "若成長由出貨量而非低毛利產品組合推動，可能改善本季營收預期；毛利影響仍待確認。",
  "evidence_ids": ["document-evidence-1"],
  "counter_evidence_ids": [],
  "fact_values": [
    {
      "name": "monthly_revenue_yoy_pct",
      "value": "12.34",
      "unit": "percent",
      "period": "2026-08",
      "evidence_id": "document-evidence-1",
      "comparison_basis": "previous_year_same_month"
    }
  ],
  "assessment": {
    "evidence_quality": "verified",
    "novelty": {
      "classification": "new",
      "rationale": "本期首次發布，並非相同公告的重複轉載。",
      "prior_event_ids": []
    },
    "reference_frames": [
      {
        "kind": "prior_year_same_month",
        "actual": "100",
        "reference": "89",
        "difference_pct": "12.34",
        "unit": "TWD x 1000",
        "is_market_expectation": false,
        "evidence_ids": ["document-evidence-1"]
      }
    ],
    "materiality": {
      "level": "medium",
      "affected_metrics": ["revenue", "gross_margin"],
      "causal_chain": [
        {
          "from": "月營收年增",
          "to": "本季營收",
          "relationship": "若九月未反轉，八月成長會提高本季營收基期",
          "support": "inference",
          "evidence_ids": ["document-evidence-1"]
        }
      ],
      "horizon": "within_competition",
      "rationale": "下一次月營收與市場重新定價均在比賽期間內。"
    }
  },
  "debate": {
    "bull_case": {
      "thesis": "成長若具延續性，短期營收預期可能上修。",
      "evidence_ids": ["document-evidence-1"],
      "assumptions": ["成長不是一次性認列"],
      "failure_conditions": ["下一期年增明顯反轉"]
    },
    "bear_case": {
      "thesis": "只有營收資料，無法證明產品組合與毛利同步改善。",
      "evidence_ids": ["document-evidence-1"],
      "assumptions": ["低毛利產品可能貢獻較多成長"],
      "failure_conditions": ["公司展望確認毛利率改善"]
    },
    "adjudication": {
      "prevailing_case": "bull",
      "rationale": "營收方向可確認，但只保留短期營收改善主張，不延伸成獲利上修。",
      "surviving_claims": ["短期營收動能改善"],
      "rejected_claims": ["毛利必然同步改善"],
      "unresolved_questions": ["市場原先預期與產品組合"]
    }
  },
  "price_confirmation": {
    "status": "unconfirmed",
    "interpretation": "事件後尚無放量上漲，因此交由下游等待。",
    "as_of": "2026-09-19",
    "event_trade_date": "2026-09-15",
    "pre_event_return_5d": 0.02,
    "event_day_return": 0.003,
    "post_event_return_to_cutoff": 0.004,
    "event_volume_ratio_20d_median": 0.9,
    "source_evidence_id": "price-evidence-1"
  },
  "risk_flags": ["NO_MARKET_EXPECTATION"],
  "uncertainties": ["缺少市場共識與產品組合資料"],
  "research_status": "candidate",
  "status_reason": "事件通過證據、新穎性、重大性、多空裁決與期間閘門；價格仍待確認。",
  "invalidation_signals": ["下一期月營收年增反轉", "公司展望下修"]
}
```

## 列舉

- `direction`：`positive`、`negative`、`mixed`、`neutral`、`uncertain`
- `research_status`：`candidate`、`pending`、`excluded`
- `evidence_quality`：`verified`、`partial`、`insufficient`
- `novelty.classification`：`new`、`update`、`repeat`、`correction`、`unclear`
- `materiality.level`：`high`、`medium`、`low`、`unknown`
- `materiality.horizon`：`within_competition`、`after_competition`、`unknown`
- `adjudication.prevailing_case`：`bull`、`bear`、`balanced`、`indeterminate`
- `price_confirmation.status`：`confirmed`、`unconfirmed`、`contradicted`、`unavailable`

## 重要不變量

- 引用、數值、事件時間、Snapshot 與行情特徵均由程式核對。
- `research_process` 必須列出四個不同的 packet ID，並宣告 `bull_bear_independent`；完整依賴關係由 `validate-debate` 驗證。
- `reference_frames.is_market_expectation=false` 時，不得在文字中寫「優於／低於市場預期」；候選另加 `NO_MARKET_EXPECTATION`。
- `causal_chain` 每一段都標示 `fact` 或 `inference`，並引用同公司證據。
- 多方與空方都必須存在；候選方向必須和裁決一致。
- 候選需有 verified 證據、非重複事件、high／medium 重大性、比賽期間內影響、至少一個基準、可用且不矛盾的行情，以及失效條件。
- Validator 驗的是資料與結構一致性，不宣稱投資 thesis 一定正確；研究品質仍須用人工標記事件集與時間點回測評估。
