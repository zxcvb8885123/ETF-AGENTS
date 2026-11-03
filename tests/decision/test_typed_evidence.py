"""證據種類版本閘門（bundle.evidence_policy = typed_evidence_v1）：

- 無 symbol 的市場層級／帳戶證據只能作個股 claim／finding 的補充，不能單獨成立；
- claim_kind／finding_kind 必須與引用的證據種類對映；
- 沒有閘門的舊 bundle 維持「引用存在且屬該股」的原行為，出現種類欄位即拒絕。
"""

import copy
import unittest

from etf_agent.decision import (
    CLAIM_KINDS,
    MARKET_REGIME_EVIDENCE_ID,
    AnalystReportValidator,
    DecisionInputValidator,
    DecisionToolError,
    MomentumEngine,
    StancePacketValidator,
    build_stance_brief,
    canonical_sha256,
    decision_bundle_sha256,
    evidence_policy,
    seal_stance_packet,
    sentiment_unavailable_report,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.input_validator import EVIDENCE_POLICY_FIELD, EVIDENCE_POLICY_TYPED

from test_analysts import EVENT_ID, bundle_with_event, report
from test_daily_pipeline import degraded_research_result

ACCOUNT_ID = "account-evidence-1"


def typed_bundle(bundle=None):
    bundle = copy.deepcopy(bundle or bundle_with_event())
    bundle[EVIDENCE_POLICY_FIELD] = EVIDENCE_POLICY_TYPED
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def finding(finding_id, evidence, kind="price"):
    return {"finding_id": finding_id, "text": "依摘要判斷。", "evidence_ids": [evidence], "finding_kind": kind}


def claim(claim_id, evidence, findings=(), kind="price"):
    return {"claim_id": claim_id, "text": "依分析報告。", "evidence_ids": list(evidence) if isinstance(evidence, list) else [evidence],
            "finding_ids": list(findings), "claim_kind": kind}


def typed_reports(bundle):
    technical = [
        {"symbol": "2317.TW", "outlook": "positive", "findings": [finding("tech-2317-1", "price-2317")], "data_gaps": []},
        {"symbol": "2330.TW", "outlook": "neutral", "findings": [finding("tech-2330-1", "price-2330")], "data_gaps": []},
    ]
    unknown = [{"symbol": s, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]} for s in ("2317.TW", "2330.TW")]
    event = [
        {"symbol": "2317.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_MATERIAL_EVENT_IN_WINDOW"], "events": []},
        {"symbol": "2330.TW", "outlook": "positive", "findings": [finding("event-2330-1", EVENT_ID, "material_event")], "data_gaps": [],
         "events": [{"evidence_id": EVENT_ID, "materiality": "high", "summary": "董事會核准資本預算。"}]},
    ]
    return {
        "technical": report(bundle, "technical", technical),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": report(bundle, "event", event),
        "sentiment": sentiment_unavailable_report(bundle),
    }


def bull_items():
    return [
        {"symbol": "2317.TW", "strength": "moderate", "claims": [claim("bull-2317-1", "price-2317", ["tech-2317-1"])], "invalidation_conditions": []},
        {"symbol": "2330.TW", "strength": "weak", "claims": [claim("bull-2330-1", EVENT_ID, ["event-2330-1"], "material_event")], "invalidation_conditions": []},
    ]


def stance_world():
    bundle = typed_bundle()
    momentum = MomentumEngine(bundle).run()
    reports = typed_reports(bundle)
    research = degraded_research_result(bundle["snapshot"], "research-1")
    envelope = build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"]
    return bundle, momentum, reports, research, envelope


class EvidenceKindContextTests(unittest.TestCase):
    def test_kinds_are_derived_from_sources_and_gate_registers_market_ids(self):
        context = DecisionContext(typed_bundle())
        self.assertEqual(context.evidence_policy, EVIDENCE_POLICY_TYPED)
        self.assertEqual(context.evidence_kinds["price-2330"], "price")
        self.assertEqual(context.evidence_kinds[EVENT_ID], "material_event")
        self.assertEqual(context.evidence_kinds[ACCOUNT_ID], "account")
        self.assertIn(MARKET_REGIME_EVIDENCE_ID, context.market_evidence_ids)
        self.assertIsNone(context.evidence_symbols[MARKET_REGIME_EVIDENCE_ID])
        self.assertEqual(context.market_sentiment["status"], "unavailable")

    def test_legacy_bundle_keeps_old_evidence_table(self):
        legacy = DecisionContext(bundle_with_event())
        self.assertIsNone(legacy.evidence_policy)
        self.assertIsNone(evidence_policy(bundle_with_event()))
        self.assertNotIn(MARKET_REGIME_EVIDENCE_ID, legacy.evidence_symbols)
        self.assertEqual(legacy.market_evidence_ids, set())
        self.assertIsNone(legacy.market_sentiment)
        typed = DecisionContext(typed_bundle())
        self.assertEqual({k: v for k, v in typed.evidence_symbols.items() if k != MARKET_REGIME_EVIDENCE_ID}, legacy.evidence_symbols)

    def test_unknown_policy_value_fails_closed(self):
        broken = typed_bundle()
        broken[EVIDENCE_POLICY_FIELD] = "typed_evidence_v9"
        broken["bundle_sha256"] = decision_bundle_sha256(broken)
        self.assertTrue(any(EVIDENCE_POLICY_FIELD in error for error in DecisionInputValidator(broken).validate()))
        with self.assertRaises(DecisionToolError):
            evidence_policy(broken)

    def test_typed_validate_evidence_rules(self):
        context = DecisionContext(typed_bundle())
        errors = []
        context.validate_evidence(["price-2330", ACCOUNT_ID], "2330.TW", "ok", errors, kind="price")
        self.assertEqual(errors, [])
        cases = [
            ([ACCOUNT_ID], "2330.TW", "price", "無法單獨支持"),
            ([MARKET_REGIME_EVIDENCE_ID], "2330.TW", "price", "無法單獨支持"),
            ([EVENT_ID], "2330.TW", "price", "主要依據"),
            ([EVENT_ID, "price-2330"], "2330.TW", "material_event", "不對映"),
            (["price-2330"], "2330.TW", "sentiment", "種類必須是"),
        ]
        for ids, symbol, kind, message in cases:
            errors = []
            context.validate_evidence(ids, symbol, "x", errors, kind=kind)
            self.assertTrue(any(message in error for error in errors), (ids, kind, errors))

    def test_legacy_validate_evidence_ignores_kind_and_market_rules(self):
        context = DecisionContext(bundle_with_event())
        errors = []
        context.validate_evidence([ACCOUNT_ID], "2330.TW", "x", errors)
        self.assertEqual(errors, [])


class TypedStanceTests(unittest.TestCase):
    def test_typed_packet_passes_and_brief_lists_evidence_kinds(self):
        bundle, momentum, reports, research, envelope = stance_world()
        validator = StancePacketValidator(bundle, momentum, reports, research, "bull")
        self.assertEqual(validator.validate(seal_stance_packet(envelope, bull_items())), [])
        brief = build_stance_brief(bundle, momentum, reports, research, "bull")
        row = next(item for item in brief["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(row["evidence_kinds"], {EVENT_ID: "material_event", "price-2330": "price"})
        self.assertEqual(brief["evidence_policy"]["claim_kinds"], list(CLAIM_KINDS))

    def test_typed_packet_rejects_missing_or_mismatched_kind(self):
        bundle, momentum, reports, research, envelope = stance_world()
        validator = StancePacketValidator(bundle, momentum, reports, research, "bull")
        cases = []
        missing = bull_items()
        missing[0]["claims"][0].pop("claim_kind")
        cases.append((missing, "claim_kind 必須是"))
        wrong = bull_items()
        wrong[1]["claims"][0]["claim_kind"] = "price"
        cases.append((wrong, "主要依據"))
        finding_mismatch = bull_items()
        finding_mismatch[0]["claims"][0]["finding_ids"] = ["event-2330-1"]
        cases.append((finding_mismatch, "不屬於"))
        market_only = bull_items()
        market_only[0]["claims"][0]["evidence_ids"] = [MARKET_REGIME_EVIDENCE_ID]
        cases.append((market_only, "無法單獨支持"))
        for items, message in cases:
            errors = validator.validate(seal_stance_packet(envelope, items))
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_legacy_bundle_rejects_claim_kind_field(self):
        from test_stance import inputs, bull_items as legacy_items
        bundle, momentum, reports, research = inputs()
        envelope = build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"]
        self.assertNotIn("evidence_kinds", build_stance_brief(bundle, momentum, reports, research, "bull")["symbols"][0])
        items = legacy_items()
        items[0]["claims"][0]["claim_kind"] = "price"
        errors = StancePacketValidator(bundle, momentum, reports, research, "bull").validate(seal_stance_packet(envelope, items))
        self.assertTrue(any("未允許欄位" in error for error in errors), errors)


class TypedAnalystTests(unittest.TestCase):
    def test_typed_reports_pass_and_untyped_findings_are_rejected(self):
        bundle = typed_bundle()
        reports = typed_reports(bundle)
        for name in ("technical", "event"):
            self.assertEqual(AnalystReportValidator(bundle, name).validate(reports[name]), [], name)
        self.assertEqual(AnalystReportValidator(bundle, "sentiment").validate(reports["sentiment"]), [])
        untyped = [
            {"symbol": "2317.TW", "outlook": "positive", "data_gaps": [],
             "findings": [{"finding_id": "tech-2317-1", "text": "依摘要判斷。", "evidence_ids": ["price-2317"]}]},
            {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": ["x"]},
        ]
        errors = AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", untyped))
        self.assertTrue(any("finding_kind 必須是" in error for error in errors), errors)
        mismatched = [
            {"symbol": "2317.TW", "outlook": "positive", "data_gaps": [], "findings": [finding("tech-2317-1", "price-2317", "financial")]},
            {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": ["x"]},
        ]
        errors = AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", mismatched))
        self.assertTrue(any("主要依據" in error for error in errors), errors)

    def test_legacy_bundle_rejects_finding_kind_field(self):
        from test_analysts import technical_items
        bundle = bundle_with_event()
        items = technical_items()
        items[0]["findings"][0]["finding_kind"] = "price"
        errors = AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", items))
        self.assertTrue(any("未允許欄位" in error for error in errors), errors)
        self.assertEqual(AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", technical_items())), [])


if __name__ == "__main__":
    unittest.main()
