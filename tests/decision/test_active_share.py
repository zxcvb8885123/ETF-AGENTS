"""競賽 Active Share：前 15 大選取、主動型 ETF 持股基準載入的 fail-closed 分支與 Guard 拒絕。"""

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionInputValidator,
    DecisionToolError,
    ProposalValidator,
    ScenarioEngine,
    decision_bundle_sha256,
    decision_policy_sha256,
    decision_rules_sha256,
)
from etf_agent.decision.active_share import (
    ACTIVE_SHARE_TOP_N,
    HOLDINGS_FIELDS,
    active_share,
    load_active_etf_benchmarks,
    top_weights,
)

from test_portfolio_risk_decision import chain_inputs, policy

CUTOFF = "2026-10-26T21:00:00+08:00"


def _row(etf_code, rank, symbol, weight, name=None, available_at="2026-10-25T20:25:00+08:00", source="FIXTURE_HOLDINGS"):
    return [etf_code, str(rank), name or symbol or "", symbol, weight, "2026-09-30", available_at, source]


class TopWeightsTests(unittest.TestCase):
    def test_keeps_largest_fifteen_with_symbol_tie_break(self):
        weights = {"S%02d" % index: Decimal("0.03") for index in range(20)}
        weights["BIG"] = Decimal("0.10")
        top = top_weights(weights)
        self.assertEqual(len(top), ACTIVE_SHARE_TOP_N)
        self.assertIn("BIG", top)
        self.assertEqual(sorted(top)[:3], ["BIG", "S00", "S01"])
        self.assertNotIn("S19", top)

    def test_active_share_uses_nav_weights_without_renormalising(self):
        portfolio = {"A": Decimal("0.10"), "B": Decimal("0.05")}
        benchmark = {"A": Decimal("0.08"), "C": Decimal("0.04")}
        # 1/2 × (|0.10−0.08| + |0.05−0| + |0−0.04|) = 0.055
        self.assertEqual(active_share(portfolio, benchmark), Decimal("0.055"))
        self.assertEqual(active_share(portfolio, portfolio), Decimal("0"))


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.holdings = self.root / "holdings.csv"
        self.approvals = self.root / "sources.json"
        self.write_approvals("approved", "2026-10-25T20:45:00+08:00")
        self.rows = [
            _row("00980A", 1, "2330", "0.09"),
            _row("00980A", 2, "", "0.05", name="輝達"),
            _row("00981A", 1, "2454", "0.06"),
        ]

    def tearDown(self):
        self.temp.cleanup()

    def write_approvals(self, status, reviewed_at):
        self.approvals.write_text(json.dumps({"sources": [
            {"source": "FIXTURE_HOLDINGS", "license_status": status, "reviewed_at": reviewed_at}
        ]}), encoding="utf-8")

    def load(self, rows=None, header=None, required=("00980A", "00981A"), cutoff=CUTOFF):
        lines = [",".join(header or HOLDINGS_FIELDS)] + [",".join(row) for row in (self.rows if rows is None else rows)]
        self.holdings.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return load_active_etf_benchmarks(self.holdings, self.approvals, list(required), cutoff)

    def test_loads_required_benchmarks_with_unlisted_names_kept(self):
        benchmarks = self.load()
        self.assertEqual([item["benchmark_id"] for item in benchmarks], ["00980A", "00981A"])
        self.assertEqual(benchmarks[0]["weights"], {"2330": "0.09", "OTHER:輝達": "0.05"})
        self.assertTrue(benchmarks[0]["version"].startswith("active-etf-holdings:"))
        self.assertEqual(benchmarks[0]["available_at"], "2026-10-25T20:25:00+08:00")

    def test_version_changes_when_weights_are_modified(self):
        first = self.load()[0]["version"]
        self.rows[0] = _row("00980A", 1, "2330", "0.08")
        self.assertNotEqual(self.load()[0]["version"], first)

    def test_missing_required_etf_is_rejected(self):
        with self.assertRaisesRegex(DecisionToolError, "缺少 00982A"):
            self.load(required=("00980A", "00982A"))

    def test_more_than_fifteen_rows_is_rejected(self):
        rows = [_row("00980A", rank, "S%02d" % rank, "0.01") for rank in range(1, 17)]
        with self.assertRaisesRegex(DecisionToolError, "超過前 15 大"):
            self.load(rows=rows, required=("00980A",))

    def test_rank_gap_is_rejected(self):
        rows = [_row("00980A", 1, "2330", "0.09"), _row("00980A", 3, "2454", "0.05")]
        with self.assertRaisesRegex(DecisionToolError, "連續"):
            self.load(rows=rows, required=("00980A",))

    def test_duplicate_symbol_is_rejected(self):
        rows = [_row("00980A", 1, "2330", "0.09"), _row("00980A", 2, "2330", "0.05")]
        with self.assertRaisesRegex(DecisionToolError, "重複"):
            self.load(rows=rows, required=("00980A",))

    def test_invalid_weight_is_rejected(self):
        for weight in ("0", "1.5", "abc"):
            with self.subTest(weight=weight), self.assertRaises(DecisionToolError):
                self.load(rows=[_row("00980A", 1, "2330", weight)], required=("00980A",))

    def test_holdings_available_after_cutoff_are_rejected(self):
        rows = [_row("00980A", 1, "2330", "0.09", available_at="2026-10-27T09:00:00+08:00")]
        with self.assertRaisesRegex(DecisionToolError, "晚於 decision_cutoff"):
            self.load(rows=rows, required=("00980A",))

    def test_unapproved_source_is_rejected(self):
        self.write_approvals("research_only", "2026-10-25T20:45:00+08:00")
        with self.assertRaisesRegex(DecisionToolError, "未經核准"):
            self.load()

    def test_approval_after_cutoff_does_not_apply(self):
        with self.assertRaisesRegex(DecisionToolError, "未經核准"):
            self.load(cutoff="2026-10-25T20:30:00+08:00")

    def test_wrong_header_is_rejected(self):
        with self.assertRaisesRegex(DecisionToolError, "欄位"):
            self.load(header=["etf_code", "symbol", "weight", "source_date"])

    def test_cutoff_without_timezone_is_rejected(self):
        with self.assertRaises(DecisionToolError):
            self.load(cutoff="2026-10-26T21:00:00")


class BenchmarkSymbolFormatTests(unittest.TestCase):
    def errors_for(self, weights):
        bundle, *_ = chain_inputs()
        bundle["benchmarks"][0]["weights"] = weights
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        return [error for error in DecisionInputValidator(bundle).validate() if "benchmarks" in error]

    def test_bare_code_not_in_universe_format_is_rejected(self):
        errors = self.errors_for({"2330": "0.10"})
        self.assertTrue(any("不在交易池" in error for error in errors))

    def test_universe_symbol_and_unlisted_name_are_accepted(self):
        self.assertEqual(self.errors_for({"2330.TW": "0.10", "OTHER:輝達": "0.05"}), [])


class GuardActiveShareTests(unittest.TestCase):
    def guard_for(self, benchmark_weights, minimum=None):
        bundle, *_ = chain_inputs()
        bundle["benchmarks"][0]["weights"] = benchmark_weights
        settings = policy()
        if minimum is not None:
            bundle["rules"]["minimum_active_share"] = minimum
            bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
            settings["minimum_active_share"] = minimum
            settings["content_sha256"] = decision_policy_sha256(settings)
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        bundle, _, _, intent, *_ = chain_inputs(bundle)
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        return bundle, settings, intent, proposal, guard

    @staticmethod
    def check(guard):
        return next(item for item in guard["checks"] if item["rule_id"] == "ACTIVE_SHARE_ALL")

    def test_overlapping_buy_is_dropped_and_reallocated(self):
        *_, original, _ = self.guard_for({"OTHER:輝達": "0.50"})
        weights = {item["symbol"]: item["weight"] for item in original["allocation_proposal"]["positions"]}
        self.assertIn("2317.TW", weights)
        bundle, settings, intent, proposal, guard = self.guard_for(weights)
        symbols = {item["symbol"] for item in proposal["allocation_proposal"]["positions"]}
        self.assertNotIn("2317.TW", symbols)
        flags = [item for item in proposal["constraint_flags"] if item["code"] == "RISK_ACTIVE_SHARE"]
        self.assertEqual([item["symbol"] for item in flags], ["2317.TW"])
        self.assertIn("etf-0050-top10", flags[0]["detail"])
        self.assertTrue(self.check(guard)["passed"])
        self.assertTrue(guard["passed"])
        # 交易 Agent 的意圖不被改寫；Validator 以同一方法重算出相同提案。
        self.assertEqual(next(item["intent"] for item in intent["items"] if item["symbol"] == "2317.TW"), "buy")
        self.assertEqual(ProposalValidator(bundle, settings, intent).validate(proposal), [])

    def test_no_droppable_overlap_still_fails_guard(self):
        # 只與續抱的 2330.TW 重疊：程式不賣既有持股，Guard 照舊拒絕。
        _, _, _, proposal, guard = self.guard_for({"2330.TW": "0.25"}, minimum="0.20")
        self.assertEqual([item for item in proposal["constraint_flags"] if item["code"] == "RISK_ACTIVE_SHARE"], [])
        self.assertFalse(self.check(guard)["passed"])
        self.assertFalse(guard["passed"])

    def test_distinct_benchmark_passes_without_drops(self):
        _, _, _, proposal, guard = self.guard_for({"OTHER:輝達": "0.50"})
        self.assertTrue(self.check(guard)["passed"])
        self.assertEqual([item for item in proposal["constraint_flags"] if item["code"] == "RISK_ACTIVE_SHARE"], [])


if __name__ == "__main__":
    unittest.main()
