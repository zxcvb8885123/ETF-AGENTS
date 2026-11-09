"""position_sizing.market_index_facts v1（大盤指數事實，紀錄用）的測試。

覆蓋：重算數字（收盤、當日漲跌、近 5／20 日報酬、60 日高點回檔、MA20／MA60）、來源未核准與缺資料包的缺口、
歷史不足不補值、最後一列不是最近交易日、晚於最近交易日即拒絕、原始回應毀損即拒絕、閘門形狀與人專用欄位、
簡報位置與提示詞、沒有閘門時簡報逐鍵不變。這是機制測試（合成指數），不是對真實行情的驗證。
"""

import copy
import json
import tempfile
import unittest
from unittest import mock
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, DecisionToolError, MomentumEngine
from etf_agent.decision.market_index import MARKET_INDEX_FACTS, market_index_facts, validate_market_index_facts_gate
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_stance_turn_facts import overnight_bundle
from test_daily_pipeline import FakeRunner
from test_typed_evidence import typed_reports

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": MARKET_INDEX_FACTS}
SENTINEL = {"version": "v1", "marker": "synthetic"}


def trading_days(end, count):
    days, day = [], date.fromisoformat(end)
    while len(days) < count:
        if not days or day.weekday() < 5:  # 結束日一定算（fixture 的最近交易日可能落在週末）
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def twse_month(days, closes):
    """證交所 FMTQIK 月檔格式（民國年日期，第 5 欄收盤）。"""
    rows = []
    for day, close in zip(days, closes):
        rows.append(["%d/%02d/%02d" % (day.year - 1911, day.month, day.day), "1", "2", "3", "{:,.2f}".format(close), "0"])
    return json.dumps({"stat": "OK", "fields": ["日期", "成交股數", "成交金額", "成交筆數", "發行量加權股價指數", "漲跌點數"], "data": rows})


def with_index(bundle, closes, end=None, approved=True):
    """把合成加權指數放進決策輸入的 market_fear_greed_input（每月一份抓取）。"""
    end = end or bundle["snapshot"]["latest_trade_date"]
    days = trading_days(end, len(closes))
    captures = []
    for month in sorted({(d.year, d.month) for d in days}):
        part = [(d, c) for d, c in zip(days, closes) if (d.year, d.month) == month]
        captures.append({"source": "TWSE_TAIEX", "url": "https://example.invalid/%d%02d" % month,
                         "raw_text": twse_month([d for d, _ in part], [c for _, c in part]),
                         "status": "fetched", "available_at": "2026-11-09T13:00:00+00:00", "error": ""})
    approvals = [{"source": "TWSE_TAIEX", "license_status": "approved" if approved else "unverified"}]
    output = copy.deepcopy(bundle)
    output["market_fear_greed_input"] = {"bundle": {"captures": captures, "source_approvals": approvals}, "result": {}}
    return output


def rising(count, start=100, step=1):
    return [Decimal(start + step * i) for i in range(count)]


class IndexFactsTests(unittest.TestCase):
    def setUp(self):
        self.base = overnight_bundle()

    def test_numbers_are_recomputed_from_the_series(self):
        closes = rising(70)
        facts = market_index_facts(with_index(self.base, closes))
        self.assertEqual(facts["close"], "169.00")
        self.assertEqual(facts["day_change_points"], "1.00")
        self.assertEqual(facts["day_change_pct"], "0.60")
        self.assertEqual(facts["return_5d_pct"], str((Decimal(169) / Decimal(164) * 100 - 100).quantize(Decimal("0.01"))))
        self.assertEqual(facts["return_20d_pct"], str((Decimal(169) / Decimal(149) * 100 - 100).quantize(Decimal("0.01"))))
        self.assertEqual(facts["drawdown_from_60d_high_pct"], "0.00")
        self.assertIs(facts["above_ma20"], True)
        self.assertIs(facts["above_ma60"], True)
        self.assertEqual(facts["data_gaps"], [])
        self.assertIs(facts["citable"], False)

    def test_a_sharp_drop_shows_drawdown_and_below_averages(self):
        closes = rising(69) + [Decimal(50)]
        facts = market_index_facts(with_index(self.base, closes))
        self.assertEqual(facts["day_change_pct"], "-%s" % str((Decimal(168 - 50) / Decimal(168) * 100).quantize(Decimal("0.01"))))
        self.assertLess(Decimal(facts["drawdown_from_60d_high_pct"]), 0)
        self.assertIs(facts["above_ma20"], False)
        self.assertIs(facts["above_ma60"], False)

    def test_short_history_lists_gaps_and_does_not_guess(self):
        facts = market_index_facts(with_index(self.base, rising(10)))
        self.assertNotIn("return_20d_pct", facts)
        self.assertNotIn("above_ma60", facts)
        self.assertNotIn("drawdown_from_60d_high_pct", facts)
        self.assertEqual(facts["return_5d_pct"], str((Decimal(109) / Decimal(104) * 100 - 100).quantize(Decimal("0.01"))))
        self.assertTrue(any("20 日" in gap for gap in facts["data_gaps"]))

    def test_unapproved_source_is_not_counted(self):
        facts = market_index_facts(with_index(self.base, rising(70), approved=False))
        self.assertNotIn("close", facts)
        self.assertTrue(any("未核准" in gap for gap in facts["data_gaps"]))

    def test_no_fear_greed_pack_is_a_gap(self):
        facts = market_index_facts(self.base)
        self.assertNotIn("close", facts)
        self.assertTrue(facts["data_gaps"])

    def test_last_row_before_the_latest_trade_date_is_flagged(self):
        trade_date = self.base["snapshot"]["latest_trade_date"]
        earlier = (trading_days(trade_date, 2)[0]).isoformat()
        facts = market_index_facts(with_index(self.base, rising(70), end=earlier))
        self.assertEqual(facts["latest_date"], earlier)
        self.assertTrue(any("不是 snapshot.latest_trade_date" in gap for gap in facts["data_gaps"]))

    def test_row_after_the_latest_trade_date_is_rejected(self):
        later = (date.fromisoformat(self.base["snapshot"]["latest_trade_date"]) + timedelta(days=3)).isoformat()
        with self.assertRaises(DecisionToolError):
            market_index_facts(with_index(self.base, rising(70), end=later))

    def test_damaged_raw_response_is_rejected(self):
        bundle = with_index(self.base, rising(70))
        bundle["market_fear_greed_input"]["bundle"]["captures"][0]["raw_text"] = "{}"
        with self.assertRaises(DecisionToolError):
            market_index_facts(bundle)

    def test_output_is_strings_bools_and_lists_only(self):
        facts = market_index_facts(with_index(self.base, rising(70)))
        for key, value in facts.items():
            self.assertIsInstance(value, (str, bool, list), key)


class GateTests(unittest.TestCase):
    def test_gate_is_a_registered_human_only_field(self):
        self.assertIn("market_index_facts", GATE_FIELDS)
        self.assertIn("market_index_facts", POSITION_SIZING_FIELDS)
        self.assertTrue(any("market_index_facts".startswith(prefix) or prefix.endswith("market_index_facts") for prefix in FORBIDDEN_PREFIXES))

    def test_shape_is_closed(self):
        for gate in ({"version": "v2"}, {"version": "v1", "extra": 1}, "v1"):
            errors = []
            validate_market_index_facts_gate({"market_index_facts": gate}, errors)
            self.assertTrue(errors, gate)
        errors = []
        validate_market_index_facts_gate({"market_index_facts": GATE}, errors)
        self.assertEqual(errors, [])


class BriefTests(unittest.TestCase):
    def run_stance(self, bundle, gate):
        # 決策輸入有雜湊與恐懼貪婪資料包驗證，合成指數塞不進去：以 patch 讓簡報使用合成事實，數字重算由上面的測試負責。
        momentum = MomentumEngine(bundle).run()
        runner = FakeRunner({"cash_stance": [{"level": "neutral", "breadth": "balanced", "rationale": "依市場事實。",
                                              "evidence_ids": [MARKET_REGIME_EVIDENCE_ID]}]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            try:
                with mock.patch("etf_agent.automation.risk_stages.market_index_facts", return_value=SENTINEL):
                    result = pipeline.run_cash_stance(bundle, momentum, typed_reports(bundle), market_index_facts_gate=gate)
            except DailyPipelineError as error:
                result = error
            path = Path(directory) / "brief_cash_stance.json"
            brief = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        return result, brief, runner

    def test_brief_carries_the_facts_and_prompt_mentions_them(self):
        _, brief, runner = self.run_stance(overnight_bundle(), GATE)
        self.assertEqual(brief["market_index_facts"], SENTINEL)
        keys = list(brief)
        self.assertEqual(keys[keys.index("regime_assessment") + 1], "market_index_facts")
        self.assertIn("market_index_facts", runner.prompts[0][1])

    def test_without_gate_brief_and_prompt_are_unchanged(self):
        _, brief, runner = self.run_stance(overnight_bundle(), None)
        self.assertNotIn("market_index_facts", brief)
        self.assertNotIn("market_index_facts", runner.prompts[0][1])


if __name__ == "__main__":
    unittest.main()
