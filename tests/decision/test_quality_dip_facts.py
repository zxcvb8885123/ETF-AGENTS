"""position_sizing.quality_dip_facts v1（優質股被大盤拖累的事實，第一階段，紀錄用）的測試。

覆蓋：

1. 事實本身（合成行情）：視窗前 beta 與殘差、交易池內名次、``price_pattern``／``candidate`` 互斥且可重算、殘差為 0 歸
   ``market_explained``、超跌固定帶 ``cause: unknown``、歷史不足降級為 unavailable、加權指數未核准只讓指數欄 null。
2. 時間：bar 晚於最近交易日、bar 或文件可得時間缺時區、bar 可得時間晚於 cutoff 一律拒絕；晚於 cutoff 的文件不計入。
3. 三個清單不混用：月營收轉負（金融業改列疑慮）、淨利由盈轉虧、事件面負面只進新壞事；年減只進疑慮；指標 unavailable、
   缺月營收、缺現金流只進不知道；單位衝突列不知道、不比大小；疑慮與不知道不改變候選標示。
4. 閘門：形狀、人專用欄位、策略反思不得改；``validate_dip_facts`` 竄改／缺漏／無閘門即拒絕；交易 Agent 文字引用背景識別碼即拒絕。
5. 整條鏈（真實 Finalizer）：閘門開啟＋dip_facts 通過；缺 dip_facts、竄改、沒有閘門卻帶 dip_facts 一律 rejected；
   配置輸出（訂單、配置、旗標）與沒有閘門時相同。
6. 每日 pipeline：技術／基本面分析師簡報多附事實（不含 price_pattern／candidate）、交易 Agent 簡報附 dip_background、
   多空研究員簡報沒有；研究員不得讀 dip_facts 檔；沒有閘門時簡報與 team_inputs 逐鍵不變。
7. 封存 run（本機才有，CI 沒有時略過）：2026-11-10 案例與 docs/quality_dip_plan.md 第 6.1 節的表比對；
   沒有閘門時封存 team_inputs 的重建驗證不變、``exposure_review`` 逐位元不變。

合成行情是機制測試，不是對真實行情的驗證；封存案例的證據等級是 replay_reference_only。
"""

import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from pathlib import Path
from unittest import mock

from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionFinalizer,
    DecisionRepository,
    DecisionToolError,
    MomentumEngine,
    TradeDecisionValidator,
    validate_exposure_review,
)
from etf_agent.decision.dip_facts import (
    CANDIDATES,
    FACTS_FIELDS,
    ITEM_FIELDS,
    PRICE_PATTERNS,
    analyst_dip_view,
    build_dip_facts,
    dip_background_brief,
    dip_reference_tokens,
    quality_dip_facts_gate,
    validate_dip_facts,
    validate_quality_dip_facts_gate,
)
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_gate_prerequisites
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

import test_exposure_rotation_e2e as e2e
from test_daily_pipeline import FakeRunner, PipelineWorld, team_outputs
from test_market_index_facts import with_index

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": "v1"}
TRADE_DATE = "2026-11-09"
CUTOFF = "2026-11-09T23:00:00+00:00"
SYMBOLS = tuple("S%d.TW" % index for index in range(10))
ARCHIVED = ROOT / "artifacts" / "decisions" / "2026-11-10" / "decision-20261109T230435Z"
DECISIONS = ROOT / "artifacts" / "decisions"


def weekdays(end, count):
    days, day = [], date.fromisoformat(end)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    return sorted(days)


def dip_bundle(days=100, extras=None, window=20, history=None):
    """10 檔合成行情：視窗前每檔日報酬都等於同一個市場序列（beta 恰為 1），評估視窗內市場每日 −1%，
    再加各檔的每日額外報酬 ``extras``。S9 的視窗日報酬等於其他九檔的平均，殘差恰為 0。"""
    extras = extras if extras is not None else {"S0.TW": "-0.01", "S1.TW": "0.005", "S2.TW": "0.03"}
    dates = weekdays(TRADE_DATE, days)
    market = [Decimal("0.01") * Decimal((index % 3) - 1) + Decimal("0.002") for index in range(days)]
    closes = {symbol: [Decimal(100)] for symbol in SYMBOLS}
    for index in range(1, days):
        in_window = index >= days - window
        returns = {}
        for symbol in SYMBOLS[:-1]:
            returns[symbol] = (Decimal("-0.01") + Decimal(extras.get(symbol, "0"))) if in_window else market[index]
        returns[SYMBOLS[-1]] = sum(returns.values(), Decimal(0)) / Decimal(9) if in_window else market[index]
        for symbol, value in returns.items():
            closes[symbol].append(closes[symbol][-1] * (Decimal(1) + value))
    series = []
    for symbol in SYMBOLS:
        bars = [{"trade_date": day, "close": str(close), "available_at": "%sT14:00:00+08:00" % day}
                for day, close in zip(dates, closes[symbol])]
        if history is not None and symbol in history:
            bars = bars[-history[symbol]:]
        series.append({"symbol": symbol, "evidence_id": "price:%s" % symbol, "bars": bars})
    return {
        "decision_cutoff": CUTOFF,
        "fundamental_period_policy": "per_symbol_latest_available_v1",
        "evidence_policy": "typed_evidence_v1",
        "snapshot": {"latest_trade_date": TRADE_DATE, "documents": []},
        "price_series": series,
    }


def revenue_doc(symbol, period, yoy, available_at, doc_id):
    return {
        "document_type": "monthly_revenue", "symbol": symbol, "source_evidence_id": "document:%d" % doc_id,
        "available_at": available_at, "monthly_revenue": {"revenue_period": period, "yoy_pct": yoy},
    }


def metric(symbol, key, value, unit, status="available", reason=None, deps=None):
    return {
        "metric_key": key, "status": status, "value": value, "unit": unit, "period": "2026Q2",
        "comparison_period": "2025Q2", "reason_code": reason,
        "dependencies": deps if deps is not None else [
            {"scope": "current", "value": "100", "currency": "TWD", "source_evidence_id": "document:900"},
            {"scope": "prior_year", "value": "90", "currency": "TWD", "source_evidence_id": "document:901"},
        ],
    }


def metrics_of(*items):
    return {"items": [{"symbol": symbol, "metrics": rows} for symbol, rows in items]}


def event_report(**outlooks):
    """{symbol: (event_outlook, [(evidence_id, materiality), ...])}；沒列的股票 neutral、沒有事件。"""
    items = []
    for symbol in SYMBOLS:
        outlook, events = outlooks.get(symbol, ("neutral", []))
        items.append({"symbol": symbol, "event_outlook": outlook,
                      "events": [{"evidence_id": evidence, "materiality": level} for evidence, level in events]})
    return {"items": items}


def facts(bundle=None, metrics=None, events=None):
    return build_dip_facts(bundle or dip_bundle(), GATE, metrics, events or event_report())


def item(result, symbol):
    return next(row for row in result["items"] if row["symbol"] == symbol)


# ---------------------------------------------------------------- 1. 價格事實與候選標示
class PriceFactsTests(unittest.TestCase):
    def setUp(self):
        self.result = facts()

    def test_shape_is_the_whitelist_and_deterministic(self):
        self.assertEqual(set(self.result), FACTS_FIELDS)
        self.assertEqual(self.result["data_status"], "available")
        for row in self.result["items"]:
            self.assertEqual(set(row), ITEM_FIELDS)
            self.assertIn(row["price_pattern"], PRICE_PATTERNS)
            self.assertIn(row["candidate"], CANDIDATES)
        self.assertEqual(self.result, facts())
        self.assertTrue(self.result["facts_id"].startswith("dip-facts:"))

    def test_windows_are_adjacent_on_the_union_date_grid(self):
        window = self.result["window"]
        dates = weekdays(TRADE_DATE, 100)
        self.assertEqual((window["base_date"], window["first_date"], window["last_date"]), (dates[-21], dates[-20], dates[-1]))
        self.assertEqual((window["beta_first_date"], window["beta_last_date"]), (dates[-81], dates[-21]))

    def test_pre_window_beta_excludes_the_decline(self):
        # 視窗前每檔都跟市場同步：beta 恰為 1；視窗內的下殺沒有灌高 beta。
        for symbol in SYMBOLS:
            self.assertEqual(item(self.result, symbol)["market_attribution"]["beta_pre_window"], "1.000000")

    def test_residual_signs_ranks_and_patterns(self):
        weak, mild, rising = item(self.result, "S0.TW"), item(self.result, "S1.TW"), item(self.result, "S2.TW")
        self.assertEqual(weak["market_attribution"]["residual_rank"], 1)
        self.assertEqual(weak["price_pattern"], "beyond_market")
        self.assertEqual((weak["candidate"], weak["cause"]), ("oversold", "unknown"))
        self.assertEqual(mild["price_pattern"], "market_explained")
        self.assertEqual((mild["candidate"], mild["cause"]), ("market_dragged", None))
        self.assertEqual((rising["price_pattern"], rising["candidate"]), ("no_decline", "none"))
        ranks = sorted(row["market_attribution"]["residual_rank"] for row in self.result["items"])
        self.assertEqual(ranks, list(range(1, 11)))
        self.assertEqual(self.result["pool"]["residual_sample"], 10)

    def test_zero_residual_is_market_explained(self):
        flat = item(self.result, "S9.TW")
        self.assertEqual(flat["market_attribution"]["residual_20d"], "0.000000")
        self.assertEqual(flat["price_pattern"], "market_explained")

    def test_relative_to_pool_uses_the_simple_mean_and_residual_uses_the_compounded_pool(self):
        pool = self.result["pool"]
        returns = [Decimal(row["market_attribution"]["return_20d"]) for row in self.result["items"]]
        self.assertAlmostEqual(float(Decimal(pool["return_20d"])), float(sum(returns) / len(returns)), places=5)
        self.assertNotEqual(pool["return_20d"], pool["compound_return_20d"])
        weak = item(self.result, "S0.TW")["market_attribution"]
        expected = Decimal(weak["return_20d"]) - Decimal(pool["compound_return_20d"])
        self.assertAlmostEqual(float(Decimal(weak["residual_20d"])), float(expected), places=5)

    def test_price_position_is_not_a_valuation(self):
        position = item(self.result, "S0.TW")["price_position"]
        self.assertEqual(set(position), {"close_vs_high_60d", "close_vs_high_120d", "close_vs_ma20", "close_vs_ma60", "new_low_20d"})
        self.assertIsNone(position["close_vs_high_120d"])  # 只有 100 日歷史，不補值
        self.assertIs(position["new_low_20d"], True)
        self.assertNotIn("valuation", json.dumps(self.result))

    def test_short_history_makes_everything_unavailable_but_keeps_returns(self):
        result = facts(dip_bundle(days=60))
        self.assertEqual(result["data_status"], "unavailable")
        self.assertTrue(result["data_gaps"])
        for row in result["items"]:
            self.assertEqual((row["price_pattern"], row["candidate"]), ("unavailable", "none"))
            self.assertIsNone(row["market_attribution"]["residual_20d"])
            self.assertIsNotNone(row["market_attribution"]["return_20d"])

    def test_a_stock_with_a_missing_day_has_no_beta_and_thin_coverage_is_unavailable(self):
        bundle = dip_bundle()
        row = bundle["price_series"][3]
        row["bars"] = [bar for index, bar in enumerate(row["bars"]) if index != 30]
        result = facts(bundle)
        self.assertIsNone(item(result, "S3.TW")["market_attribution"]["beta_pre_window"])
        self.assertEqual(item(result, "S3.TW")["price_pattern"], "unavailable")
        self.assertEqual(result["pool"]["residual_sample"], 9)  # 9／10 仍高於 80%
        for index in (4, 5):
            bundle["price_series"][index]["bars"] = [bar for n, bar in enumerate(bundle["price_series"][index]["bars"]) if n != 30]
        self.assertEqual(facts(bundle)["data_status"], "unavailable")  # 7／10 低於 80%

    def test_index_relative_return_and_unapproved_source(self):
        closes = [Decimal(1000) + Decimal(index) for index in range(100)]
        approved = facts(with_index(dip_bundle(), closes))
        self.assertIsNotNone(item(approved, "S0.TW")["market_attribution"]["relative_to_index_20d"])
        self.assertIsNotNone(approved["pool"]["index_return_20d"])
        unapproved = facts(with_index(dip_bundle(), closes, approved=False))
        self.assertIsNone(item(unapproved, "S0.TW")["market_attribution"]["relative_to_index_20d"])
        self.assertTrue(any("未核准" in gap for gap in unapproved["data_gaps"]))
        # 指數不可用不影響交易池口徑。
        self.assertEqual(item(unapproved, "S0.TW")["price_pattern"], "beyond_market")


# ---------------------------------------------------------------- 2. 時間
class TimeTests(unittest.TestCase):
    def test_bar_after_the_latest_trade_date_is_rejected(self):
        bundle = dip_bundle()
        bundle["price_series"][0]["bars"].append({"trade_date": "2026-11-10", "close": "1", "available_at": "2026-11-10T14:00:00+08:00"})
        with self.assertRaises(DecisionToolError):
            facts(bundle)

    def test_bar_time_without_timezone_or_after_cutoff_is_rejected(self):
        for value in ("2026-11-09T14:00:00", "2026-11-10T09:00:00+00:00"):
            bundle = dip_bundle()
            bundle["price_series"][0]["bars"][-1]["available_at"] = value
            with self.subTest(value), self.assertRaises(DecisionToolError):
                facts(bundle)

    def test_document_time_without_timezone_is_rejected(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"].append(revenue_doc("S1.TW", "2026-10", "-5", "2026-11-05T10:00:00", 1))
        with self.assertRaises(DecisionToolError):
            facts(bundle)

    def test_document_after_cutoff_is_not_counted(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"] += [
            revenue_doc("S1.TW", "2026-09", "5", "2026-10-01T10:00:00+00:00", 1),
            revenue_doc("S1.TW", "2026-10", "-5", "2026-11-10T10:00:00+00:00", 2),
        ]
        flags = item(facts(bundle), "S1.TW")["fundamental_flags"]
        self.assertEqual(flags["new_adverse_facts"], [])
        self.assertEqual(flags["latest_revenue_available_at"], "2026-10-01T10:00:00+00:00")


# ---------------------------------------------------------------- 3. 三個清單
class FlagListTests(unittest.TestCase):
    def window_date(self, offset=0):
        return "%sT10:00:00+00:00" % weekdays(TRADE_DATE, 100)[-10 + offset]

    def test_revenue_turning_negative_in_the_window_is_new_adverse_and_drops_the_candidate(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"] += [
            revenue_doc("S1.TW", "2026-09", "5", "2026-09-20T10:00:00+00:00", 1),
            revenue_doc("S1.TW", "2026-10", "-5", self.window_date(), 2),
        ]
        row = item(facts(bundle), "S1.TW")
        adverse = row["fundamental_flags"]["new_adverse_facts"]
        self.assertEqual([fact["item"] for fact in adverse], ["monthly_revenue_yoy_turned_negative"])
        self.assertEqual(adverse[0]["evidence_ids"], ["document:2", "document:1"])
        self.assertEqual(row["price_pattern"], "market_explained")
        self.assertEqual(row["candidate"], "none")
        self.assertIs(row["fundamental_flags"]["no_new_fundamental_data_in_window"], False)

    def test_financial_industry_turn_is_only_a_concern(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"] += [
            revenue_doc("S1.TW", "2026-09", "5", "2026-09-20T10:00:00+00:00", 1),
            revenue_doc("S1.TW", "2026-10", "-5", self.window_date(), 2),
            {"document_type": "financial_statement", "symbol": "S1.TW", "source_evidence_id": "document:3",
             "available_at": "2026-08-20T10:00:00+00:00", "financial_statement": {"industry": "fh"}},
        ]
        row = item(facts(bundle), "S1.TW")
        self.assertEqual(row["fundamental_flags"]["new_adverse_facts"], [])
        self.assertEqual([c["item"] for c in row["fundamental_flags"]["concerns"]], ["monthly_revenue_yoy_turned_negative"])
        self.assertEqual(row["candidate"], "market_dragged")

    def test_old_turn_missing_previous_and_missing_documents(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"] += [
            revenue_doc("S1.TW", "2026-08", "5", "2026-08-20T10:00:00+00:00", 1),
            revenue_doc("S1.TW", "2026-09", "-5", "2026-09-20T10:00:00+00:00", 2),  # 視窗前就轉負：不是新壞事
            revenue_doc("S2.TW", "2026-10", "-5", self.window_date(), 3),  # 沒有前一筆：不知道
        ]
        result = facts(bundle)
        self.assertEqual(item(result, "S1.TW")["fundamental_flags"]["new_adverse_facts"], [])
        self.assertIs(item(result, "S1.TW")["fundamental_flags"]["no_new_fundamental_data_in_window"], True)
        self.assertIn("NO_PREVIOUS_MONTH_YOY", [u["reason_code"] for u in item(result, "S2.TW")["fundamental_flags"]["unknown"]])
        self.assertIn("NO_MONTHLY_REVENUE_DOCUMENT", [u["reason_code"] for u in item(result, "S3.TW")["fundamental_flags"]["unknown"]])

    def test_year_over_year_declines_are_concerns_only_and_do_not_change_the_label(self):
        metrics = metrics_of(("S1.TW", [
            metric("S1.TW", "revenue_yoy_pct", "-0.77", "percent"),
            metric("S1.TW", "net_income_yoy_pct", "-14.95", "percent"),
            metric("S1.TW", "operating_margin_pp_yoy", "-4.385", "percentage_points"),
        ]))
        row = item(facts(metrics=metrics), "S1.TW")
        flags = row["fundamental_flags"]
        self.assertEqual(flags["new_adverse_facts"], [])
        self.assertEqual(sorted(c["item"] for c in flags["concerns"]),
                         ["net_income_yoy_pct", "operating_margin_pp_yoy", "revenue_yoy_pct"])
        margin = next(c for c in flags["concerns"] if c["item"] == "operating_margin_pp_yoy")
        self.assertEqual((margin["value"], margin["period"]), ("-4.39", "2026Q2"))
        self.assertEqual(row["candidate"], "market_dragged")

    def test_unavailable_metrics_and_cash_flow_are_unknown_only(self):
        metrics = metrics_of(("S0.TW", [
            metric("S0.TW", "revenue_yoy_pct", None, None, status="unavailable", reason="COMPARABLE_FACT_NOT_REPORTED"),
            metric("S0.TW", "net_income_yoy_pct", None, None, status="unavailable", reason="NONPOSITIVE_COMPARISON_BASE"),
            metric("S0.TW", "operating_margin_pp_yoy", None, None, status="unavailable", reason="UNSUPPORTED_INDUSTRY"),
        ]))
        row = item(facts(metrics=metrics), "S0.TW")
        flags = row["fundamental_flags"]
        self.assertEqual((flags["new_adverse_facts"], flags["concerns"]), ([], []))
        reasons = {u["item"]: u["reason_code"] for u in flags["unknown"]}
        self.assertEqual(reasons["net_income_yoy_pct"], "NONPOSITIVE_COMPARISON_BASE")
        self.assertEqual(reasons["cash_flow"], "NO_CASH_FLOW_DOCUMENT")
        # 不知道既不否決也不背書：超跌標示照舊。
        self.assertEqual((row["candidate"], row["cause"]), ("oversold", "unknown"))
        self.assertIn("NO_FINANCIAL_METRICS", [u["reason_code"] for u in item(facts(metrics=metrics), "S1.TW")["fundamental_flags"]["unknown"]])

    def test_unit_conflict_is_unknown_and_never_compared(self):
        foreign = [
            {"scope": "current", "value": "100", "currency": "USD", "source_evidence_id": "document:900"},
            {"scope": "prior_year", "value": "90", "currency": "TWD", "source_evidence_id": "document:901"},
        ]
        metrics = metrics_of(("S1.TW", [
            metric("S1.TW", "revenue_yoy_pct", "-3", "percentage_points"),
            metric("S1.TW", "net_income_yoy_pct", "-3", "percent", deps=foreign),
        ]))
        flags = item(facts(metrics=metrics), "S1.TW")["fundamental_flags"]
        self.assertEqual(flags["concerns"], [])
        conflicts = sorted(u["item"] for u in flags["unknown"] if u["reason_code"] == "UNIT_CONFLICT")
        self.assertEqual(conflicts, ["net_income_yoy_pct", "revenue_yoy_pct"])

    def test_net_income_turning_to_loss_in_the_window_is_new_adverse_not_also_a_concern(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"].append({"document_type": "financial_statement", "symbol": "S1.TW",
                                                "source_evidence_id": "document:900", "available_at": self.window_date(),
                                                "financial_statement": {"industry": "ci"}})
        deps = [
            {"scope": "current", "value": "-10", "currency": "TWD", "source_evidence_id": "document:900"},
            {"scope": "prior_year", "value": "90", "currency": "TWD", "source_evidence_id": "document:901"},
        ]
        metrics = metrics_of(("S1.TW", [metric("S1.TW", "net_income_yoy_pct", "-111.1", "percent", deps=deps)]))
        flags = item(facts(bundle, metrics), "S1.TW")["fundamental_flags"]
        self.assertEqual([f["item"] for f in flags["new_adverse_facts"]], ["net_income_turned_loss"])
        self.assertNotIn("net_income_yoy_pct", [c["item"] for c in flags["concerns"]])
        # 同一份財報若在視窗前就可得：只是疑慮。
        bundle["snapshot"]["documents"][-1]["available_at"] = "2026-08-20T10:00:00+00:00"
        flags = item(facts(bundle, metrics), "S1.TW")["fundamental_flags"]
        self.assertEqual(flags["new_adverse_facts"], [])
        self.assertIn("net_income_yoy_pct", [c["item"] for c in flags["concerns"]])

    def test_negative_event_outlook_with_a_new_high_event_is_new_adverse(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"] += [
            {"document_type": "material_event", "symbol": "S0.TW", "source_evidence_id": "document:50", "available_at": self.window_date()},
            {"document_type": "material_event", "symbol": "S1.TW", "source_evidence_id": "document:51", "available_at": self.window_date()},
        ]
        events = event_report(**{"S0.TW": ("negative", [("document:50", "high")]),
                                 "S1.TW": ("positive", [("document:51", "high")])})
        result = facts(bundle, events=events)
        self.assertEqual(item(result, "S0.TW")["fundamental_flags"]["new_adverse_facts"],
                         [{"item": "high_event_negative_outlook", "evidence_ids": ["document:50"]}])
        self.assertEqual(item(result, "S0.TW")["candidate"], "none")
        self.assertEqual(item(result, "S1.TW")["fundamental_flags"]["new_adverse_facts"], [])
        self.assertEqual(item(result, "S0.TW")["fundamental_flags"]["event_check"], "applied")

    def test_analyst_view_has_no_labels_and_says_the_event_check_is_not_applied(self):
        view = analyst_dip_view(dip_bundle(), GATE, None)
        self.assertIs(view["background"]["citable"], False)
        for symbol in SYMBOLS:
            technical = view["technical"][symbol]
            self.assertEqual(set(technical), {"market_attribution", "price_position"})
            self.assertEqual(technical["market_attribution"]["evidence_ids"], ["price:%s" % symbol])
            self.assertEqual(view["fundamental"][symbol]["fundamental_flags"]["event_check"], "not_applied")
        text = json.dumps(view)
        for token in ("price_pattern", "candidate", "oversold", "market_dragged"):
            self.assertNotIn(token, text)


# ---------------------------------------------------------------- 4. 閘門、重算與不可引用
class GateAndValidationTests(unittest.TestCase):
    def test_gate_is_a_registered_human_only_field_without_prerequisites(self):
        self.assertIn("quality_dip_facts", GATE_FIELDS)
        self.assertIn("quality_dip_facts", POSITION_SIZING_FIELDS)
        self.assertIn("quality_dip_facts", FORBIDDEN_PREFIXES)
        errors = []
        validate_gate_prerequisites({"quality_dip_facts": GATE}, errors)
        self.assertEqual(errors, [])

    def test_shape_is_closed(self):
        for gate in ({"version": "v2"}, {"version": "v1", "window": 30}, "v1"):
            errors = []
            validate_quality_dip_facts_gate({"quality_dip_facts": gate}, errors)
            self.assertTrue(errors, gate)
        self.assertIsNone(quality_dip_facts_gate({}))
        self.assertIsNone(quality_dip_facts_gate(None))

    def test_bundle_without_fundamental_period_policy_is_rejected(self):
        bundle = dip_bundle()
        bundle.pop("fundamental_period_policy")
        with self.assertRaises(DecisionToolError):
            facts(bundle)

    def test_validate_rejects_tampering_missing_and_gateless_policy(self):
        bundle, events = dip_bundle(), event_report()
        good = build_dip_facts(bundle, GATE, None, events)
        policy = {"position_sizing": {"quality_dip_facts": GATE}}

        def check(team, pol=policy):
            errors = []
            validate_dip_facts(bundle, pol, team, errors)
            return errors

        team = {"dip_facts": good, "analyst_reports": {"event": events}, "fundamental_metrics": None}
        self.assertEqual(check(team), [])
        for label, mutate in {
            "殘差被改": lambda f: f["items"][0]["market_attribution"].update(residual_20d="0.100000"),
            "標示被改": lambda f: f["items"][1].update(candidate="oversold"),
            "多出欄位": lambda f: f.update(signal="buy"),
            "少一檔": lambda f: f.update(items=f["items"][1:]),
        }.items():
            tampered = copy.deepcopy(good)
            mutate(tampered)
            with self.subTest(label):
                self.assertTrue(check(dict(team, dip_facts=tampered)))
        self.assertTrue(check(dict(team, dip_facts=None)))
        self.assertTrue(check(team, {"position_sizing": {}}))
        self.assertTrue(check(dict(team, analyst_reports={})))  # 沒有事件報告無法重算

    def test_trader_brief_has_no_evidence_ids_and_tokens_are_rejected_in_text(self):
        result = facts()
        brief = dip_background_brief(result)
        self.assertIs(brief["background"]["citable"], False)
        self.assertNotIn("evidence_ids", json.dumps(brief))
        self.assertNotIn(result["facts_id"], json.dumps(brief))
        self.assertEqual(dip_reference_tokens(None), [])
        self.assertEqual(dip_reference_tokens(result), ["dip_background", "dip_facts", result["facts_id"]])


# ---------------------------------------------------------------- 5. 真實 Finalizer 整條鏈
def gated_world(directory):
    """test_exposure_rotation_e2e 的 2.7 整條鏈，另把 quality_dip_facts 閘門放進 position_sizing（配置前就在）。"""
    original = e2e.exposure_sizing

    def with_gate(*args, **kwargs):
        sizing = original(*args, **kwargs)
        sizing["quality_dip_facts"] = dict(GATE)
        return sizing

    with mock.patch.object(e2e, "exposure_sizing", with_gate):
        return e2e.ChainWorld(directory)


class FinalizerChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.world = gated_world(cls.directory.name)
        cls.plain = e2e.ChainWorld(cls.directory.name)
        cls.facts = build_dip_facts(cls.world.bundle, GATE, cls.world.metrics, cls.world.reports["event"])
        cls.team = cls.world.tampered_team(lambda team: team.update(dip_facts=cls.facts))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_gate_with_dip_facts_is_approved_and_version_is_unchanged(self):
        result = self.world.finalize(team=self.team)
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(self.team["schema_version"], "2.7")

    def test_missing_tampered_or_gateless_dip_facts_are_rejected(self):
        world = self.world
        missing = world.finalize(team=world.team)
        self.assertEqual(missing["status"], "rejected")
        self.assertTrue(any("quality_dip_facts" in error for error in missing["errors"]), missing["errors"])
        tampered = copy.deepcopy(self.team)
        tampered["dip_facts"]["items"][0]["market_attribution"]["return_20d"] = "0.123456"
        result = world.finalize(team=tampered)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("dip_facts 與重算結果不同" in error for error in result["errors"]), result["errors"])
        sneaked = self.plain.tampered_team(lambda team: team.update(dip_facts=self.facts))
        result = self.plain.finalize(team=sneaked)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("quality_dip_facts" in error for error in result["errors"]), result["errors"])

    def test_allocation_output_is_identical_with_and_without_the_gate(self):
        gated, plain = self.world.proposal, self.plain.proposal
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(gated[key], plain[key], key)
        engine = AllocationOrderEngine(self.world.bundle, self.world.policy).run(self.world.trade)
        self.assertEqual(engine["order_proposal"], plain["order_proposal"])

    def test_trader_text_citing_the_background_is_rejected_only_with_facts(self):
        world = self.world
        trade = copy.deepcopy(world.trade)
        trade["items"][0]["rationale"] = "依 dip_background 判斷。"
        from etf_agent.decision import artifact_content_sha256
        trade["content_sha256"] = artifact_content_sha256(trade)
        with_facts = TradeDecisionValidator(world.bundle, world.momentum, world.debate, hold_conviction=True,
                                            dip_facts=self.facts).validate(trade)
        self.assertTrue(any("不得引用 dip_background" in error for error in with_facts), with_facts)
        without = TradeDecisionValidator(world.bundle, world.momentum, world.debate, hold_conviction=True).validate(trade)
        self.assertFalse(any("dip_background" in error for error in without), without)
        trade["items"][0]["rationale"] = "依 %s。" % self.facts["facts_id"]
        trade["content_sha256"] = artifact_content_sha256(trade)
        errors = TradeDecisionValidator(world.bundle, world.momentum, world.debate, hold_conviction=True,
                                        dip_facts=self.facts).validate(trade)
        self.assertTrue(any("不得引用 dip_background" in error for error in errors), errors)


class ReplayTests(unittest.TestCase):
    def test_replay_with_the_gate_binds_and_allocates_like_without_it(self):
        helper = e2e.ReplayTests()
        helper.setUp()
        plain = helper.template(e2e.gateless_policy_template)
        gated = copy.deepcopy(plain)
        gated["position_sizing"]["quality_dip_facts"] = dict(GATE)
        plain_policy, plain_notes = helper.replay(plain)
        policy, notes = helper.replay(gated)
        self.assertEqual({k: v for k, v in notes.items() if k not in ("policy_id", "content_sha256")},
                         {k: v for k, v in plain_notes.items() if k not in ("policy_id", "content_sha256")})
        self.assertEqual(policy["position_sizing"]["quality_dip_facts"], GATE)
        gated_body = AllocationOrderEngine(helper.bundle, policy).run(helper.intent)
        plain_body = AllocationOrderEngine(helper.bundle, plain_policy).run(helper.intent)
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(gated_body[key], plain_body[key], key)


# ---------------------------------------------------------------- 6. 每日 pipeline 的簡報
def gated_template():
    from test_daily_pipeline import gateless_policy_template
    template = gateless_policy_template()
    template["position_sizing"]["quality_dip_facts"] = dict(GATE)
    return template


class PipelineBriefTests(unittest.TestCase):
    def run_world(self, template=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        world = PipelineWorld(Path(directory.name), approved_status=True, template=template)
        runner = FakeRunner(team_outputs())
        result = world.run(runner)
        self.assertEqual(result.status, "completed", result.errors)
        load = lambda name: json.loads((world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))  # noqa: E731
        return world, result, runner, load

    def test_gate_adds_facts_to_analysts_and_trader_but_not_researchers(self):
        world, result, runner, load = self.run_world(gated_template())
        self.assertEqual(result.decision_status, "approved")
        technical, fundamental = load("brief_technical_b0"), load("brief_fundamental_b0")
        self.assertIn("dip_facts_background", technical)
        self.assertTrue(all({"market_attribution", "price_position"} <= set(row) for row in technical["symbols"]))
        self.assertTrue(all("fundamental_flags" in row for row in fundamental["symbols"]))
        for brief in (technical, fundamental):
            self.assertNotIn("candidate", json.dumps(brief, ensure_ascii=False))
            self.assertNotIn("price_pattern", json.dumps(brief, ensure_ascii=False))
        for role in ("bull", "bear"):
            text = json.dumps(load("brief_%s_b0" % role), ensure_ascii=False)
            for token in ("dip_", "market_attribution", "fundamental_flags", "price_pattern"):
                self.assertNotIn(token, text, role)
        trader = load("brief_trader_b0")
        self.assertIs(trader["dip_background"]["citable"], False)
        self.assertTrue(all("dip" in row for row in trader["symbols"]))
        self.assertNotIn("evidence_ids", json.dumps(trader["dip_background"]))
        prompts = dict(runner.prompts)
        self.assertIn("dip_background", prompts["trader_b0"])
        self.assertIn("dip_facts_background", prompts["technical_b0"])
        self.assertNotIn("dip", prompts["bull_b0"])
        team = json.loads((DecisionRepository(world.repository).verify("decision-fixture") / "team_inputs.json").read_text(encoding="utf-8"))
        self.assertEqual(team["schema_version"], "2.6")
        self.assertEqual(team["dip_facts"], load("dip_facts"))
        self.assertIn("dip_facts*", LATER_STAGE_GLOBS)

    def test_without_gate_briefs_prompts_and_team_inputs_are_unchanged(self):
        world, _, runner, load = self.run_world()
        self.assertFalse((world.run_dir / "dip_facts.json").exists())
        for name in ("brief_technical_b0", "brief_fundamental_b0", "brief_trader_b0", "brief_bull_b0"):
            text = json.dumps(load(name), ensure_ascii=False)
            self.assertNotIn("dip_", text, name)
            self.assertNotIn("market_attribution", text, name)
        for _, prompt in runner.prompts:
            self.assertNotIn("dip_", prompt)
        team = json.loads((DecisionRepository(world.repository).verify("decision-fixture") / "team_inputs.json").read_text(encoding="utf-8"))
        self.assertNotIn("dip_facts", team)

    def test_gate_does_not_change_orders_or_portfolio(self):
        _, gated, _, gated_load = self.run_world(gated_template())
        _, plain, _, plain_load = self.run_world()
        self.assertEqual(gated.orders, plain.orders)
        self.assertEqual(gated_load("proposal_r0")["allocation_proposal"], plain_load("proposal_r0")["allocation_proposal"])


# ---------------------------------------------------------------- 7. 封存 run 的案例與回歸
@unittest.skipUnless((ARCHIVED / "team_inputs.json").is_file(), "封存 run 只在本機")
class ArchivedCaseTests(unittest.TestCase):
    """docs/quality_dip_plan.md 第 6.1 節：2026-11-10 封存 run 唯讀重算（replay_reference_only）。

    同一個測試行程裡其他模組（calibration.tools）會把全域 Decimal 精度改成 40；正式 pipeline 與 CLI 用預設的 28，
    所以這裡的重算一律在預設 Context 內執行，與封存當時相同。"""

    def run(self, result=None):
        with localcontext(Context()):
            return super().run(result)

    @classmethod
    def setUpClass(cls):
        with localcontext(Context()):
            cls._load()

    @classmethod
    def _load(cls):
        load = lambda name: json.loads((ARCHIVED / ("%s.json" % name)).read_text(encoding="utf-8"))  # noqa: E731
        cls.files = {name: load(name) for name in (
            "decision_input", "team_inputs", "policy", "momentum", "debate", "intent")}
        team = cls.files["team_inputs"]
        cls.facts = build_dip_facts(cls.files["decision_input"], GATE, team["fundamental_metrics"], team["analyst_reports"]["event"])

    def test_case_table(self):
        # (20 日報酬, 相對交易池, 視窗前 beta, 殘差, 名次, 候選)；計畫表的數字取到 0.1 pp，這裡容許 ±0.15 pp。
        expected = {
            "2327.TW": ("-0.3648", "-0.1804", "1.93", "0.005", 79, "market_dragged"),
            "2308.TW": ("-0.4171", "-0.2327", "1.24", "-0.180", 4, "oversold"),
            "2454.TW": ("-0.2296", "-0.0452", "1.46", "0.050", 105, "market_dragged"),
            "2317.TW": ("-0.0516", "0.1328", "0.80", "0.101", 133, "market_dragged"),
            "2382.TW": ("-0.1149", "0.0694", "0.75", "0.029", 93, "market_dragged"),
        }
        for symbol, (ret, rel, beta, residual, rank, candidate) in expected.items():
            row = item(self.facts, symbol)
            attribution = row["market_attribution"]
            with self.subTest(symbol):
                self.assertAlmostEqual(float(attribution["return_20d"]), float(ret), delta=0.0001)
                self.assertAlmostEqual(float(attribution["relative_to_pool_20d"]), float(rel), delta=0.0001)
                self.assertAlmostEqual(float(attribution["beta_pre_window"]), float(beta), delta=0.011)
                self.assertAlmostEqual(float(attribution["residual_20d"]), float(residual), delta=0.0015)
                self.assertEqual((attribution["residual_rank"], attribution["residual_sample"]), (rank, 149))
                self.assertEqual(row["candidate"], candidate)
                self.assertEqual(row["fundamental_flags"]["new_adverse_facts"], [])
        self.assertEqual(item(self.facts, "2308.TW")["cause"], "unknown")
        concerns = {c["item"] for c in item(self.facts, "2454.TW")["fundamental_flags"]["concerns"]}
        self.assertEqual(concerns, {"revenue_yoy_pct", "net_income_yoy_pct", "operating_margin_pp_yoy", "free_cash_flow"})
        self.assertEqual({c["item"] for c in item(self.facts, "2317.TW")["fundamental_flags"]["concerns"]},
                         {"operating_cash_flow", "free_cash_flow"})
        self.assertEqual({c["item"] for c in item(self.facts, "2382.TW")["fundamental_flags"]["concerns"]}, {"operating_margin_pp_yoy"})
        self.assertEqual({u["item"] for u in item(self.facts, "2308.TW")["fundamental_flags"]["unknown"]},
                         {"revenue_yoy_pct", "net_income_yoy_pct", "operating_margin_pp_yoy"})
        patterns = [row["price_pattern"] for row in self.facts["items"]]
        self.assertEqual((patterns.count("beyond_market"), patterns.count("market_explained"), patterns.count("no_decline")), (72, 56, 21))

    def test_without_the_gate_the_archived_team_chain_and_exposure_review_are_unchanged(self):
        files = self.files
        self.assertNotIn("dip_facts", files["team_inputs"])
        finalizer = DecisionFinalizer(files["decision_input"], files["policy"], files["momentum"], files["debate"],
                                      files["intent"], files["team_inputs"], DECISIONS)
        self.assertEqual(finalizer._validate_team_chain(), [])
        errors = []
        validate_exposure_review(files["decision_input"], files["policy"], files["team_inputs"]["exposure_review"], errors)
        self.assertEqual(errors, [])

    def test_with_the_gate_the_archived_chain_needs_and_rebuilds_dip_facts(self):
        files = self.files
        policy = copy.deepcopy(files["policy"])
        policy["position_sizing"]["quality_dip_facts"] = dict(GATE)
        team = dict(files["team_inputs"], dip_facts=self.facts)

        def chain(team_inputs):
            return DecisionFinalizer(files["decision_input"], policy, files["momentum"], files["debate"], files["intent"],
                                     team_inputs, DECISIONS)._validate_team_chain()

        self.assertEqual(chain(team), [])
        self.assertTrue(any("quality_dip_facts" in error for error in chain(files["team_inputs"])))
        tampered = copy.deepcopy(team)
        tampered["dip_facts"]["items"][0]["candidate"] = "oversold"
        self.assertTrue(any("dip_facts 與重算結果不同" in error for error in chain(tampered)))


if __name__ == "__main__":
    unittest.main()
