"""position_sizing.event_direction v1（優質股階段 2：事件分析師每則事件的 direction 列舉）的測試。

覆蓋：閘門形狀與註冊、事件報告 Validator（沒有 direction 的舊報告驗證不變；有就必須是列舉；閘門啟用時必填）、
Schema 只在閘門開啟時才多這個欄位、新壞事事件面改看每則事件（high 且 negative 才算；缺 direction 列不知道、不當成通過）、
舊判準（整檔 event_outlook）在沒有閘門時逐位元不變，以及 validate_dip_facts 依 Policy 重算並要求每則事件都有 direction。
"""

import copy
import unittest

from etf_agent.automation.agent_schemas import event_analyst_schema
from etf_agent.decision.dip_facts import (
    EVENT_DIRECTIONS,
    build_dip_facts,
    event_direction_gate,
    validate_dip_facts,
    validate_event_direction_gate,
    validate_event_directions,
)
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_gate_prerequisites
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_quality_dip_facts import GATE, SYMBOLS, dip_bundle, event_report, item, weekdays, TRADE_DATE


def window_date(offset=0):
    return "%sT10:00:00+00:00" % weekdays(TRADE_DATE, 100)[-10 + offset]


def bundle_with_events():
    bundle = dip_bundle()
    bundle["snapshot"]["documents"] += [
        {"document_type": "material_event", "symbol": "S0.TW", "source_evidence_id": "document:50", "available_at": window_date()},
        {"document_type": "material_event", "symbol": "S1.TW", "source_evidence_id": "document:51", "available_at": window_date()},
    ]
    return bundle


def directed(**by_symbol):
    """{symbol: (outlook, [(evidence_id, materiality, direction), ...])}。"""
    report = event_report(**{symbol: (outlook, [(e, m) for e, m, _ in events]) for symbol, (outlook, events) in by_symbol.items()})
    for row in report["items"]:
        for event, (_, _, direction) in zip(row["events"], by_symbol.get(row["symbol"], ("", []))[1]):
            if direction is not None:
                event["direction"] = direction
    return report


class GateTests(unittest.TestCase):
    def test_gate_is_registered_human_only_and_requires_quality_dip_facts(self):
        self.assertIn("event_direction", GATE_FIELDS)
        self.assertIn("event_direction", POSITION_SIZING_FIELDS)
        self.assertIn("event_direction", FORBIDDEN_PREFIXES)
        errors = []
        validate_gate_prerequisites({"event_direction": GATE}, errors)
        self.assertTrue(any("quality_dip_facts" in error for error in errors), errors)
        errors = []
        validate_gate_prerequisites({"event_direction": GATE, "quality_dip_facts": GATE}, errors)
        self.assertEqual(errors, [])

    def test_shape_is_closed(self):
        for gate in ({"version": "v2"}, {"version": "v1", "x": 1}, "v1"):
            errors = []
            validate_event_direction_gate({"event_direction": gate}, errors)
            self.assertTrue(errors, gate)
        self.assertIsNone(event_direction_gate({}))
        self.assertEqual(event_direction_gate({"event_direction": GATE}), GATE)

    def test_schema_only_has_direction_when_the_gate_is_on(self):
        def event_props(schema):
            return schema["properties"]["items"]["items"]["properties"]["events"]["items"]
        plain, gated = event_props(event_analyst_schema(False)), event_props(event_analyst_schema(False, True))
        self.assertNotIn("direction", plain["properties"])
        self.assertEqual(gated["properties"]["direction"]["enum"], list(EVENT_DIRECTIONS))
        self.assertIn("direction", gated["required"])


class FlagTests(unittest.TestCase):
    def build(self, events, direction):
        return build_dip_facts(bundle_with_events(), GATE, None, events, direction)

    def test_without_the_gate_the_old_outlook_rule_is_unchanged(self):
        events = directed(**{"S0.TW": ("negative", [("document:50", "high", None)])})
        flags = item(self.build(events, False), "S0.TW")["fundamental_flags"]
        self.assertEqual(flags["new_adverse_facts"], [{"item": "high_event_negative_outlook", "evidence_ids": ["document:50"]}])

    def test_direction_rule_ignores_the_whole_stock_outlook(self):
        events = directed(**{
            "S0.TW": ("positive", [("document:50", "high", "negative")]),   # 整檔 positive，但這則事件負面
            "S1.TW": ("negative", [("document:51", "high", "positive")]),   # 整檔 negative，但這則事件正面
        })
        result = self.build(events, True)
        self.assertEqual(item(result, "S0.TW")["fundamental_flags"]["new_adverse_facts"],
                         [{"item": "high_event_negative_direction", "evidence_ids": ["document:50"]}])
        self.assertEqual(item(result, "S0.TW")["candidate"], "none")
        self.assertEqual(item(result, "S1.TW")["fundamental_flags"]["new_adverse_facts"], [])

    def test_only_high_materiality_negative_events_count(self):
        events = directed(**{"S0.TW": ("neutral", [("document:50", "medium", "negative")])})
        self.assertEqual(item(self.build(events, True), "S0.TW")["fundamental_flags"]["new_adverse_facts"], [])

    def test_a_high_event_without_direction_is_unknown_not_adverse(self):
        events = directed(**{"S0.TW": ("negative", [("document:50", "high", None)])})
        flags = item(self.build(events, True), "S0.TW")["fundamental_flags"]
        self.assertEqual(flags["new_adverse_facts"], [])
        self.assertIn({"item": "high_event_negative_direction", "reason_code": "EVENT_DIRECTION_MISSING",
                       "evidence_ids": ["document:50"]}, flags["unknown"])

    def test_events_available_before_the_window_do_not_count(self):
        bundle = dip_bundle()
        bundle["snapshot"]["documents"].append(
            {"document_type": "material_event", "symbol": "S0.TW", "source_evidence_id": "document:50",
             "available_at": "%sT10:00:00+00:00" % weekdays(TRADE_DATE, 100)[-60]})
        events = directed(**{"S0.TW": ("neutral", [("document:50", "high", "negative")])})
        result = build_dip_facts(bundle, GATE, None, events, True)
        self.assertEqual(item(result, "S0.TW")["fundamental_flags"]["new_adverse_facts"], [])

    def test_missing_report_item_is_unknown(self):
        report = directed()
        report["items"] = [row for row in report["items"] if row["symbol"] != "S0.TW"]
        flags = item(self.build(report, True), "S0.TW")["fundamental_flags"]
        self.assertIn({"item": "high_event_negative_direction", "reason_code": "NO_EVENT_REPORT_ITEM"}, flags["unknown"])


class ValidateTests(unittest.TestCase):
    def check(self, team, policy):
        errors = []
        validate_dip_facts(bundle_with_events(), policy, team, errors)
        return errors

    def test_rebuild_follows_the_policy_gate_and_requires_direction_on_every_event(self):
        events = directed(**{"S0.TW": ("negative", [("document:50", "high", "negative")])})
        policy = {"position_sizing": {"quality_dip_facts": GATE, "event_direction": GATE}}
        facts = build_dip_facts(bundle_with_events(), GATE, None, events, True)
        team = {"dip_facts": facts, "analyst_reports": {"event": events}, "fundamental_metrics": None}
        self.assertEqual(self.check(team, policy), [])
        # 沒有閘門的 Policy 以舊判準重算，不相符即拒絕。
        self.assertTrue(self.check(team, {"position_sizing": {"quality_dip_facts": GATE}}))
        # 閘門開啟但報告有事件缺 direction：拒絕。
        missing = directed(**{"S0.TW": ("negative", [("document:50", "high", None)])})
        facts = build_dip_facts(bundle_with_events(), GATE, None, missing, True)
        errors = self.check({"dip_facts": facts, "analyst_reports": {"event": missing}, "fundamental_metrics": None}, policy)
        self.assertTrue(any("必須有 direction" in error for error in errors), errors)
        tampered = copy.deepcopy(team)
        tampered["dip_facts"]["items"][0]["candidate"] = "oversold"
        self.assertTrue(self.check(tampered, policy))

    def test_validate_event_directions_rejects_invalid_values(self):
        report = directed(**{"S0.TW": ("negative", [("document:50", "high", "bearish")])})
        errors = []
        validate_event_directions(report, errors)
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
