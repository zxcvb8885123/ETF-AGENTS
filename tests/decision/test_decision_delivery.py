import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.decision import DecisionRepository, DecisionFinalizer, RevisionHistoryBuilder, decision_policy_sha256, decision_rules_sha256, decision_bundle_sha256
from etf_agent.decision_delivery import build_delivery, validate_delivery, save_failure, DeliveryError
from etf_agent.decision_delivery.builder import build_report
from etf_agent.decision_delivery.renderer import render_report, render_report_v2, renderer_for
from etf_agent.decision_delivery.renderer_v1 import render_report_v1
from etf_agent.decision_delivery.validator import validate_report
from test_team_chain import team_world, finalize
from test_portfolio_risk_decision import risk_review, policy, valid_inputs


def world(rejected=False):
    b, m, d, t, p, team, *_ = team_world()
    a, result = finalize(b, m, d, t, p, team)
    if rejected:
        a["risk_review"] = risk_review(b, a["proposal"], a["scenario"], a["guard"], "reject")
        a["revision_history"] = RevisionHistoryBuilder(b, p, t).create(a["proposal"], a["scenario"], a["guard"], a["risk_review"])
        result = DecisionFinalizer(b, p, m, d, t, team).run(*(a[k] for k in ("proposal", "scenario", "guard", "risk_review", "revision_history")))
    return {**a, "decision_input": b, "momentum": m, "debate": d, "intent": t, "policy": p, "team_inputs": team, "decision": result}


class DeliveryTests(unittest.TestCase):
    def test_legacy_report_index_can_be_replaced_by_later_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "reports"
            reports.mkdir()
            (reports / "latest.json").write_text(json.dumps({"schema_version": "1.0", "workflow_run_id": "old", "generated_at": "2026-09-29T16:33:20+08:00"}), encoding="utf-8")
            result = save_failure(root / "delivery", "舊帳本待結算", "today", reports_root=reports, started_at="2026-10-19T20:24:30+08:00")
            self.assertTrue(result["published"])
            self.assertIn("started_at", json.loads((reports / "latest.json").read_text(encoding="utf-8")))

    def test_interrupted_latest_copy_is_repairable(self):
        from etf_agent.decision_delivery import repository
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DecisionRepository(root / "decisions").save("example", world())
            original = repository.atomic_text

            def fail_copy(path, text):
                if Path(path).name == "latest.md":
                    raise OSError("模擬發布中斷")
                return original(path, text)

            with patch.object(repository, "atomic_text", side_effect=fail_copy):
                with self.assertRaisesRegex(OSError, "發布中斷"):
                    build_delivery(root / "decisions", "example", root / "delivery", reports_root=root / "reports", started_at="2026-10-11T08:00:00+08:00")
            self.assertTrue((root / "reports/latest.json").exists())
            self.assertFalse((root / "reports/latest.md").exists())
            result = build_delivery(root / "decisions", "example", root / "delivery", reports_root=root / "reports", started_at="2026-10-11T08:00:00+08:00")
            self.assertIn(result["delivery_id"], (root / "reports/latest.md").read_text(encoding="utf-8"))

    def test_same_execution_can_recover_failure_without_overwriting_newer_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            when = "2026-10-11T08:00:00+08:00"
            save_failure(root / "delivery", "交付中斷", "daily", reports_root=root / "reports", started_at=when)
            DecisionRepository(root / "decisions").save("example", world())
            result = build_delivery(root / "decisions", "example", root / "delivery", reports_root=root / "reports", started_at=when)
            self.assertTrue(result["published"])
            late_failure = save_failure(root / "delivery", "舊執行的遲到錯誤", "daily", reports_root=root / "reports", started_at=when)
            self.assertFalse(late_failure["published"])

    def test_cli_round_trip(self):
        import subprocess
        import sys
        script = Path(__file__).resolve().parents[2] / "cli/decision_delivery.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DecisionRepository(root / "decisions").save("example", world())
            result = subprocess.run([sys.executable, "-X", "utf8", str(script), "build", "--decision-repository", str(root / "decisions"), "--decision-run-id", "example", "--output-repository", str(root / "delivery")], capture_output=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            output = json.loads(result.stdout)
            check = subprocess.run([sys.executable, "-X", "utf8", str(script), "validate", "--output-repository", str(root / "delivery"), "--delivery-id", output["delivery_id"]], capture_output=True, encoding="utf-8")
            self.assertEqual(check.returncode, 0, check.stderr + check.stdout)
            self.assertTrue(json.loads(check.stdout)["decision_validated"])

    def test_current_integrated_team_and_trade_clock(self):
        from test_daily_pipeline import PipelineWorld, FakeRunner, team_outputs
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = PipelineWorld(root, approved_status=True)
            result = pipeline.run(FakeRunner(team_outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            delivery = build_delivery(pipeline.repository, "decision-fixture", root / "delivery")
            self.assertEqual(delivery["report_status"], "ready", delivery)
            report = json.loads((Path(delivery["directory"]) / "report.json").read_text(encoding="utf-8"))
            self.assertIsNotNone(report["provenance"]["trade_date"])
            self.assertEqual(report["analyst_reports"]["event"]["schema_version"], "2.2")

    def test_no_trade_has_holdings_and_no_orders(self):
        from test_trader import items
        b, *_ = valid_inputs()
        settings = policy()
        settings.update(cash_weight_ceiling="0.99", minimum_active_share="0.01")
        settings["content_sha256"] = decision_policy_sha256(settings)
        b["rules"].update(cash_weight_must_be_below="0.99", minimum_active_share="0.01")
        b["rules"]["config_sha256"] = decision_rules_sha256(b["rules"])
        b["bundle_sha256"] = decision_bundle_sha256(b)
        rows = items()
        rows[0].update(intent="no_trade", conviction=None, adopted_claim_ids=[], rejected_claim_ids=["bull-2317-1"])
        b, m, d, t, p, team, *_ = team_world(b, rows, settings)
        a, result = finalize(b, m, d, t, p, team)
        a.update(decision_input=b, momentum=m, debate=d, intent=t, policy=p, team_inputs=team, decision=result)
        report = build_report(a, "no-trade")
        self.assertEqual(report["decision_status"], "no_trade")
        self.assertEqual(report["orders"], [])
        self.assertTrue(report["portfolio_after_estimate"]["positions"])

    def test_resealed_invalid_result_is_not_certified(self):
        a = world()
        a["intent"]["items"][0]["rationale"] = "改寫"
        a["decision"] = DecisionFinalizer(a["decision_input"], a["policy"], a["momentum"], a["debate"], a["intent"], a["team_inputs"]).run(*(a[k] for k in ("proposal", "scenario", "guard", "risk_review", "revision_history")))
        self.assertEqual(a["decision"]["status"], "rejected")
        with self.assertRaises(DeliveryError):
            build_report(a, "bad-chain")

    def test_pipeline_delivery_only_requires_no_model(self):
        import importlib.util
        script = Path(__file__).resolve().parents[2] / "scripts/run_daily_pipeline.py"
        spec = importlib.util.spec_from_file_location("delivery_daily_test", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DecisionRepository(root / "artifacts/decisions").save("example", world())
            path = root / "artifacts/daily/2026-10-11/daily-20261011/pipeline.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"status": "failed", "planned_decision_run_id": "example", "started_at": "2026-10-11T08:00:00+08:00"}), encoding="utf-8")
            result = module.deliver_saved_run(root, "daily-20261011")
            self.assertEqual(result["report_status"], "ready")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "completed")
            # 已完成 run 的來源後來被竄改時，不保留 completed 假象。
            source = next((root / "artifacts/decisions").glob("*/example/decision.json"))
            source.write_text("{}", encoding="utf-8")
            blocked = module.deliver_saved_run(root, "daily-20261011")
            self.assertEqual(blocked["report_status"], "blocked")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"], "failed")
            self.assertIn("未產生已驗證", (root / "artifacts/latest/latest.md").read_text(encoding="utf-8"))

    def test_report_and_markdown_rebuild_and_tampering(self):
        a = world()
        report = build_report(a, "example")
        text = render_report(report)
        validate_report(a, "example", report, text)
        self.assertIn("尚未成交", text)
        self.assertIn("未提供", text)  # fixture 沒有交易時鐘，不推測日期。
        self.assertEqual(report["orders"], a["decision"]["orders"])
        with self.assertRaises(DeliveryError):
            validate_report(a, "example", report, text + "被改寫")
        report["orders"][0]["shares"] += 1000
        with self.assertRaises(DeliveryError):
            validate_report(a, "example", report, text)

    def test_markdown_is_compact_and_readable(self):
        report = build_report(world(), "compact")
        report["risk_summary"]["constraint_flags"] = [
            {"symbol": "AAA.TW", "code": "MAX_POSITIONS", "detail": "持股檔數已達上限"},
            {"symbol": "BBB.TW", "code": "MAX_POSITIONS", "detail": "持股檔數已達上限"}]
        text = render_report(report)
        self.assertNotIn("&quot;", text)
        self.assertIn("（MAX\\_POSITIONS，2 檔）：AAA.TW、BBB.TW", text)
        self.assertIn("### 參考價格", text)
        self.assertNotIn('"evidence"', text)  # 逐筆引用只留在 report.json。
        main = {row["symbol"] for row in report["decision_rows"] if row["before"] or row["after"] or row["order"]}
        appendix = text.split("## 附錄", 1)[1].split("## 引用與價格時間", 1)[0]
        for symbol in main:
            self.assertNotIn("### %s\n" % symbol, appendix)

    def test_sealed_markdown_is_rebuilt_with_its_own_template(self):
        report = build_report(world(), "versions")
        self.assertIs(renderer_for(render_report(report)), render_report)
        legacy = render_report_v1(report)
        self.assertIs(renderer_for(legacy), render_report_v1)
        self.assertNotEqual(legacy, render_report(report))
        v2 = render_report_v2(report)
        self.assertIs(renderer_for(v2), render_report_v2)
        self.assertIn("金額單位：未提供", v2)
        self.assertIn("估值時間：2026-09-20T00:55:00+00:00。", v2)

    def test_account_currency_and_valuation_time_are_shown_in_taipei(self):
        report = build_report(world(), "display")
        text = render_report(report)
        self.assertIn("金額單位：TWD（新台幣元）", text)
        self.assertIn("估值時間：2026-09-20T08:55:00+08:00（台北時間）。", text)
        self.assertEqual(report["account_before"]["valuation_at"], "2026-09-20T00:55:00+00:00")  # 封存值不改寫。
        broken = copy.deepcopy(report)
        broken["account_before"]["valuation_at"] = "2026-09-20T00:55:00"
        with self.assertRaises(ValueError):
            render_report(broken)

    def test_rejected_never_presents_proposal_orders(self):
        report = build_report(world(True), "reject")
        self.assertEqual(report["decision_status"], "rejected")
        self.assertEqual(report["orders"], [])
        self.assertIsNone(report["portfolio_after_estimate"])
        self.assertIsNone(report["order_totals"])
        self.assertIn("決策遭拒絕", render_report(report))

    def test_missing_team_and_modified_input_fail_closed(self):
        a = world()
        del a["team_inputs"]
        with self.assertRaises(DeliveryError):
            build_report(a, "missing")
        a = world()
        a["decision_input"]["decision_cutoff"] = "2026-09-20T10:00:00"
        with self.assertRaises(DeliveryError):
            build_report(a, "bad-time")

    def test_store_reuse_validate_and_failure_replaces_latest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DecisionRepository(root / "decisions").save("example", world())
            result = build_delivery(root / "decisions", "example", root / "delivery", reports_root=root / "reports", started_at="2026-10-11T08:00:00+08:00")
            self.assertEqual(result["report_status"], "ready")
            self.assertEqual(result["dplan_status"], "blocked")
            self.assertTrue(validate_delivery(root / "delivery", result["delivery_id"])["decision_validated"])
            again = build_delivery(root / "decisions", "example", root / "delivery")
            self.assertEqual(result["delivery_id"], again["delivery_id"])
            failure = save_failure(root / "delivery", "資料不足", "today", reports_root=root / "reports", started_at="2026-10-11T09:00:00+08:00")
            self.assertIn("資料不足", (root / "reports/latest.md").read_text(encoding="utf-8"))
            old = build_delivery(root / "decisions", "example", root / "delivery", reports_root=root / "reports", started_at="2026-10-11T08:00:00+08:00")
            self.assertFalse(old["published"])
            Path(result["report_path"]).write_text("竄改", encoding="utf-8")
            with self.assertRaises(DeliveryError):
                validate_delivery(root / "delivery", result["delivery_id"])

    def test_invalid_manifest_only_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            result = build_delivery(Path(directory) / "missing", "example", Path(directory) / "delivery")
            self.assertEqual(result["report_status"], "blocked")
            self.assertFalse((Path(result["directory"]) / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
