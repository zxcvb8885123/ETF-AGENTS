import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.calibration import CalibrationError, intent_landing_report
from etf_agent.core.artifact_store import ImmutableRunStore

ROOT = Path(__file__).resolve().parents[2]
REAL_DECISIONS = ROOT / "artifacts" / "decisions"


def order(symbol, side, amount, intent="buy"):
    return {"symbol": symbol, "side": side, "lots": 1, "shares": 1000, "gross_amount": amount, "intent": intent}


def make_artifacts(run_id, intents, orders, flags):
    decision_id = "trade-decision:%s" % run_id
    intent = {
        "decision_id": decision_id, "snapshot_id": "snap", "decision_cutoff": "2026-11-03T23:31:03+00:00", "status": "completed",
        "items": [{"symbol": s, "intent": i, "conviction": c} for s, i, c in intents],
    }
    proposal = {
        "trade_intent_result_id": decision_id, "snapshot_id": "snap", "decision_cutoff": "2026-11-03T23:31:03+00:00", "status": "completed",
        "constraint_flags": flags, "order_proposal": {"orders": orders},
    }
    return {"intent": intent, "proposal": proposal}


class IntentLandingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ImmutableRunStore(self.root, schema_version="1.0", error=CalibrationError, dated=True)
        self.artifacts = make_artifacts(
            "decision-20261103T231233Z",
            [("1101.TW", "buy", "high"), ("1102.TW", "buy", "low"), ("1103.TW", "add", "medium"), ("1104.TW", "hold", None),
             ("1105.TW", "hold", None), ("1106.TW", "no_trade", None)],
            [order("1101.TW", "buy", "1000"), order("1104.TW", "sell", "300", "exit"), order("1105.TW", "sell", "100", "trim")],
            [{"symbol": "1102.TW", "code": "MAX_POSITIONS", "detail": "x"}, {"symbol": "1104.TW", "code": "RISK_DRAWDOWN", "detail": "x"},
             {"symbol": "1105.TW", "code": "RISK_DRAWDOWN", "detail": "x"}, {"symbol": None, "code": "PORTFOLIO_NOTE", "detail": "x"}],
        )

    def save(self, artifacts=None, run_id="decision-20261103T231233Z", day="2026-11-04"):
        return self.store.save(run_id, artifacts or self.artifacts, day=day)

    def rewrite(self, run_dir, name, mutate):
        path = run_dir / ("%s.json" % name)
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_counts_landing_reasons_and_overrides(self):
        self.save()
        report = intent_landing_report(self.root)
        self.assertEqual(report["evidence_grade"], "replay_reference_only")
        self.assertEqual(report["run_count"], 1)
        day = report["days"][0]
        self.assertEqual((day["buy_add_intents"], day["landed"], day["unlanded"]), (3, 1, 2))
        self.assertEqual(day["unlanded_reasons"], {"MAX_POSITIONS": 1, "NO_FLAG": 1})
        self.assertEqual(day["flag_counts"]["RISK_DRAWDOWN"], 2)
        self.assertEqual(day["flag_counts"]["PORTFOLIO_NOTE"], 1)
        self.assertEqual(day["sell_gross_amount"], "400")
        self.assertEqual(day["overridden_sell_gross_amount"], "400")
        self.assertEqual(day["overridden_sell_share_percent"], "100.00")
        self.assertEqual([item["symbol"] for item in day["overridden_sells"]], ["1104.TW", "1105.TW"])
        self.assertEqual(day["overridden_sells"][0]["flags"], ["RISK_DRAWDOWN"])
        self.assertEqual(report["totals"]["landed"], 1)

    def test_conviction_layers_and_unrecorded_policy(self):
        self.save()
        report = intent_landing_report(self.root)
        self.assertEqual(report["report_version"], "intent_landing.v2")
        self.assertEqual(report["totals"]["by_conviction"], {
            "high": {"landed": 1, "unlanded": 0}, "low": {"landed": 0, "unlanded": 1}, "medium": {"landed": 0, "unlanded": 1}})
        self.assertEqual([segment["policy_id"] for segment in report["by_policy"]], ["未記錄"])

    def add_run(self, run_id, day, policy_id, sold_intent="hold"):
        artifacts = make_artifacts(
            run_id,
            [("1101.TW", "buy", "high"), ("1102.TW", "buy", "medium"), ("1104.TW", sold_intent, None)],
            [order("1101.TW", "buy", "1000"), order("1104.TW", "sell", "300", "exit")],
            [{"symbol": "1102.TW", "code": "MAX_POSITIONS", "detail": "x"}],
        )
        artifacts["policy"] = {"policy_id": policy_id}
        return self.store.save(run_id, artifacts, day=day)

    def test_segments_by_policy_sum_to_totals(self):
        self.add_run("decision-20261103T231233Z", "2026-11-04", "strat-a+trade-decision:decision-20261103T231233Z")
        self.add_run("decision-20261104T231233Z", "2026-11-05", "strat-a+trade-decision:decision-20261104T231233Z", "add")
        self.add_run("decision-20261105T231233Z", "2026-11-06", "strat-b+trade-decision:decision-20261105T231233Z")
        report = intent_landing_report(self.root)
        segments = {segment["policy_id"]: segment for segment in report["by_policy"]}
        self.assertEqual(list(segments), ["strat-a", "strat-b"])
        self.assertEqual((segments["strat-a"]["run_count"], segments["strat-a"]["buy_add_intents"], segments["strat-a"]["landed"]), (2, 5, 2))
        self.assertEqual(segments["strat-a"]["overridden_sell_gross_amount"], "600")
        self.assertEqual(segments["strat-a"]["by_conviction"]["high"], {"landed": 2, "unlanded": 0})
        self.assertEqual((segments["strat-a"]["first_date"], segments["strat-a"]["last_date"]), ("2026-11-04", "2026-11-05"))
        totals = report["totals"]
        for key in ("buy_add_intents", "landed", "unlanded"):
            self.assertEqual(sum(segment[key] for segment in segments.values()), totals[key])
        self.assertEqual(sum(int(segment["overridden_sell_gross_amount"]) for segment in segments.values()), int(totals["overridden_sell_gross_amount"]))
        self.assertEqual(sum(int(segment["sell_gross_amount"]) for segment in segments.values()), int(totals["sell_gross_amount"]))
        self.assertNotIn("target", json.dumps(report, ensure_ascii=False).lower())

    def test_invalid_policy_id_is_rejected(self):
        run_dir = self.add_run("decision-20261103T231233Z", "2026-11-04", "strat-a")
        self.rewrite(run_dir, "policy", lambda p: p.update(policy_id=""))
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def test_agent_exit_is_not_override(self):
        self.artifacts["intent"]["items"][3]["intent"] = "exit"
        self.save()
        day = intent_landing_report(self.root)["days"][0]
        self.assertEqual(day["overridden_sell_gross_amount"], "100")
        self.assertEqual(day["overridden_sell_share_percent"], "25.00")

    def test_since_filters_and_empty_is_rejected(self):
        self.save()
        self.assertEqual(intent_landing_report(self.root, since="2026-11-04")["run_count"], 1)
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root, since="2026-11-05")
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root, since="11-04")

    def test_empty_root_is_rejected(self):
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def test_missing_artifact_is_rejected(self):
        run_dir = self.save()
        (run_dir / "proposal.json").unlink()
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def test_tampered_artifact_is_rejected(self):
        run_dir = self.save()
        self.rewrite(run_dir, "proposal", lambda p: p["order_proposal"]["orders"].pop(0))
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def test_one_bad_run_rejects_whole_batch(self):
        self.save()
        other = copy.deepcopy(make_artifacts("decision-20261104T230000Z", [("1101.TW", "buy", "high")], [], []))
        other["proposal"]["status"] = "rejected"
        self.store.save("decision-20261104T230000Z", other, day="2026-11-05")
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def test_manifest_without_required_artifact_is_rejected(self):
        self.save(dict(intent=self.artifacts["intent"]))
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def assert_invalid(self, mutate, name="proposal"):
        artifacts = copy.deepcopy(self.artifacts)
        mutate(artifacts[name])
        self.save(artifacts)
        with self.assertRaises(CalibrationError):
            intent_landing_report(self.root)

    def test_field_mismatches_are_rejected(self):
        self.assert_invalid(lambda p: p.update(snapshot_id="other"))

    def test_proposal_from_other_intent_is_rejected(self):
        self.assert_invalid(lambda p: p.update(trade_intent_result_id="trade-decision:other"))

    def test_invalid_order_fields_are_rejected(self):
        self.assert_invalid(lambda p: p["order_proposal"]["orders"][0].update(side="short"))

    def test_order_for_unknown_symbol_is_rejected(self):
        self.assert_invalid(lambda p: p["order_proposal"]["orders"].append(order("9999.TW", "buy", "1")))

    def test_non_numeric_amount_is_rejected(self):
        self.assert_invalid(lambda p: p["order_proposal"]["orders"][0].update(gross_amount="NaN"))

    def test_duplicate_intent_symbol_is_rejected(self):
        self.assert_invalid(lambda i: i["items"].append(dict(i["items"][0])), name="intent")

    def test_incomplete_intent_is_rejected(self):
        self.assert_invalid(lambda i: i.update(status="failed"), name="intent")

    def test_malformed_flag_is_rejected(self):
        self.assert_invalid(lambda p: p["constraint_flags"].append({"symbol": "1101.TW"}))

    def test_does_not_write_into_decisions_root(self):
        run_dir = self.save()
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        intent_landing_report(self.root)
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)
        self.assertTrue((run_dir / "manifest.json").exists())


@unittest.skipUnless((REAL_DECISIONS / "2026-11-10").is_dir(), "沒有封存的 artifacts/decisions，略過重現文件數字")
class ArchivedRunsReproductionTest(unittest.TestCase):
    """重現 docs/agent_autonomy_plan.md 第 1.2 節的數字（封存為 2026-10-27～11-10 共 11 份）。"""

    @classmethod
    def setUpClass(cls):
        cls.report = intent_landing_report(REAL_DECISIONS)

    def test_overall_landing(self):
        totals = self.report["totals"]
        self.assertEqual((totals["buy_add_intents"], totals["landed"]), (282, 40))
        self.assertEqual(totals["unlanded_reasons"]["MAX_POSITIONS"], 185)
        self.assertEqual(totals["unlanded_reasons"]["NO_FLAG"], 26)

    def test_policy_segments_sum_to_archived_totals(self):
        totals = self.report["totals"]
        segments = self.report["by_policy"]
        self.assertEqual(sum(segment["buy_add_intents"] for segment in segments), 282)
        self.assertEqual(sum(segment["landed"] for segment in segments), 40)
        self.assertEqual(sum(segment["unlanded"] for segment in segments), 242)
        self.assertEqual(sum(int(segment["overridden_sell_gross_amount"]) for segment in segments), 48913350)
        self.assertEqual(totals["overridden_sell_gross_amount"], "48913350")
        self.assertEqual(sum(segment["run_count"] for segment in segments), self.report["run_count"])

    def test_since_1104(self):
        totals = intent_landing_report(REAL_DECISIONS, since="2026-11-04")["totals"]
        self.assertEqual((totals["buy_add_intents"], totals["landed"]), (115, 6))

    def test_1104_hold_sold(self):
        day = next(item for item in self.report["days"] if item["date"] == "2026-11-04")
        self.assertEqual((day["buy_add_intents"], day["landed"]), (21, 0))
        self.assertEqual(sorted(item["symbol"] for item in day["overridden_sells"]),
                         ["1815.TWO", "2379.TW", "3443.TW", "3533.TW", "3653.TW", "6239.TW"])
        self.assertEqual(day["overridden_sell_gross_amount"], "48450690")
        self.assertEqual(day["sell_gross_amount"], "63046070")
        self.assertEqual(day["overridden_sell_share_percent"], "76.85")
