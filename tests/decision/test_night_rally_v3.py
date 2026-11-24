"""night_rally_override v3 與夜盤相關修正的測試（機制測試，合成資料）。

- 美國感恩節（美股休市）：隔夜背景整體 unavailable、但夜盤成分可用時，風險簡報的通道仍附夜盤成分。
- v3 回撤下限優先：下限高於 aggressive 時不要求 aggressive；v2 逐位元維持舊行為（仍要求）。
- v3 要求 aggressive 時 exposure_view 不得是 keep_defensive；v2 不擋。
- 純紀錄輸入遇到晚於最近交易日的資料：截掉並記缺口，不讓整個 run 失敗。
"""

import copy
import unittest
from datetime import datetime, timedelta

from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.market_scope import overnight_market_channel
from etf_agent.decision.night_rally import (
    night_rally_brief, night_rally_override_gate, night_rally_required_evidence, validate_night_rally_requirement,
)
from etf_agent.decision.recency import evidence_recency
from etf_agent.perception.overnight_market import attach_overnight_market, evaluate
from etf_agent.perception.overnight_market.contracts import PACK_VERSION_V13, seal

from test_analysts import bundle_with_event
from test_overnight_market import NIGHT_ROWS, approvals, night_capture, with_trade_date

SIZING = {"night_rally_override": {"version": "v3", "min_night_change_pct": "5"}}


def thanksgiving_bundle():
    """美股休市：沒有任何美股／日韓抓取成功，只有週五夜盤 +9%；整體 unavailable、夜盤成分 available。"""
    base = with_trade_date(bundle_with_event())
    cutoff = datetime.fromisoformat(base["decision_cutoff"])
    available = (cutoff - timedelta(minutes=5)).isoformat()
    row = dict(NIGHT_ROWS[0], CDate="20260918", CTime="045959", CLastPrice="50140.00", CRefPrice="46000.00")
    value = seal({"schema_version": PACK_VERSION_V13, "snapshot_id": base["snapshot_id"], "decision_cutoff": base["decision_cutoff"],
                  "latest_trade_date": "2026-09-18", "captures": [night_capture([row], available=available)],
                  "source_approvals": approvals()})
    return attach_overnight_market(base, {"bundle": value, "result": evaluate(value)})


class ThanksgivingTests(unittest.TestCase):
    def setUp(self):
        self.bundle = thanksgiving_bundle()
        self.result = self.bundle["overnight_market_input"]["result"]
        self.night = [row for row in self.result["components"] if row["symbol"] == "TXF_NIGHT"][0]

    def test_the_fixture_is_overall_unavailable_with_an_available_night(self):
        self.assertEqual(self.result["status"], "unavailable")
        self.assertEqual(self.night["status"], "available")
        self.assertEqual(self.night["change_pct"], "9")

    def test_the_default_channel_stays_empty_like_before(self):
        channel = overnight_market_channel(self.bundle)
        self.assertEqual(channel["components"], [])
        self.assertNotIn("night_only", channel)

    def test_the_gated_channel_shows_the_required_night_evidence_and_it_is_citable(self):
        channel = overnight_market_channel(self.bundle, keep_available_night=True)
        self.assertEqual(channel["status"], "unavailable")
        self.assertTrue(channel["night_only"])
        self.assertEqual([row["symbol"] for row in channel["components"]], ["TXF_NIGHT"])
        gate = night_rally_override_gate({"night_rally_override": {"version": "v2", "min_night_change_pct": "5"}})
        required = night_rally_required_evidence(gate, self.bundle)
        self.assertEqual(required, channel["components"][0]["evidence_id"])
        context = DecisionContext(self.bundle)
        self.assertIn(required, context.market_evidence_ids)  # 註冊為市場層級證據，風險 Agent 可引用

    def test_the_gated_channel_is_unchanged_when_the_whole_overnight_is_available(self):
        from test_cash_stance_recency import overnight_bundle
        bundle = overnight_bundle()
        self.assertEqual(overnight_market_channel(bundle), overnight_market_channel(bundle, keep_available_night=True))


class DrawdownFloorPriorityTests(unittest.TestCase):
    def setUp(self):
        self.bundle = thanksgiving_bundle()

    def gate(self, version, floor):
        sizing = {"night_rally_override": {"version": version, "min_night_change_pct": "5"}}
        return night_rally_override_gate(sizing, {"stance_floor": floor})

    def test_v3_with_a_floor_above_aggressive_does_not_require_aggressive(self):
        for floor in ("neutral", "defensive"):
            gate = self.gate("v3", floor)
            self.assertIsNone(night_rally_required_evidence(gate, self.bundle), floor)
            errors = []
            validate_night_rally_requirement(gate, self.bundle, {"level": floor, "evidence_ids": []}, "cash_stance", errors)
            self.assertEqual(errors, [])
            self.assertIn("回撤下限", night_rally_brief(gate, self.bundle)["drawdown_floor_note"])
            self.assertFalse(night_rally_brief(gate, self.bundle)["aggressive_required"])

    def test_v3_without_a_floor_still_requires_aggressive(self):
        for floor in (None, "none"):
            gate = self.gate("v3", floor) if floor is None else night_rally_override_gate(SIZING, {})
            self.assertIsNotNone(night_rally_required_evidence(gate, self.bundle))

    def test_v2_keeps_requiring_aggressive_whatever_the_floor(self):
        gate = self.gate("v2", "defensive")
        self.assertNotIn("drawdown_floor", gate)
        self.assertIsNotNone(night_rally_required_evidence(gate, self.bundle))

    def test_the_gate_without_a_control_is_the_original_object(self):
        sizing = {"night_rally_override": {"version": "v3", "min_night_change_pct": "5"}}
        self.assertIs(night_rally_override_gate(sizing), sizing["night_rally_override"])


class ExposureViewTests(unittest.TestCase):
    def setUp(self):
        self.bundle = thanksgiving_bundle()
        self.night = [row for row in self.bundle["overnight_market_input"]["result"]["components"]
                      if row["symbol"] == "TXF_NIGHT"][0]["evidence_id"]

    def check(self, version, view):
        gate = {"version": version, "min_night_change_pct": "5"}
        stance = {"level": "aggressive", "evidence_ids": [self.night, MARKET_REGIME_EVIDENCE_ID], "exposure_view": view}
        errors = []
        validate_night_rally_requirement(gate, self.bundle, stance, "cash_stance", errors)
        return errors

    def test_v3_refuses_keep_defensive_when_aggressive_is_required(self):
        errors = self.check("v3", "keep_defensive")
        self.assertEqual(len(errors), 1)
        self.assertIn("exposure_view 不得是 keep_defensive", errors[0])

    def test_v3_accepts_other_views_and_v2_does_not_check_the_view(self):
        self.assertEqual(self.check("v3", "start_rebuild"), [])
        self.assertEqual(self.check("v2", "keep_defensive"), [])


class LateDataIsAGapTests(unittest.TestCase):
    def test_a_fear_greed_component_after_the_latest_trade_date_is_a_gap_not_an_error(self):
        bundle = copy.deepcopy(thanksgiving_bundle())
        bundle["market_fear_greed_input"] = {"result": {"components": [{"evidence_id": "fg:late", "as_of": "2099-01-01"}]}}
        recency = evidence_recency(bundle, {"fg:late"})
        self.assertTrue(any("fg:late" in gap and "晚於台股最近交易日" in gap for gap in recency["data_gaps"]), recency["data_gaps"])


if __name__ == "__main__":
    unittest.main()
