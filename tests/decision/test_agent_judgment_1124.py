"""2026-11-24 11:30：姿態判斷交還風險 Agent（night_rally_override v4、offense.agent_view_turn）。

- v4：全市場情緒 negative 不再擋 aggressive，夜盤也不強迫；v1～v3 與沒有閘門時護欄不變。
- agent_view_turn：exposure_view 與 turn_assessment 的一致性不再檢查；沒有欄位時照舊退回。
"""

import copy
import json
import unittest
from pathlib import Path

from etf_agent.automation import risk_stages
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.night_rally import night_rally_brief, sentiment_guard_lifted, validate_night_rally_override_gate
from etf_agent.decision.offense import agent_view_turn_enabled, validate_offense_gate, validate_offense_stance
from etf_agent.decision.sizing import validate_cash_stance

from test_cash_stance_guard import STANCE, negative_sentiment_bundle

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": "v4", "min_night_change_pct": "5"}


class SentimentGuardTests(unittest.TestCase):
    def setUp(self):
        self.bundle = negative_sentiment_bundle()
        self.assertEqual(DecisionContext(self.bundle).market_sentiment["outlook"], "negative")

    def errors(self, stance, gate):
        errors = []
        validate_cash_stance(DecisionContext(self.bundle), stance, "cash_stance", errors, night_rally_gate=gate)
        return errors

    def test_v4_lets_aggressive_pass_under_negative_sentiment(self):
        self.assertEqual(self.errors(dict(STANCE, level="aggressive"), GATE), [])

    def test_older_versions_and_no_gate_keep_the_guard(self):
        aggressive = dict(STANCE, level="aggressive")
        for gate in (None, dict(GATE, version="v1"), dict(GATE, version="v3")):
            self.assertTrue(any("一致性護欄" in error for error in self.errors(aggressive, gate)), gate)

    def test_v4_still_requires_market_level_evidence(self):
        price_only = dict(STANCE, level="aggressive", evidence_ids=["price-2330"])
        self.assertTrue(any("市場層級證據" in error for error in self.errors(price_only, GATE)))

    def test_v4_never_forces_and_the_brief_says_so(self):
        self.assertTrue(sentiment_guard_lifted(GATE))
        self.assertFalse(sentiment_guard_lifted(dict(GATE, version="v1")))
        self.assertFalse(sentiment_guard_lifted(None))
        brief = night_rally_brief(GATE, self.bundle)
        self.assertEqual(brief["version"], "v4")
        self.assertNotIn("aggressive_required", brief)
        self.assertIn("完全由你看行情判斷", brief["rule"])

    def test_gate_shape(self):
        errors = []
        validate_night_rally_override_gate({"night_rally_override": GATE}, errors)
        self.assertEqual(errors, [])


class ViewTurnTests(unittest.TestCase):
    FACTS = {"facts_id": "offense-facts:abc", "categories": {
        "washout": {"status": "available"}, "breadth_thrust": {"status": "available"}, "volume_confirmation": {"status": "unavailable"},
        "sentiment_extreme": {"status": "unavailable"}, "index_reclaim": {"status": "unavailable"}}}

    def run_stance(self, view_turn_match, **fields):
        stance = {"level": "aggressive", "exposure_view": "rebuild", "turn_assessment": "no_turn_evidence", "rationale": "x",
                  "evidence_ids": ["mkt-a", "reg"]}
        stance.update(fields)
        errors = []
        validate_offense_stance(stance, stance["evidence_ids"], {"mkt-a", "reg"}, {"reg"}, self.FACTS, ["offense_facts"],
                                "cash_stance", errors, view_turn_match=view_turn_match)
        return errors

    def test_mismatch_is_refused_by_default_and_accepted_when_the_agent_decides(self):
        self.assertTrue(any("不一致" in error for error in self.run_stance(True)))
        self.assertEqual(self.run_stance(False), [])

    def test_other_offense_checks_stay(self):
        self.assertTrue(self.run_stance(False, evidence_ids=["reg"]))  # 進攻仍要兩個市場層級證據
        self.assertTrue(self.run_stance(False, rationale="依 offense_facts 判斷"))  # 背景鍵名仍不得寫出

    def test_the_gate_field_is_version_only(self):
        self.assertTrue(agent_view_turn_enabled({"agent_view_turn": {"version": "v1"}}))
        self.assertFalse(agent_view_turn_enabled({}))
        self.assertFalse(agent_view_turn_enabled(None))
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        sizing = live["position_sizing"]
        errors = []
        validate_offense_gate(sizing, errors)
        self.assertEqual(errors, [])
        bad = copy.deepcopy(sizing)
        bad["offense"]["agent_view_turn"] = {"version": "v1", "x": "1"}
        errors = []
        validate_offense_gate(bad, errors)
        self.assertTrue(any("agent_view_turn" in error for error in errors))

    def test_prompts_drop_the_consistency_rule_only_with_the_field(self):
        gate = {"agent_view_turn": {"version": "v1"}}
        for prompt, match, free in (
            (risk_stages.TURN_ASSESSMENT_PROMPT_OFFENSE, risk_stages.VIEW_TURN_MATCH_SENTENCE, risk_stages.VIEW_TURN_FREE_SENTENCE),
            (risk_stages.OFFENSE_VIEW_PROMPT, risk_stages.OFFENSE_VIEW_MATCH_SENTENCE, risk_stages.OFFENSE_VIEW_FREE_SENTENCE),
        ):
            self.assertEqual(risk_stages.free_view_turn(prompt, match, free, {}), prompt)
            changed = risk_stages.free_view_turn(prompt, match, free, gate)
            self.assertNotIn(match, changed)
            self.assertIn(free, changed)


class LiveConfigTests(unittest.TestCase):
    def test_the_live_policy_hands_the_stance_back_to_the_agent(self):
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        sizing = live["position_sizing"]
        self.assertEqual(live["policy_id"], "strategy-2026-11-26-cooldown-v4-beta-1")
        self.assertEqual(sizing["night_rally_override"]["version"], "v4")
        self.assertTrue(agent_view_turn_enabled(sizing["offense"]))
        self.assertEqual(sizing["agent_swap"]["min_holding_days"], 2)  # 2026-11-26 由 0 改回 2
        self.assertNotIn("victim_bear_claim", sizing["agent_swap"])


if __name__ == "__main__":
    unittest.main()
