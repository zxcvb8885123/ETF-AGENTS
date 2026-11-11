import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.calibration import CalibrationError, shadow_swap_report
from etf_agent.core.artifact_store import ImmutableRunStore
from etf_agent.decision.contracts import artifact_content_sha256

CUTOFF = "2026-11-10T23:00:00+00:00"
RUN_ID = "decision-20261110T231233Z"
DECISION_ID = "trade-decision:%s" % RUN_ID


def pair(buy, sell):
    return {"buy_symbol": buy, "sell_symbol": sell, "buy_basis_ids": ["e1"], "sell_basis_ids": ["e2"], "reason": "x"}


def nomination(status="completed", pairs=()):
    body = {
        "schema_version": "1.0", "nomination_id": "swap-nomination:%s" % RUN_ID, "snapshot_id": "snap", "decision_cutoff": CUTOFF,
        "trade_decision_id": DECISION_ID, "status": status, "nominations": list(pairs), "thesis_hint_responses": [], "errors": [],
    }
    body["content_sha256"] = artifact_content_sha256(body)
    return body


def order(symbol, side, intent):
    return {"symbol": symbol, "side": side, "lots": 1, "shares": 1000, "gross_amount": "1000", "intent": intent}


def flag(symbol, code):
    return {"symbol": symbol, "code": code, "detail": "x"}


def make_artifacts(nom, intents, orders, flags, gate=None, limit=2):
    gate = {"version": "v1", "max_swaps_per_run": limit, "min_holding_days": 5} if gate is None else gate
    return {
        "policy": {"position_sizing": {"agent_swap": gate}},
        "team_inputs": {"schema_version": "2.9", "swap_nomination": nom},
        "intent": {
            "decision_id": DECISION_ID, "snapshot_id": "snap", "decision_cutoff": CUTOFF, "status": "completed",
            "items": [{"symbol": s, "intent": i, "conviction": c} for s, i, c in intents],
        },
        "proposal": {
            "trade_intent_result_id": DECISION_ID, "snapshot_id": "snap", "decision_cutoff": CUTOFF, "status": "completed",
            "constraint_flags": flags, "order_proposal": {"orders": orders},
        },
    }


INTENTS = [
    ("1101.TW", "buy", "high"), ("1102.TW", "buy", "medium"), ("1103.TW", "buy", "high"), ("1104.TW", "buy", "medium"),
    ("1105.TW", "buy", "high"), ("2001.TW", "hold", "low"), ("2002.TW", "hold", "low"), ("2003.TW", "hold", "medium"),
    ("2004.TW", "hold", "low"), ("2005.TW", "hold", "low"), ("2006.TW", "hold", None),
]
PAIRS = [
    pair("1101.TW", "2001.TW"),  # 影子基準已有買單 -> unneeded
    pair("1102.TW", "2002.TW"),  # 被名額擋下 -> would_fill
    pair("1103.TW", "2003.TW"),  # 被名額擋下 -> would_fill
    pair("1104.TW", "2004.TW"),  # 被名額擋下 -> 超過上限 2 -> swap_cap
    pair("1105.TW", "2005.TW"),  # 沒有 MAX_POSITIONS、現金不足 -> cancelled:cash_fit
]
ORDERS = [order("1101.TW", "buy", "buy")]
FLAGS = [
    flag("1102.TW", "MAX_POSITIONS"), flag("1103.TW", "MAX_POSITIONS"), flag("1104.TW", "MAX_POSITIONS"),
    flag("1105.TW", "RISK_CASH_FIT"), flag("PORTFOLIO", "PORTFOLIO_NOTE"),
]


class ShadowSwapReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ImmutableRunStore(self.root, schema_version="1.0", error=CalibrationError, dated=True)
        self.artifacts = make_artifacts(nomination(pairs=PAIRS), INTENTS, ORDERS, FLAGS)

    def save(self, artifacts=None, run_id=RUN_ID, day="2026-11-11"):
        return self.store.save(run_id, artifacts or self.artifacts, day=day)

    def rewrite(self, run_dir, name, mutate):
        path = run_dir / ("%s.json" % name)
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_counts_pairs_and_estimates_v2_outcomes(self):
        self.save()
        report = shadow_swap_report(self.root)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["evidence_grade"], "replay_reference_only")
        self.assertIn("前瞻驗證", report["notice"])
        self.assertEqual((report["shadow_run_count"], report["skipped_non_shadow_runs"]), (1, 0))
        day = report["days"][0]
        self.assertEqual((day["nomination_status"], day["nominated_pairs"], day["estimated_fills"]), ("completed", 5, 2))
        self.assertEqual([p["estimated_outcome"] for p in day["pairs"]], [
            "unneeded", "would_fill", "would_fill", "cancelled:swap_cap", "cancelled:cash_fit"])
        self.assertEqual((day["actual_orders"], day["actual_buy_orders"], day["actual_sell_orders"], day["actual_max_positions_flags"]), (1, 1, 0, 3))
        self.assertTrue(day["pairs"][0]["baseline_buy_order"])
        self.assertEqual(day["pairs"][4]["baseline_buy_flags"], ["RISK_CASH_FIT"])
        totals = report["totals"]
        self.assertEqual(totals["nomination_status_days"], {"completed": 1, "not_needed": 0, "exhausted": 0, "unavailable": 0})
        self.assertEqual(totals["nominated_pairs"], 5)
        self.assertEqual((totals["estimated_fills"], totals["estimated_unneeded"]), (2, 1))
        self.assertEqual(totals["estimated_cancelled"], {"cash_fit": 1, "swap_cap": 1})
        self.assertEqual(totals["estimated_skipped"], {})
        self.assertEqual(totals["buy_conviction"], {"high": 3, "medium": 2})
        self.assertEqual(totals["sell_conviction"], {"low": 4, "medium": 1})
        self.assertEqual(totals["actual_orders"], 1)

    def test_missing_optional_fields_are_reported_as_unrecorded(self):
        self.save()
        report = shadow_swap_report(self.root)
        self.assertEqual(report["report_version"], "shadow_swap_report.v2")
        day = report["days"][0]
        self.assertEqual(day["purposes"], {"未記錄": 5})
        self.assertEqual(day["lower_conviction_review"], "未記錄")
        self.assertEqual(day["high_conviction_victim_pairs"], 0)
        self.assertEqual(day["thesis_hints"], {"nominated": 0, "kept": 0})
        self.assertEqual(day["min_new_conviction"], "未記錄")
        self.assertEqual(day["buy_intents_below_min_new_conviction"], "未記錄")
        totals = report["totals"]
        self.assertEqual(totals["purposes"], {"未記錄": 5})
        self.assertEqual(totals["lower_conviction_review"]["unrecorded_days"], 1)
        self.assertEqual(totals["lower_conviction_review"]["recorded_days"], 0)
        self.assertEqual(totals["buy_intents_below_min_new_conviction"], "未記錄")
        self.assertEqual(totals["renominated_as_buy_within_10_days"], 0)

    def exposure_artifacts(self, run_id, pairs, intents, review, hints, rotation=None):
        nom = nomination(pairs=pairs)
        nom["trade_decision_id"] = "trade-decision:%s" % run_id
        nom["thesis_hint_responses"] = hints
        nom["lower_conviction_review"] = review
        nom["content_sha256"] = artifact_content_sha256({k: v for k, v in nom.items() if k != "content_sha256"})
        artifacts = make_artifacts(nom, intents, ORDERS, FLAGS)
        artifacts["intent"]["decision_id"] = artifacts["proposal"]["trade_intent_result_id"] = nom["trade_decision_id"]
        if rotation is not None:
            artifacts["policy"]["position_sizing"]["rotation"] = rotation
        return artifacts

    def test_purpose_review_hints_and_renomination_are_counted(self):
        first_pairs = [dict(pair("1102.TW", "2001.TW"), purpose="thesis"), dict(pair("1103.TW", "2003.TW"), purpose="exposure")]
        intents = [s for s in INTENTS] + [("1106.TW", "buy", "low")]
        intents[5] = ("2001.TW", "hold", "high")
        first = self.exposure_artifacts(
            RUN_ID, first_pairs, intents,
            [{"symbol": "2002.TW", "response": "kept", "reason": "題材未變"}, {"symbol": "2003.TW", "response": "nominated", "reason": "x"}],
            [{"symbol": "2001.TW", "response": "nominated", "reason": "r"}, {"symbol": "2004.TW", "response": "kept", "reason": "r"}],
            rotation={"min_new_conviction": "medium"},
        )
        self.store.save(RUN_ID, first, day="2026-11-11")
        second_id = "decision-20261112T231233Z"
        # 第二天提名「2001.TW」為換入：它前一天被提名為換出者，且在第二天的意圖裡要是第一輪 buy。
        intents_two = [("2001.TW", "buy", "high"), ("2002.TW", "hold", "low")]
        second = self.exposure_artifacts(
            second_id, [dict(pair("2001.TW", "2002.TW"), purpose="exposure")], intents_two, [], [])
        self.store.save(second_id, second, day="2026-11-12")
        report = shadow_swap_report(self.root)
        first_day, second_day = report["days"]
        self.assertEqual(first_day["purposes"], {"exposure": 1, "thesis": 1})
        self.assertEqual(first_day["high_conviction_victim_pairs"], 1)
        self.assertEqual(first_day["lower_conviction_review"], {
            "nominated": 1, "kept": 1, "kept_reasons": [{"symbol": "2002.TW", "reason": "題材未變"}]})
        self.assertEqual(first_day["thesis_hints"], {"nominated": 1, "kept": 1})
        self.assertEqual(first_day["min_new_conviction"], "medium")
        self.assertEqual(first_day["buy_intents_below_min_new_conviction"], 1)
        self.assertEqual(first_day["renominated_as_buy_within_10_days"], [])
        self.assertEqual(second_day["renominated_as_buy_within_10_days"], ["2001.TW"])
        self.assertEqual(second_day["lower_conviction_review"], {"nominated": 0, "kept": 0, "kept_reasons": []})
        totals = report["totals"]
        self.assertEqual(totals["purposes"], {"exposure": 2, "thesis": 1})
        self.assertEqual(totals["thesis_hints"], {"nominated": 1, "kept": 1})
        self.assertEqual(totals["high_conviction_victim_pairs"], 1)
        self.assertEqual(totals["lower_conviction_review"]["recorded_days"], 2)
        self.assertEqual(totals["lower_conviction_review"]["kept_reasons"], [
            {"date": "2026-11-11", "symbol": "2002.TW", "reason": "題材未變"}])
        self.assertEqual(totals["renominated_as_buy_within_10_days"], 1)
        # 第二天沒有 rotation 設定：合計回報「未記錄」，不補值。
        self.assertEqual(totals["buy_intents_below_min_new_conviction"], "未記錄")

    def test_invalid_purpose_hint_or_review_response_is_rejected(self):
        def bad_purpose(a):
            a["team_inputs"]["swap_nomination"]["nominations"][0]["purpose"] = "quality_dip"
            a["team_inputs"]["swap_nomination"]["content_sha256"] = artifact_content_sha256(
                {k: v for k, v in a["team_inputs"]["swap_nomination"].items() if k != "content_sha256"})
        self.assert_invalid(bad_purpose)

    def test_invalid_hint_response_is_rejected(self):
        def mutate(a):
            a["team_inputs"]["swap_nomination"]["thesis_hint_responses"] = [{"symbol": "2001.TW", "response": "maybe", "reason": "x"}]
            a["team_inputs"]["swap_nomination"]["content_sha256"] = artifact_content_sha256(
                {k: v for k, v in a["team_inputs"]["swap_nomination"].items() if k != "content_sha256"})
        self.assert_invalid(mutate)

    def test_program_status_with_review_is_rejected(self):
        def mutate(a):
            nom = nomination("exhausted")
            nom["lower_conviction_review"] = [{"symbol": "2001.TW", "response": "kept", "reason": "x"}]
            nom["content_sha256"] = artifact_content_sha256({k: v for k, v in nom.items() if k != "content_sha256"})
            a["team_inputs"]["swap_nomination"] = nom
        self.assert_invalid(mutate)

    def test_status_branches_are_counted_across_days(self):
        self.save()
        for index, status in enumerate(("not_needed", "exhausted", "unavailable")):
            run_id = "decision-2026111%dT231233Z" % (index + 1)
            artifacts = make_artifacts(nomination(status), INTENTS, ORDERS, FLAGS)
            artifacts["intent"]["decision_id"] = artifacts["proposal"]["trade_intent_result_id"] = "trade-decision:%s" % run_id
            artifacts["team_inputs"]["swap_nomination"] = nomination(status)
            artifacts["team_inputs"]["swap_nomination"]["trade_decision_id"] = "trade-decision:%s" % run_id
            artifacts["team_inputs"]["swap_nomination"]["content_sha256"] = artifact_content_sha256(
                {k: v for k, v in artifacts["team_inputs"]["swap_nomination"].items() if k != "content_sha256"})
            self.store.save(run_id, artifacts, day="2026-11-1%d" % (index + 2))
        report = shadow_swap_report(self.root)
        self.assertEqual(report["shadow_run_count"], 4)
        self.assertEqual(report["totals"]["nomination_status_days"], {"completed": 1, "not_needed": 1, "exhausted": 1, "unavailable": 1})
        self.assertEqual(report["totals"]["nominated_pairs"], 5)
        self.assertEqual(shadow_swap_report(self.root, since="2026-11-13")["shadow_run_count"], 2)

    def test_derisk_day_skips_every_pair(self):
        artifacts = make_artifacts(nomination(pairs=PAIRS), INTENTS, ORDERS, FLAGS + [flag("2006.TW", "RISK_DRAWDOWN")])
        self.save(artifacts)
        totals = shadow_swap_report(self.root)["totals"]
        self.assertEqual(totals["estimated_skipped"], {"derisk_day": 5})
        self.assertEqual(totals["estimated_fills"], 0)

    def test_exited_victim_is_cancelled(self):
        artifacts = make_artifacts(nomination(pairs=[pair("1102.TW", "2002.TW")]), INTENTS, ORDERS + [order("2002.TW", "sell", "exit")], FLAGS)
        self.save(artifacts)
        self.assertEqual(shadow_swap_report(self.root)["totals"]["estimated_cancelled"], {"victim_unavailable": 1})

    def test_no_shadow_data_is_explicit_not_fake_numbers(self):
        report = shadow_swap_report(self.root)
        self.assertEqual(report["status"], "no_shadow_data")
        self.assertEqual(report["shadow_run_count"], 0)
        self.assertNotIn("totals", report)
        self.assertEqual(report["evidence_grade"], "replay_reference_only")
        self.assertEqual(shadow_swap_report(self.root / "missing")["status"], "no_shadow_data")

    def test_non_shadow_runs_are_skipped_and_counted(self):
        legacy = make_artifacts(nomination(), INTENTS, ORDERS, FLAGS)
        legacy["policy"] = {"position_sizing": {}}
        del legacy["team_inputs"]["swap_nomination"]
        self.store.save("decision-20261109T231233Z", legacy, day="2026-11-10")
        v2 = make_artifacts(nomination(), INTENTS, ORDERS, FLAGS, gate={"version": "v2", "max_swaps_per_run": 2})
        self.store.save("decision-20261109T231234Z", v2, day="2026-11-10")
        report = shadow_swap_report(self.root)
        self.assertEqual((report["status"], report["skipped_non_shadow_runs"]), ("no_shadow_data", 2))
        self.save()
        report = shadow_swap_report(self.root)
        self.assertEqual((report["shadow_run_count"], report["skipped_non_shadow_runs"]), (1, 2))

    def test_missing_artifact_is_rejected(self):
        run_dir = self.save()
        (run_dir / "proposal.json").unlink()
        with self.assertRaises(CalibrationError):
            shadow_swap_report(self.root)

    def test_tampered_artifact_is_rejected(self):
        run_dir = self.save()
        self.rewrite(run_dir, "team_inputs", lambda t: t["swap_nomination"]["nominations"].pop())
        with self.assertRaises(CalibrationError):
            shadow_swap_report(self.root)

    def test_manifest_without_required_artifact_is_rejected(self):
        artifacts = {name: value for name, value in self.artifacts.items() if name != "intent"}
        self.save(artifacts)
        with self.assertRaises(CalibrationError):
            shadow_swap_report(self.root)

    def test_one_bad_run_rejects_the_whole_batch(self):
        self.save()
        bad = copy.deepcopy(self.artifacts)
        bad["proposal"]["status"] = "rejected"
        self.store.save("decision-20261111T231233Z", bad, day="2026-11-12")
        with self.assertRaises(CalibrationError):
            shadow_swap_report(self.root)

    def assert_invalid(self, mutate):
        artifacts = copy.deepcopy(self.artifacts)
        mutate(artifacts)
        self.save(artifacts)
        with self.assertRaises(CalibrationError):
            shadow_swap_report(self.root)

    def test_cutoff_and_snapshot_mismatch_is_rejected(self):
        self.assert_invalid(lambda a: a["proposal"].update(decision_cutoff="2026-11-10T22:00:00+00:00"))

    def test_snapshot_mismatch_with_nomination_is_rejected(self):
        def mutate(a):
            a["team_inputs"]["swap_nomination"]["snapshot_id"] = "other"
            a["team_inputs"]["swap_nomination"]["content_sha256"] = artifact_content_sha256(
                {k: v for k, v in a["team_inputs"]["swap_nomination"].items() if k != "content_sha256"})
        self.assert_invalid(mutate)

    def test_nomination_content_hash_mismatch_is_rejected(self):
        self.assert_invalid(lambda a: a["team_inputs"]["swap_nomination"].update(errors=["tampered"]))

    def test_nomination_from_another_decision_is_rejected(self):
        self.assert_invalid(lambda a: a["proposal"].update(trade_intent_result_id="trade-decision:other"))

    def test_v1_without_nomination_is_rejected(self):
        self.assert_invalid(lambda a: a["team_inputs"].pop("swap_nomination"))

    def test_nomination_without_gate_is_rejected(self):
        self.assert_invalid(lambda a: a["policy"].update(position_sizing={}))

    def test_agent_swap_flags_in_shadow_run_are_rejected(self):
        self.assert_invalid(lambda a: a["proposal"]["constraint_flags"].append(flag("1102.TW", "AGENT_SWAP_FILL")))

    def test_invalid_pairs_are_rejected(self):
        def wrong_intent(a):
            a["intent"]["items"][0]["intent"] = "hold"
        self.assert_invalid(wrong_intent)

    def test_program_status_with_pairs_is_rejected(self):
        def mutate(a):
            a["team_inputs"]["swap_nomination"] = nomination("exhausted", PAIRS)
        self.assert_invalid(mutate)

    def test_bad_since_is_rejected(self):
        with self.assertRaises(CalibrationError):
            shadow_swap_report(self.root, since="11-04")


if __name__ == "__main__":
    unittest.main()
