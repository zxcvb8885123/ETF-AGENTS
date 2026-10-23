import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import (
    AnalystReportValidator,
    MomentumEngine,
    build_analyst_brief,
    canonical_sha256,
    decision_bundle_sha256,
    high_materiality_events,
    seal_report,
    sentiment_unavailable_report,
)
from etf_agent.decision.analysts import compact_event_batch, report_envelope

from test_daily_pipeline import FakeRunner
from test_portfolio_decision_agent import decision_bundle


ROOT = Path(__file__).resolve().parents[2]
EVENT_ID = "document:event-2330"


def bundle_with_event():
    bundle = decision_bundle()
    snapshot = bundle["snapshot"]
    snapshot["source_evidence"].append(
        {"evidence_id": EVENT_ID, "source": "TWSE_MOPS", "authority": "mops", "data_type": "material_event"}
    )
    snapshot["documents"].append(
        {
            "document_id": "event-2330", "source": "TWSE_MOPS", "external_id": "event-1", "version": "1",
            "document_type": "material_event", "symbol": "2330.TW", "title": "董事會通過資本支出",
            "body": "本公司董事會決議核准資本預算。", "source_url": "https://mops.twse.com.tw",
            "published_at": "2026-09-19T08:00:00+00:00", "available_at": "2026-09-19T09:00:00+00:00",
            "content_sha256": "c" * 64, "source_evidence_id": EVENT_ID,
        }
    )
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def finding(finding_id, evidence):
    return {"finding_id": finding_id, "text": "依摘要判斷。", "evidence_ids": [evidence]}


def technical_items():
    return [
        {"symbol": "2317.TW", "outlook": "positive", "findings": [finding("tech-2317-1", "price-2317")], "data_gaps": []},
        {"symbol": "2330.TW", "outlook": "neutral", "findings": [finding("tech-2330-1", "price-2330")], "data_gaps": []},
    ]


def event_items(materiality="high"):
    return [
        {"symbol": "2317.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_MATERIAL_EVENT_IN_WINDOW"], "events": []},
        {
            "symbol": "2330.TW", "outlook": "positive", "findings": [finding("event-2330-1", EVENT_ID)], "data_gaps": [],
            "events": [{"evidence_id": EVENT_ID, "materiality": materiality, "summary": "董事會核准資本預算。"}],
        },
    ]


def report(bundle, analyst, items):
    return seal_report(report_envelope(bundle, analyst, "report-%s" % analyst), items)


class AnalystReportValidatorTests(unittest.TestCase):
    def test_valid_reports_cover_universe_with_owned_evidence(self):
        bundle = bundle_with_event()
        self.assertEqual(AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", technical_items())), [])
        self.assertEqual(AnalystReportValidator(bundle, "event").validate(report(bundle, "event", event_items())), [])
        sentiment = sentiment_unavailable_report(bundle)
        self.assertEqual(sentiment["status"], "unavailable")
        self.assertEqual(AnalystReportValidator(bundle, "sentiment").validate(sentiment), [])

    def test_rejects_gaps_missing_findings_foreign_evidence_numbers_and_duplicates(self):
        bundle = bundle_with_event()
        cases = []
        missing = technical_items()[:1]
        cases.append((missing, "未覆蓋交易池"))
        unknown = technical_items()
        unknown[0] = dict(unknown[0], outlook="unknown", findings=[], data_gaps=[])
        cases.append((unknown, "data_gaps"))
        empty = technical_items()
        empty[0] = dict(empty[0], findings=[])
        cases.append((empty, "至少有一項 finding"))
        foreign = technical_items()
        foreign[0]["findings"] = [finding("tech-2317-1", "price-2330")]
        cases.append((foreign, "不屬於"))
        numbers = technical_items()
        numbers[0]["weight"] = "0.1"
        cases.append((numbers, "不得包含配置"))
        duplicate = technical_items()
        duplicate[1]["findings"] = [finding("tech-2317-1", "price-2330")]
        cases.append((duplicate, "重複"))
        for items, message in cases:
            errors = AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", items))
            self.assertTrue(any(message in error for error in errors), (message, errors))

    def test_event_analyst_must_classify_every_material_event_exactly_once(self):
        bundle = bundle_with_event()
        unclassified = event_items()
        unclassified[1]["events"] = []
        errors = AnalystReportValidator(bundle, "event").validate(report(bundle, "event", unclassified))
        self.assertTrue(any("未分級全部重大訊息" in error for error in errors), errors)
        misplaced = event_items()
        misplaced[0]["events"] = [dict(misplaced[1]["events"][0])]
        errors = AnalystReportValidator(bundle, "event").validate(report(bundle, "event", misplaced))
        self.assertTrue(any("不是 2317.TW 的重大訊息" in error for error in errors), errors)
        technical_with_events = technical_items()
        technical_with_events[0]["events"] = []
        errors = AnalystReportValidator(bundle, "technical").validate(report(bundle, "technical", technical_with_events))
        self.assertTrue(any("未允許欄位" in error for error in errors), errors)

    def test_event_counter_evidence_and_pending_confirmations(self):
        # 策略文件：事件要看反證與尚待確認事項；舊封存沒有這兩欄仍有效，有就必須是字串陣列。
        bundle = bundle_with_event()
        validator = AnalystReportValidator(bundle, "event")
        items = event_items()
        items[1]["events"][0].update(counter_evidence=["同期財報顯示資本支出已占營收三成。"],
                                     pending_confirmations=["資本預算的執行時程尚未公布。"])
        self.assertEqual(validator.validate(report(bundle, "event", items)), [])
        for bad in ("只有一句", [1], [""]):
            items = event_items()
            items[1]["events"][0]["counter_evidence"] = bad
            self.assertTrue(any("counter_evidence" in error for error in validator.validate(report(bundle, "event", items))), bad)
        items = event_items()
        items[1]["events"][0]["impact_score"] = ["high"]
        self.assertTrue(any("impact_score" in error for error in validator.validate(report(bundle, "event", items))))

    def test_high_materiality_events_are_listed_for_full_event_research(self):
        bundle = bundle_with_event()
        self.assertEqual(high_materiality_events(report(bundle, "event", event_items("high"))), [{"symbol": "2330.TW", "evidence_id": EVENT_ID}])
        self.assertEqual(high_materiality_events(report(bundle, "event", event_items("low"))), [])


class AnalystBriefTests(unittest.TestCase):
    def test_event_excerpt_keeps_every_id_and_full_source(self):
        bundle = bundle_with_event()
        original = "董事會決議" + "投資計畫與產能評估。" * 150
        bundle["snapshot"]["documents"][0]["body"] = original
        bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        brief = build_analyst_brief(bundle, MomentumEngine(bundle).run(), "event", event_market_scope=True)
        compact, source = compact_event_batch(brief, bundle)
        event = next(item for item in compact["symbols"] if item["symbol"] == "2330.TW")["events"][0]
        self.assertEqual(event["evidence_id"], EVENT_ID)
        self.assertTrue(event["brief_excerpt_truncated"])
        self.assertEqual(source["events"][0]["body"], original)
        self.assertEqual(source["content_sha256"], canonical_sha256({key: value for key, value in source.items() if key != "content_sha256"}))

    def test_briefs_only_rearrange_existing_inputs(self):
        bundle = bundle_with_event()
        momentum = MomentumEngine(bundle).run()
        technical = build_analyst_brief(bundle, momentum, "technical")
        tsmc = next(item for item in technical["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(tsmc["momentum"]["evidence_ids"], ["price-2330"])
        # 策略文件寫「均線」：給均線數值本身，不只給是否站上均線。
        source = next(item for item in momentum["items"] if item["symbol"] == "2330.TW")
        for field in ("ma20", "ma60"):
            self.assertIn(field, tsmc["momentum"])
            if source[field] is not None:
                self.assertEqual(Decimal(tsmc["momentum"][field]), Decimal(source[field]).quantize(Decimal("0.01")))
        self.assertEqual(technical["regime_assessment"], momentum["regime_assessment"])
        event = build_analyst_brief(bundle, momentum, "event")
        self.assertEqual(next(item for item in event["symbols"] if item["symbol"] == "2330.TW")["events"][0]["evidence_id"], EVENT_ID)
        fundamental = build_analyst_brief(bundle, momentum, "fundamental", None)
        self.assertEqual({item["financial_status"] for item in fundamental["symbols"]}, {"unavailable"})
        # 沒有已核對的現金流文件時明確標為資料缺口，不留空白讓分析師自行推測。
        self.assertEqual({item["cash_flow"]["status"] for item in fundamental["symbols"]}, {"unavailable"})


class AnalystTeamTests(unittest.TestCase):
    def test_team_runs_batches_retries_failed_batch_and_merges(self):
        bundle = bundle_with_event()
        momentum = MomentumEngine(bundle).run()
        fundamental = [
            {"symbol": "2317.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]},
            {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]},
        ]
        broken = copy.deepcopy(technical_items())[:1]
        runner = FakeRunner(
            {
                "technical_b0": [{"items": broken}, {"items": technical_items()}],
                "fundamental_b0": [{"items": fundamental}],
                "event_b0": [{"items": [dict(row, event_outlook=row["outlook"]) for row in event_items() if row["symbol"] == "2330.TW"]}],
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            reports = pipeline.run_analyst_team(bundle, momentum)
            saved = json.loads((Path(directory) / "analyst_event.json").read_text(encoding="utf-8"))

        self.assertEqual(set(reports), {"technical", "fundamental", "event"})
        self.assertEqual([name for name, _ in runner.prompts], ["technical_b0", "technical_b0", "fundamental_b0", "event_b0"])
        self.assertIn("未覆蓋", runner.prompts[1][1])
        self.assertEqual(saved["items"][1]["events"][0]["materiality"], "high")
        self.assertEqual((saved["items"][0]["symbol"], saved["items"][0]["outlook"], saved["items"][0]["events"]), ("2317.TW", "unknown", []))


if __name__ == "__main__":
    unittest.main()
