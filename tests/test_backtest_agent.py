import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from etf_agent.backtest import (
    AccountLedger,
    BacktestRequestValidator,
    BacktestRepository,
    BacktestService,
    BacktestToolError,
    FixturePointInTimeDataProvider,
    HistoricalClock,
)
from etf_agent.decision import artifact_content_sha256, canonical_sha256


def request():
    result = {
        "schema_version": "1.0",
        "request_id": "fixture-three-days",
        "start_date": "2026-09-01",
        "end_date": "2026-09-03",
        "timezone": "Asia/Taipei",
        "decision_time": "08:55",
        "initial_account": {
            "settled_cash": "500000",
            "unsettled_cash": "0",
            "positions": [{"symbol": "2330.TW", "shares": 1000, "cost_basis": "100"}],
        },
        "calendar": [
            {"trade_date": "2026-09-01", "decision_cutoff": "2026-09-01T08:55:00+08:00", "execution_at": "2026-09-01T09:00:00+08:00", "close_at": "2026-09-01T13:30:00+08:00", "settlement_date": "2026-09-02"},
            {"trade_date": "2026-09-02", "decision_cutoff": "2026-09-02T08:55:00+08:00", "execution_at": "2026-09-02T09:00:00+08:00", "close_at": "2026-09-02T13:30:00+08:00", "settlement_date": "2026-09-03"},
            {"trade_date": "2026-09-03", "decision_cutoff": "2026-09-03T08:55:00+08:00", "execution_at": "2026-09-03T09:00:00+08:00", "close_at": "2026-09-03T13:30:00+08:00", "settlement_date": "2026-09-04"},
        ],
        "data_manifest": {"mode": "fixture", "version": "fixture-v1", "content_sha256": "fixture-data"},
        "execution_assumptions": {"price_source_version": "fixture-unadjusted-v1", "lot_size": 1000, "commission_rate": "0.001425", "sell_tax_rate": "0.003", "minimum_commission": "20", "reuse_sell_proceeds": False},
    }
    result["content_sha256"] = canonical_sha256(result)
    return result


def decision(status, orders=None, suffix="1"):
    result = {"decision_id": "decision:" + suffix, "status": status, "orders": orders or [], "errors": []}
    result["content_sha256"] = artifact_content_sha256(result)
    return result


def research_versions():
    payload = {"evidence_id": "fixture-evidence", "version": 1}
    return [{"artifact_id": "research-v1", "available_at": "2026-08-31T20:00:00+08:00", "payload": payload, "content_sha256": canonical_sha256(payload)}]


def daily_inputs():
    return {
        "2026-09-01": {
            "research_versions": research_versions(),
            "decision": decision("approved", [
                {"symbol": "2330.TW", "side": "sell", "shares": 1000},
                {"symbol": "2317.TW", "side": "buy", "shares": 1000},
            ]),
            "execution_market": {"price_basis": "unadjusted", "available_at": "2026-09-01T09:00:00+08:00", "quotes": {"2330.TW": {"execution_price": "110", "tradable": True, "available_lots": 1}, "2317.TW": {"execution_price": "120", "tradable": True, "available_lots": 1}}},
            "close_market": {"price_basis": "unadjusted", "available_at": "2026-09-01T13:30:00+08:00", "quotes": {"2317.TW": "121"}},
        },
        "2026-09-02": {
            "research_versions": research_versions(),
            "decision": decision("no_trade", suffix="2"),
            "execution_market": {"price_basis": "unadjusted", "available_at": "2026-09-02T09:00:00+08:00", "quotes": {}},
            "close_market": {"price_basis": "unadjusted", "available_at": "2026-09-02T13:30:00+08:00", "quotes": {"2317.TW": "122"}},
            "corporate_actions": [{"type": "cash_dividend", "symbol": "2317.TW", "cash_per_share": "1", "payment_date": "2026-09-03"}],
        },
        "2026-09-03": {
            "research_versions": research_versions(),
            "decision": decision("no_trade", suffix="3"),
            "execution_market": {"price_basis": "unadjusted", "available_at": "2026-09-03T09:00:00+08:00", "quotes": {}},
            "close_market": {"price_basis": "unadjusted", "available_at": "2026-09-03T13:30:00+08:00", "quotes": {"2317.TW": "123"}},
        },
    }


class BacktestAgentTests(unittest.TestCase):
    def test_request_and_clock_are_valid(self):
        value = request()
        self.assertEqual(BacktestRequestValidator().validate(value), [])
        self.assertEqual([item["trade_date"] for item in HistoricalClock(value)], ["2026-09-01", "2026-09-02", "2026-09-03"])
        wrong_timezone = request()
        wrong_timezone["calendar"][0]["decision_cutoff"] = "2026-09-01T00:55:00+00:00"
        wrong_timezone["content_sha256"] = canonical_sha256({key: item for key, item in wrong_timezone.items() if key != "content_sha256"})
        self.assertIn("calendar[0].decision_cutoff 必須是交易日 08:55 +08:00", BacktestRequestValidator().validate(wrong_timezone))

    def test_provider_rejects_future_or_tampered_version(self):
        future = research_versions()
        future[0]["available_at"] = "2026-09-01T09:00:00+08:00"
        with self.assertRaisesRegex(BacktestToolError, "沒有可用"):
            FixturePointInTimeDataProvider(future).at("2026-09-01T08:55:00+08:00")
        broken = research_versions()
        broken[0]["payload"]["version"] = 2
        with self.assertRaisesRegex(BacktestToolError, "雜湊"):
            FixturePointInTimeDataProvider(broken).at("2026-09-01T08:55:00+08:00")

    def test_three_day_replay_settles_sale_and_dividend(self):
        result = BacktestService().run_fixture(request(), daily_inputs())
        self.assertEqual(result["status"], "completed")
        first = result["days"][0]
        self.assertEqual(len(first["execution"]["fills"]), 2)
        self.assertEqual(first["ledger"]["unsettled_cash"], "109513")
        second = result["days"][1]
        self.assertEqual(second["ledger"]["unsettled_cash"], "1000")
        third = result["days"][2]
        self.assertEqual(third["ledger"]["unsettled_cash"], "0")
        self.assertEqual(third["ledger"]["positions"][0]["shares"], 1000)
        self.assertEqual(BacktestService().validate_fixture(request(), daily_inputs(), result), [])

    def test_untradable_order_is_saved_as_unfilled(self):
        inputs = daily_inputs()
        inputs["2026-09-01"]["execution_market"]["quotes"]["2317.TW"]["tradable"] = False
        result = BacktestService().run_fixture(request(), inputs)
        unfilled = result["days"][0]["execution"]["unfilled_orders"]
        self.assertEqual(unfilled[0]["reason"], "NOT_TRADABLE")

    def test_buy_cannot_reuse_unsettled_sale_when_rule_disallows_it(self):
        value = request()
        value["initial_account"]["settled_cash"] = "0"
        value["content_sha256"] = canonical_sha256({key: item for key, item in value.items() if key != "content_sha256"})
        result = BacktestService().run_fixture(value, daily_inputs())
        first = result["days"][0]
        self.assertEqual([fill["side"] for fill in first["execution"]["fills"]], ["sell"])
        self.assertEqual(first["execution"]["unfilled_orders"][0]["symbol"], "2317.TW")
        self.assertEqual(first["ledger"]["unsettled_cash"], "109513")

    def test_execution_market_cannot_arrive_after_execution_time(self):
        inputs = daily_inputs()
        inputs["2026-09-01"]["execution_market"]["available_at"] = "2026-09-01T09:01:00+08:00"
        with self.assertRaisesRegex(BacktestToolError, "晚於 execution_at"):
            BacktestService().run_fixture(request(), inputs)

    def test_adjusted_or_early_close_market_is_rejected(self):
        adjusted = daily_inputs()
        adjusted["2026-09-01"]["close_market"]["price_basis"] = "adjusted"
        with self.assertRaisesRegex(BacktestToolError, "未還原價格"):
            BacktestService().run_fixture(request(), adjusted)
        early = daily_inputs()
        early["2026-09-01"]["close_market"]["available_at"] = "2026-09-01T13:29:00+08:00"
        with self.assertRaisesRegex(BacktestToolError, "早於 close_at"):
            BacktestService().run_fixture(request(), early)

    def test_split_preserves_whole_lots_and_rejects_fractional_shares(self):
        ledger = AccountLedger(request()["initial_account"])
        ledger.apply_actions([{"type": "split", "symbol": "2330.TW", "numerator": 2, "denominator": 1}])
        self.assertEqual(ledger.positions["2330.TW"]["shares"], 2000)
        fractional = AccountLedger(request()["initial_account"])
        with self.assertRaisesRegex(BacktestToolError, "零股"):
            fractional.apply_actions([{"type": "split", "symbol": "2330.TW", "numerator": 3, "denominator": 2}])

    def test_historical_verified_mode_requires_full_decision_rebuild_inputs(self):
        value = request()
        value["data_manifest"]["mode"] = "historical_verified"
        value["content_sha256"] = canonical_sha256({key: item for key, item in value.items() if key != "content_sha256"})
        inputs = daily_inputs()
        for item in inputs.values():
            for version in item["research_versions"]:
                version.update({"evidence_id": "evidence:fixture", "source": "fixture", "content_as_of": "2026-08-31T19:00:00+08:00", "published_at": "2026-08-31T19:30:00+08:00", "fetched_at": "2026-08-31T20:00:00+08:00"})
        with self.assertRaisesRegex(BacktestToolError, "完整 DecisionResult"):
            BacktestService().run_fixture(value, inputs)

    def test_repository_detects_tampered_run(self):
        result = BacktestService().run_fixture(request(), daily_inputs())
        with tempfile.TemporaryDirectory() as directory:
            repository = BacktestRepository(Path(directory))
            saved = repository.save("fixture-run", {"request": request(), "daily_inputs": daily_inputs(), "run": result})
            self.assertEqual(repository.verify("fixture-run"), saved)
            (saved / "run.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(BacktestToolError, "manifest"):
                repository.verify("fixture-run")

    def test_coverage_and_report_are_deterministic(self):
        service = BacktestService()
        self.assertTrue(service.inspect_fixture(request(), daily_inputs())["valid"])
        run = service.run_fixture(request(), daily_inputs())
        report = service.build_report(request(), run)
        self.assertEqual(report["last_nav"], run["days"][-1]["ledger"]["nav"])
        self.assertEqual(report["content_sha256"], artifact_content_sha256(report))
        self.assertIn("回測帳務驗收報告", service.render_report_markdown(report))

    def test_cli_replay_and_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_path, daily_path, result_path = root / "request.json", root / "daily.json", root / "run.json"
            request_path.write_text(json.dumps(request()), encoding="utf-8")
            daily_path.write_text(json.dumps(daily_inputs()), encoding="utf-8")
            script = Path(__file__).parents[1] / "cli" / "backtest.py"
            base = [sys.executable, str(script), "--request", str(request_path), "--daily-inputs", str(daily_path)]
            replay = subprocess.run(base + ["replay", "--output", str(result_path)], capture_output=True, text=True)
            self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
            validate = subprocess.run(base + ["validate-run", "--input", str(result_path)], capture_output=True, text=True)
            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)
            coverage = subprocess.run(base + ["inspect-coverage"], capture_output=True, text=True)
            self.assertEqual(coverage.returncode, 0, coverage.stdout + coverage.stderr)
            report = subprocess.run([sys.executable, str(script), "--request", str(request_path), "build-report", "--input", str(result_path)], capture_output=True, text=True)
            self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
