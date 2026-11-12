"""position_sizing.benchmark_facts v1（0050 基準事實，紀錄用，已實作、尚未開啟）的測試。

覆蓋：四個區塊的數字可重算、最新日與交易日不一致或歷史不足標 unavailable、價格 fetched_at 晚於截止／缺時區即拒絕、
持股檔 available_at 晚於截止／來源未核准／代號重複／權重不合法即拒絕、持股檔過期標 unavailable、內容被改動即拒絕、
決策輸入建立（有閘門才有 benchmark_input、沒有閘門逐位元不變）、簡報位置與提示、理由寫出鍵名即驗證失敗、
閘門形狀與人專用欄位、正式設定沒有開啟。這是機制測試（合成行情），不是對真實行情的驗證。
"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.data import MarketDataDatabase
from etf_agent.decision import (
    MARKET_REGIME_EVIDENCE_ID, DecisionInputBuilder, DecisionToolError, MomentumEngine,
)
from etf_agent.decision.benchmark_facts import (
    BENCHMARK_FACTS, BENCHMARK_INPUT_SCHEMA, benchmark_input_sha256, build_benchmark_facts, build_benchmark_input,
    validate_benchmark_facts_gate, validate_benchmark_input,
)
from etf_agent.decision.fields import decision_bundle_sha256
from etf_agent.decision.input_context import DecisionContext
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_cash_stance
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_stance_turn_facts import overnight_bundle
from test_daily_pipeline import FakeRunner
from test_decision_input_builder import builder_inputs, seed_history
from test_typed_evidence import typed_reports

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": BENCHMARK_FACTS, "max_holdings_age_days": 5}
POLICY = {
    "max_stock_weight": "0.1", "special_weight_limits": {"2330.TW": "0.25"},
    "position_sizing": {"benchmark_facts": GATE, "exposure_rotation": {"version": "v3", "beta_window_days": 60}},
}
CUTOFF_TEXT = "2026-09-20T00:55:00+00:00"
HOLD_ROWS = [("2330.TW", "0.5892"), ("2317.TW", "0.0302")]


def holdings_pack(rows=HOLD_ROWS, source_date="2026-09-19", available_at="2026-09-19T20:00:00+00:00"):
    return {
        "status": "available", "etf_code": "0050", "source": "YUANTA_ETF_WEIGHTS", "license_status": "approved",
        "source_date": source_date, "available_at": available_at,
        "rows": [{"rank": i, "symbol": s, "stock_name": s, "weight": w} for i, (s, w) in enumerate(rows, start=1)],
    }


def pack_for(bundle, closes=None, holdings=None):
    """0050 收盤與交易池同日（預設每日 +1%），含內容雜湊。"""
    days = sorted({bar["trade_date"] for row in bundle["price_series"] for bar in row["bars"]})
    closes = closes or [Decimal("100") * (Decimal("1.01") ** i) for i in range(len(days))]
    pack = {
        "schema_version": BENCHMARK_INPUT_SCHEMA, "snapshot_id": bundle["snapshot_id"],
        "decision_cutoff": bundle["decision_cutoff"],
        "benchmark": {"benchmark_id": "0050_buy_and_hold", "symbol": "0050.TW", "source": "TWSE_STOCK_DAY",
                      "license_status": "approved", "reviewed_at": "2026-10-23T23:50:00+08:00"},
        "prices": [{"trade_date": d, "close": str(c), "source": "TWSE_STOCK_DAY", "fetched_at": "2026-09-19T12:00:00+00:00"}
                   for d, c in zip(days, closes)],
        "holdings": holdings if holdings is not None else holdings_pack(),
    }
    pack["content_sha256"] = benchmark_input_sha256(pack)
    return pack


def with_pack(bundle, pack):
    output = copy.deepcopy(bundle)
    output["benchmark_input"] = pack
    output["bundle_sha256"] = decision_bundle_sha256(output)
    return output


def reseal(pack):
    pack["content_sha256"] = benchmark_input_sha256(pack)
    return pack


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.base = overnight_bundle()
        self.pack = pack_for(self.base)
        self.bundle = with_pack(self.base, self.pack)

    def facts(self, bundle=None):
        return build_benchmark_facts(bundle or self.bundle, POLICY)

    def test_return_block_is_recomputed(self):
        facts = self.facts()
        block = facts["benchmark_return"]
        closes = [Decimal(row["close"]) for row in self.pack["prices"]]
        self.assertEqual(block["status"], "available")
        self.assertEqual(block["return_1d"], str((closes[-1] / closes[-2] - 1).quantize(Decimal("0.000001"))))
        self.assertEqual(block["return_5d"], str((closes[-1] / closes[-6] - 1).quantize(Decimal("0.000001"))))
        self.assertIs(facts["citable"], False)
        self.assertNotIn("evidence_id", facts)

    def test_holdings_gap_uses_start_weights(self):
        facts = self.facts()
        gap = facts["holdings_gap_1d"]
        two = {r["symbol"]: Decimal(str(r["bars"][-1]["close"])) / Decimal(str(r["bars"][-2]["close"])) - 1 for r in self.base["price_series"]}
        weight = Decimal("0.2469879518072289156626506024096385542169")
        contribution = weight * two["2330.TW"]
        self.assertEqual(gap["status"], "available")
        self.assertEqual(gap["holdings_contribution"], str(contribution.quantize(Decimal("0.000001"))))
        bench = Decimal(facts["benchmark_return"]["return_1d"])
        self.assertEqual(gap["gap"], str((contribution - bench).quantize(Decimal("0.000001"))))

    def test_active_weights_rows_and_limits(self):
        block = self.facts()["active_weights"]
        self.assertEqual(block["status"], "available")
        row = {item["symbol"]: item for item in block["rows"]}
        self.assertEqual(row["2330.TW"]["benchmark_weight"], "0.589200")
        self.assertEqual(row["2330.TW"]["portfolio_weight"], "0.246988")
        self.assertEqual(row["2330.TW"]["difference"], "-0.342212")
        self.assertEqual(row["2330.TW"]["rule_weight_limit"], "0.250000")
        self.assertEqual(row["2317.TW"]["portfolio_weight"], "0.000000")
        self.assertEqual(row["2317.TW"]["rule_weight_limit"], "0.100000")
        self.assertEqual(block["benchmark_top_n_total"], "0.619400")
        self.assertEqual(block["age_trading_days"], 0)

    def test_beta_is_covariance_over_variance(self):
        # 0050 每日報酬固定 1% 時變異為 0 → 不可用；改用變動的序列。
        days = len(self.pack["prices"])
        closes = [Decimal(100)]
        for i in range(1, days):
            closes.append(closes[-1] * (Decimal("1.01") if i % 2 else Decimal("0.995")))
        bundle = with_pack(self.base, pack_for(self.base, closes))
        block = self.facts(bundle)["beta_to_benchmark"]
        self.assertEqual(block["status"], "available")
        window = 60
        span = sorted({bar["trade_date"] for row in self.base["price_series"] for bar in row["bars"]})[-(window + 1):]
        bars = {b["trade_date"]: Decimal(b["close"]) for b in self.base["price_series"][0]["bars"]}
        weight = Decimal("0.2469879518072289156626506024096385542169")
        port = [weight * (bars[d] / bars[p] - 1) for p, d in zip(span, span[1:])]
        by_date = dict(zip(sorted(bars), closes))
        bench = [by_date[d] / by_date[p] - 1 for p, d in zip(span, span[1:])]
        mp, mb = sum(port) / window, sum(bench) / window
        expected = sum((p - mp) * (b - mb) for p, b in zip(port, bench)) / sum((b - mb) ** 2 for b in bench)
        self.assertEqual(block["beta"], str(expected.quantize(Decimal("0.000001"))))
        self.assertEqual(block["beta_window_days"], 60)

    def test_constant_benchmark_return_has_no_beta(self):
        self.assertEqual(self.facts()["beta_to_benchmark"]["status"], "unavailable")

    def test_short_benchmark_history_is_unavailable_not_guessed(self):
        pack = copy.deepcopy(self.pack)
        pack["prices"] = pack["prices"][-10:]
        facts = self.facts(with_pack(self.base, reseal(pack)))
        self.assertEqual(facts["beta_to_benchmark"]["status"], "unavailable")
        self.assertEqual(facts["benchmark_return"]["status"], "available")

    def test_return_5d_is_null_with_short_history(self):
        pack = copy.deepcopy(self.pack)
        pack["prices"] = pack["prices"][-3:]
        block = self.facts(with_pack(self.base, reseal(pack)))["benchmark_return"]
        self.assertIsNone(block["return_5d"])
        self.assertEqual(block["status"], "available")

    def test_last_benchmark_day_before_trade_date_is_unavailable(self):
        pack = copy.deepcopy(self.pack)
        pack["prices"] = pack["prices"][:-1]
        facts = self.facts(with_pack(self.base, reseal(pack)))
        self.assertEqual(facts["benchmark_return"]["status"], "unavailable")
        self.assertEqual(facts["holdings_gap_1d"]["status"], "unavailable")

    def test_benchmark_day_after_trade_date_is_rejected(self):
        pack = copy.deepcopy(self.pack)
        pack["prices"].append({"trade_date": "2026-09-22", "close": "200", "source": "TWSE_STOCK_DAY",
                               "fetched_at": "2026-09-19T12:00:00+00:00"})
        with self.assertRaises(DecisionToolError):
            self.facts(with_pack(self.base, reseal(pack)))

    def test_stale_holdings_file_is_unavailable(self):
        pack = pack_for(self.base, holdings=holdings_pack(source_date="2026-09-01"))
        block = self.facts(with_pack(self.base, pack))["active_weights"]
        self.assertEqual(block["status"], "unavailable")
        self.assertGreater(block["age_trading_days"], 5)
        self.assertNotIn("rows", block)

    def test_empty_holdings_file_is_unavailable(self):
        pack = pack_for(self.base, holdings={"status": "unavailable", "reason": "0050 成分權重檔沒有資料列"})
        facts = self.facts(with_pack(self.base, pack))
        self.assertEqual(facts["active_weights"]["status"], "unavailable")
        self.assertEqual(facts["benchmark_return"]["status"], "available")

    def test_no_account_makes_account_blocks_unavailable(self):
        bundle = copy.deepcopy(self.bundle)
        del bundle["account_snapshot"]
        facts = self.facts(bundle)
        for key in ("holdings_gap_1d", "active_weights", "beta_to_benchmark"):
            self.assertEqual(facts[key]["status"], "unavailable", key)

    def test_missing_input_or_gate_is_rejected(self):
        with self.assertRaises(DecisionToolError):
            self.facts(self.base)
        with self.assertRaises(DecisionToolError):
            build_benchmark_facts(self.bundle, {"position_sizing": {}})

    def test_tampered_content_is_rejected(self):
        bundle = copy.deepcopy(self.bundle)
        bundle["benchmark_input"]["holdings"]["rows"][0]["weight"] = "0.1"
        with self.assertRaises(DecisionToolError):
            self.facts(bundle)

    def test_output_has_only_json_scalars(self):
        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            else:
                self.assertIsInstance(value, (str, bool, int, type(None)))
        walk(self.facts())


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.base = overnight_bundle()

    def errors(self, pack, **kwargs):
        return validate_benchmark_input(pack, self.base["snapshot_id"], self.base["decision_cutoff"], **kwargs)

    def test_valid_pack_has_no_errors(self):
        self.assertEqual(self.errors(pack_for(self.base)), [])

    def test_price_fetched_after_cutoff_or_without_timezone_is_rejected(self):
        for stamp in ("2026-09-20T01:00:00+00:00", "2026-09-19T12:00:00"):
            pack = pack_for(self.base)
            pack["prices"][-1]["fetched_at"] = stamp
            self.assertTrue(self.errors(reseal(pack)), stamp)

    def test_holdings_available_after_cutoff_or_without_timezone_is_rejected(self):
        for stamp in ("2026-09-20T01:00:00+00:00", "2026-09-19T12:00:00"):
            pack = pack_for(self.base, holdings=holdings_pack(available_at=stamp))
            self.assertTrue(self.errors(pack), stamp)

    def test_unapproved_sources_are_rejected(self):
        pack = pack_for(self.base)
        pack["holdings"]["license_status"] = "unverified"
        self.assertTrue(self.errors(reseal(pack)))
        pack = pack_for(self.base)
        pack["benchmark"]["license_status"] = "unverified"
        self.assertTrue(self.errors(reseal(pack)))
        pack = pack_for(self.base)
        pack["prices"][0]["source"] = "YAHOO"
        self.assertTrue(self.errors(reseal(pack)))

    def test_bad_weights_and_symbols_are_rejected(self):
        for rows in ([("2330.TW", "0.5"), ("2330.TW", "0.1")], [("2330.TW", "1.2")], [("2330.TW", "0")],
                     [("2330.TW", "0.7"), ("2317.TW", "0.4")], [("2330.tw", "0.5")], []):
            pack = pack_for(self.base, holdings=holdings_pack(rows=rows))
            self.assertTrue(self.errors(pack), rows)

    def test_more_than_ten_rows_is_rejected(self):
        rows = [("%d.TW" % (2000 + i), "0.01") for i in range(11)]
        self.assertTrue(self.errors(pack_for(self.base, holdings=holdings_pack(rows=rows))))

    def test_snapshot_mismatch_unknown_field_and_hash_are_rejected(self):
        pack = pack_for(self.base)
        pack["snapshot_id"] = "other"
        self.assertTrue(self.errors(reseal(pack)))
        pack = pack_for(self.base)
        pack["extra"] = 1
        self.assertTrue(self.errors(reseal(pack)))
        pack = pack_for(self.base)
        pack["prices"][0]["close"] = "1"
        self.assertTrue(self.errors(pack))

    def test_prices_must_increase_and_not_pass_the_trade_date(self):
        pack = pack_for(self.base)
        pack["prices"][1], pack["prices"][2] = pack["prices"][2], pack["prices"][1]
        self.assertTrue(self.errors(reseal(pack)))
        self.assertTrue(self.errors(pack_for(self.base), latest_trade_date="2026-09-10"))

    def test_bundle_validator_checks_the_key(self):
        from etf_agent.decision import DecisionInputValidator
        bundle = with_pack(self.base, pack_for(self.base))
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        baseline = DecisionInputValidator(self.base).validate()
        self.assertEqual(DecisionInputValidator(bundle).validate(), baseline)
        bundle["benchmark_input"]["prices"][-1]["fetched_at"] = "2026-09-21T00:00:00+00:00"
        reseal(bundle["benchmark_input"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        self.assertGreater(len(DecisionInputValidator(bundle).validate()), len(baseline))


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.snapshot, self.rules, self.history, self.account = builder_inputs()

    def test_without_the_key_the_bundle_is_unchanged(self):
        plain = DecisionInputBuilder(self.snapshot, self.rules, self.history, account_snapshot=self.account).build()
        none_given = DecisionInputBuilder(self.snapshot, self.rules, self.history, account_snapshot=self.account,
                                          benchmark_input=None).build()
        self.assertNotIn("benchmark_input", plain)
        self.assertEqual(plain, none_given)

    def test_with_the_key_it_enters_the_hash(self):
        plain = DecisionInputBuilder(self.snapshot, self.rules, self.history, account_snapshot=self.account).build()
        with_key = DecisionInputBuilder(self.snapshot, self.rules, self.history, account_snapshot=self.account,
                                        benchmark_input={"marker": 1}).build()
        self.assertEqual(with_key["benchmark_input"], {"marker": 1})
        self.assertNotEqual(plain["bundle_sha256"], with_key["bundle_sha256"])


class BuildInputTests(unittest.TestCase):
    """由資料庫、持股檔與核准檔建立 benchmark_input（as-of 讀法）。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.snapshot = {"snapshot_id": "s-1", "decision_cutoff": "2026-11-12T12:55:00+00:00", "latest_trade_date": "2026-11-12"}
        rows = []
        for day, close, fetched in (("2026-11-10", "100", "2026-11-10T09:00:00+00:00"),
                                    ("2026-11-11", "101", "2026-11-11T09:00:00+00:00"),
                                    ("2026-11-12", "102", "2026-11-12T09:00:00+00:00"),
                                    ("2026-11-12", "999", "2026-11-12T23:00:00+00:00")):  # cutoff 之後才抓到的版本，不得採用
            rows.append({"symbol": "0050.TW", "trade_date": day, "open_price": close, "high_price": close, "low_price": close,
                         "close_price": close, "volume_shares": 1000, "source": "TWSE_STOCK_DAY", "fetched_at": fetched})
        self.database = self.root / "market.db"
        seed_history(MarketDataDatabase(self.database), rows[:3] + [dict(rows[3], trade_date="2026-11-13")])
        self.holdings = self.root / "holdings.csv"
        self.approvals = self.root / "approvals.json"
        self.benchmarks = ROOT / "config" / "benchmarks.json"
        self.header = "etf_code,rank,stock_name,symbol,weight,source_date,available_at,source\n"
        self.line = "0050,1,台積電,2330.TW,0.5892,2026-11-12,2026-11-12T16:25:00+08:00,YUANTA_ETF_WEIGHTS\n"
        self.approve("approved")

    def approve(self, status, reviewed="2026-11-12T16:30:00+08:00"):
        self.approvals.write_text(json.dumps({"sources": [{"source": "YUANTA_ETF_WEIGHTS", "license_status": status,
                                                           "reviewed_at": reviewed}]}), encoding="utf-8")

    def build(self):
        return build_benchmark_input(self.database, self.holdings, self.approvals, self.benchmarks, self.snapshot)

    def test_builds_with_as_of_prices_and_holdings(self):
        self.holdings.write_text(self.header + self.line, encoding="utf-8")
        pack = self.build()
        self.assertEqual([row["trade_date"] for row in pack["prices"]], ["2026-11-10", "2026-11-11", "2026-11-12"])
        self.assertEqual(pack["prices"][-1]["close"], "102")
        self.assertEqual(pack["holdings"]["rows"][0]["weight"], "0.5892")
        self.assertEqual(pack["holdings"]["status"], "available")
        self.assertEqual(pack["content_sha256"], benchmark_input_sha256(pack))

    def test_header_only_holdings_file_is_unavailable_not_invented(self):
        self.holdings.write_text(self.header, encoding="utf-8")
        pack = self.build()
        self.assertEqual(pack["holdings"]["status"], "unavailable")
        self.assertEqual(len(pack["prices"]), 3)

    def test_unapproved_or_late_source_is_rejected(self):
        self.holdings.write_text(self.header + self.line, encoding="utf-8")
        self.approve("unverified")
        with self.assertRaises(DecisionToolError):
            self.build()
        self.approve("approved", reviewed="2026-11-13T00:00:00+08:00")  # 核准時間晚於截止
        with self.assertRaises(DecisionToolError):
            self.build()

    def test_holdings_available_after_cutoff_is_rejected(self):
        self.holdings.write_text(self.header + self.line.replace("2026-11-12T16:25:00+08:00", "2026-11-12T22:25:00+08:00"), encoding="utf-8")
        with self.assertRaises(DecisionToolError):
            self.build()

    def test_bad_header_is_rejected(self):
        self.holdings.write_text("a,b\n1,2\n", encoding="utf-8")
        with self.assertRaises(DecisionToolError):
            self.build()

    def test_shipped_files_are_consistent(self):
        """專案內實際的持股檔與核准檔可被讀取（來源核准、前幾大、權重合理）。"""
        from etf_agent.decision.benchmark_facts import load_benchmark_holdings
        pack = load_benchmark_holdings(ROOT / "data" / "benchmark_holdings.csv", ROOT / "config" / "benchmark_holdings_sources.json",
                                       "2026-11-13T00:00:00+08:00")
        self.assertEqual(pack["status"], "available")
        self.assertEqual(pack["rows"][0]["symbol"], "2330.TW")
        self.assertLessEqual(sum(Decimal(row["weight"]) for row in pack["rows"]), 1)


class GateTests(unittest.TestCase):
    def test_gate_is_a_registered_human_only_field(self):
        self.assertIn("benchmark_facts", GATE_FIELDS)
        self.assertIn("benchmark_facts", POSITION_SIZING_FIELDS)
        self.assertIn("benchmark_facts", FORBIDDEN_PREFIXES)

    def test_shape_is_closed(self):
        for gate in ({"version": "v2", "max_holdings_age_days": 5}, {"version": "v1"}, {"max_holdings_age_days": 5},
                     {"version": "v1", "max_holdings_age_days": 0}, {"version": "v1", "max_holdings_age_days": "5"},
                     {"version": "v1", "max_holdings_age_days": True}, {"version": "v1", "max_holdings_age_days": 5, "x": 1}, "v1"):
            errors = []
            validate_benchmark_facts_gate({"benchmark_facts": gate}, errors)
            self.assertTrue(errors, gate)
        errors = []
        validate_benchmark_facts_gate({"benchmark_facts": GATE}, errors)
        self.assertEqual(errors, [])

    def test_gate_requires_exposure_view_and_is_not_open_in_the_shipped_policy(self):
        from etf_agent.decision.sizing import GATE_PREREQUISITES
        rule = next(item for item in GATE_PREREQUISITES if item.field == "benchmark_facts")
        self.assertEqual(rule.requires, ("exposure_view",))
        self.assertTrue(rule.human_only)
        policy = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.assertNotIn("benchmark_facts", policy["position_sizing"])


class StanceTests(unittest.TestCase):
    def stance(self, rationale):
        return {"level": "neutral", "breadth": "balanced", "rationale": rationale, "evidence_ids": [MARKET_REGIME_EVIDENCE_ID]}

    def errors(self, bundle, rationale):
        errors = []
        validate_cash_stance(DecisionContext(bundle), self.stance(rationale), "cash_stance", errors)
        return errors

    def test_key_names_in_the_rationale_are_rejected_only_with_benchmark_input(self):
        base = overnight_bundle()
        bundle = with_pack(base, pack_for(base))
        for text in ("依 benchmark_facts 判斷", "active_weights 顯示落後", "beta_to_benchmark 偏低",
                     "holdings_gap_1d 為負", "benchmark_return 為正"):
            self.assertTrue(any("鍵名" in error for error in self.errors(bundle, text)), text)
            self.assertEqual(self.errors(base, text), [], text)
        self.assertEqual(self.errors(bundle, "市場中性，帳戶落後 0050 但不追高。"), [])


class BriefTests(unittest.TestCase):
    def run_stance(self, bundle, facts):
        momentum = MomentumEngine(bundle).run()
        runner = FakeRunner({"cash_stance": [{"level": "neutral", "breadth": "balanced", "rationale": "依市場事實。",
                                              "evidence_ids": [MARKET_REGIME_EVIDENCE_ID]}]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            kwargs = {} if facts is None else {"benchmark_facts": facts}
            pipeline.run_cash_stance(bundle, momentum, typed_reports(bundle), **kwargs)
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))
        return brief, runner

    def test_brief_and_prompt_carry_the_facts(self):
        base = overnight_bundle()
        facts = build_benchmark_facts(with_pack(base, pack_for(base)), POLICY)
        brief, runner = self.run_stance(base, facts)
        self.assertEqual(brief["benchmark_facts"], facts)
        self.assertIs(brief["benchmark_facts"]["citable"], False)
        self.assertIn("benchmark_facts", runner.prompts[0][1])

    def test_without_the_gate_brief_and_prompt_are_unchanged(self):
        brief, runner = self.run_stance(overnight_bundle(), None)
        self.assertNotIn("benchmark_facts", brief)
        self.assertNotIn("benchmark_facts", runner.prompts[0][1])
        self.assertNotIn("0050", runner.prompts[0][1])

    def test_untyped_bundle_with_facts_is_rejected(self):
        base = overnight_bundle(typed=False)
        facts = build_benchmark_facts(with_pack(base, pack_for(base)), POLICY)
        with self.assertRaises(DailyPipelineError):
            self.run_stance(base, facts)


if __name__ == "__main__":
    unittest.main()
