"""position_sizing.cash_stance_brief v2（現金姿態簡報縮減，已實作、尚未開啟）：計畫 3.1 與 3.5。

- 沒有閘門：簡報的鍵與順序、提示文字與舊版逐位元相同（鍵順序以固定清單鎖住）。
- 有閘門：evidence_recency 維持第一、必讀欄位在前、公司事件摘要在最後（只列 high 事件）；計數加總等於交易池檔數。
- offense_reference=omit：簡報的 offense_facts 不附 reference、提示文字拿掉「與一組對照樣本」；team_inputs 的 offense_facts 照舊含 reference。
- 閘門形狀錯、列舉值錯、多餘欄位、沒有 offense 卻填 offense_reference 一律整份 Policy 拒絕；列入反思禁區。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation import risk_stages
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, MomentumEngine
from etf_agent.decision.cash_stance_brief import (
    BRIEF_KEY_ORDER, cash_stance_brief_gate, company_events_summary, offense_reference_omitted, validate_cash_stance_brief_gate,
)
from etf_agent.decision.offense import offense_brief
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_position_sizing
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_cash_stance_turn_facts import overnight_bundle
from test_daily_pipeline import FakeRunner
from test_offense import OFFENSE, live, with_offense
from test_typed_evidence import typed_reports

ROOT = Path(__file__).resolve().parents[2]
GATE = {"version": "v2", "company_events": "high_only"}
GATE_OMIT = {**GATE, "offense_reference": "omit"}
FACTS = {
    "as_of": "2026-11-10", "data_status": "available", "categories": {}, "data_gaps": [],
    "reference": {"evidence_grade": "replay_reference_only", "event_dates": ["2026-11-02"]},
}


def brief_errors(sizing):
    errors = []
    validate_cash_stance_brief_gate(sizing, errors)
    return errors


def policy_errors(policy):
    errors = []
    validate_position_sizing(policy, ["2330.TW"], errors)
    return [error for error in errors if "cash_stance_brief" in error]


class GateTests(unittest.TestCase):
    def test_registration_and_forbidden_zone(self):
        self.assertIn("cash_stance_brief", GATE_FIELDS)
        self.assertIn("cash_stance_brief", POSITION_SIZING_FIELDS)
        self.assertTrue(any("cash_stance_brief".startswith(prefix) for prefix in FORBIDDEN_PREFIXES))

    def test_live_policy_enables_the_gate(self):
        # 2026-11-12 16:40 使用者決定開啟；回滾樣板 config/policy_versions/2026-11-12-before-cash-stance-brief.json
        self.assertEqual(live()["position_sizing"]["cash_stance_brief"], GATE_OMIT)

    def test_valid_shapes_pass(self):
        self.assertEqual(brief_errors({"cash_stance_brief": GATE}), [])
        self.assertEqual(brief_errors({"cash_stance_brief": GATE_OMIT, "offense": {}}), [])
        policy = with_offense()
        policy["position_sizing"]["cash_stance_brief"] = dict(GATE_OMIT)
        self.assertEqual(policy_errors(policy), [])
        policy = live()
        policy["position_sizing"]["cash_stance_brief"] = dict(GATE_OMIT)  # 現行正式設定已有 offense
        self.assertEqual(policy_errors(policy), [])

    def test_fail_closed_branches(self):
        bad = (
            {"version": "v1", "company_events": "high_only"},
            {"version": "v3", "company_events": "high_only"},
            {"company_events": "high_only"},
            {"version": "v2"},
            {"version": "v2", "company_events": "all"},
            {"version": "v2", "company_events": "high_only", "offense_reference": "keep"},
            {"version": "v2", "company_events": "high_only", "extra": 1},
            "v2",
        )
        for gate in bad:
            self.assertTrue(brief_errors({"cash_stance_brief": gate, "offense": {}}), gate)
            policy = with_offense()
            policy["position_sizing"]["cash_stance_brief"] = gate
            self.assertTrue(policy_errors(policy), gate)

    def test_offense_reference_needs_offense(self):
        errors = brief_errors({"cash_stance_brief": GATE_OMIT})
        self.assertTrue(any("需要 position_sizing.offense" in error for error in errors))
        policy = live()
        policy["position_sizing"].pop("offense", None)
        policy["position_sizing"].pop("cash_stance_brief", None)  # 前提閘門被拿掉時，依賴它們的簡報閘門一併拿掉
        policy["position_sizing"]["cash_stance_brief"] = dict(GATE_OMIT)
        self.assertTrue(policy_errors(policy))

    def test_accessors(self):
        self.assertIsNone(cash_stance_brief_gate({}))
        self.assertIsNone(cash_stance_brief_gate(None))
        self.assertEqual(cash_stance_brief_gate({"cash_stance_brief": GATE}), GATE)
        self.assertFalse(offense_reference_omitted(None))
        self.assertFalse(offense_reference_omitted(GATE))
        self.assertTrue(offense_reference_omitted(GATE_OMIT))


class SummaryTests(unittest.TestCase):
    def items(self):
        return [
            {"symbol": "A.TW", "outlook": "positive", "events": [
                {"evidence_id": "ev-a1", "materiality": "high", "summary": "重大訊息", "direction": "positive"},
                {"evidence_id": "ev-a2", "materiality": "low", "summary": "例行", "direction": "neutral"}]},
            {"symbol": "B.TW", "outlook": "unknown", "events": []},
            {"symbol": "C.TW", "outlook": "negative", "events": [
                {"evidence_id": "ev-c1", "materiality": "medium", "summary": "中等"},
                {"evidence_id": "ev-c2", "materiality": "high", "summary": "利空", "direction": "negative"}]},
        ]

    def test_counts_high_only_and_omitted(self):
        summary = company_events_summary(self.items())
        self.assertEqual(summary["symbols_total"], 3)
        self.assertEqual(sum(summary["event_outlook_counts"].values()), 3)
        self.assertEqual(summary["event_outlook_counts"], {"negative": 1, "positive": 1, "unknown": 1})
        self.assertEqual([row["evidence_id"] for row in summary["high_events"]], ["ev-a1", "ev-c2"])
        self.assertEqual(summary["high_events"][0]["evidence_ids"], ["ev-a1"])
        self.assertEqual(summary["omitted"], {"non_high_events": 2})
        total = sum(sum(row.values()) for row in summary["materiality_by_direction"].values())
        self.assertEqual(total, 4)
        self.assertEqual(summary["materiality_by_direction"]["medium"], {"not_provided": 1})
        self.assertNotIn("findings", json.dumps(summary))

    def test_empty_report(self):
        summary = company_events_summary([])
        self.assertEqual(summary["high_events"], [])
        self.assertEqual(summary["symbols_total"], 0)


class OffenseBriefTests(unittest.TestCase):
    def test_reference_is_kept_by_default_and_dropped_on_request(self):
        full = offense_brief(FACTS, OFFENSE)
        self.assertEqual(full["reference"], FACTS["reference"])
        self.assertEqual(offense_brief(FACTS, OFFENSE, include_reference=True), full)
        omitted = offense_brief(FACTS, OFFENSE, include_reference=False)
        self.assertNotIn("reference", omitted)
        self.assertEqual({k: v for k, v in full.items() if k != "reference"}, omitted)


class PromptTests(unittest.TestCase):
    def test_reference_phrase_is_removed_only_by_replace(self):
        self.assertIn("與一組對照樣本", risk_stages.OFFENSE_VIEW_PROMPT)
        replaced = risk_stages.OFFENSE_VIEW_PROMPT.replace(risk_stages.REFERENCE_PHRASE, risk_stages.REFERENCE_PHRASE_OMITTED)
        self.assertNotIn("對照樣本", replaced)


class BriefTests(unittest.TestCase):
    def run_stance(self, gate, **kwargs):
        bundle = overnight_bundle()
        momentum = MomentumEngine(bundle).run()
        runner = FakeRunner({"cash_stance": [{"level": "neutral", "breadth": "balanced", "rationale": "依市場事實。",
                                              "evidence_ids": [MARKET_REGIME_EVIDENCE_ID]}] * 3})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            try:
                pipeline.run_cash_stance(bundle, momentum, typed_reports(bundle), **({"cash_stance_brief_gate": gate} if gate else {}), **kwargs)
            except Exception:  # 簡報在呼叫 Agent 之前已封存；驗證結果不是這裡要測的
                pass
            brief = json.loads((Path(directory) / "brief_cash_stance.json").read_text(encoding="utf-8"))
        return brief, runner

    def test_without_gate_brief_keys_order_and_prompt_are_the_legacy_ones(self):
        brief, runner = self.run_stance(None)
        self.assertEqual(list(brief), [
            "regime_assessment", "analyst_outlooks", "tradability", "company_events", "account", "rules",
            "market_sentiment", "drawdown_control", "positions_by_breadth", "cash_buffer_by_stance", "cash_ratio_now", "citable_evidence_ids",
            "market_level_evidence_ids",
        ])
        self.assertIsInstance(brief["company_events"], list)
        self.assertNotIn("公司事件已縮成摘要", runner.prompts[0][1])

    def test_with_gate_order_summary_and_prompt(self):
        brief, runner = self.run_stance(GATE)
        keys = list(brief)
        self.assertEqual(keys[-1], "company_events")
        self.assertLess(keys.index("market_level_evidence_ids"), keys.index("regime_assessment"))
        self.assertLess(keys.index("citable_evidence_ids"), keys.index("account"))
        positions = [BRIEF_KEY_ORDER.index(key) for key in keys if key in BRIEF_KEY_ORDER]
        self.assertEqual(positions, sorted(positions))
        events = brief["company_events"]
        self.assertEqual(events["symbols_total"], 2)
        self.assertEqual(sum(events["event_outlook_counts"].values()), 2)
        self.assertEqual([row["symbol"] for row in events["high_events"]], ["2330.TW"])
        # 列出的 evidence_ids 都存在於決策輸入
        bundle = overnight_bundle()
        known = json.dumps(bundle)
        for row in events["high_events"]:
            for evidence_id in row["evidence_ids"]:
                self.assertIn(evidence_id, known)
        self.assertIn("公司事件已縮成摘要", runner.prompts[0][1])

    def test_reference_omit_drops_reference_from_brief_and_prompt_only(self):
        extra = {
            "exposure_view_gate": {"version": "v2"},
            "exposure_facts": {"as_of": "2026-11-10", "portfolio": {}, "sectors": {}, "up_day": {}, "data_gaps": []},
            "offense_gate": OFFENSE, "offense_facts": FACTS, "cash_stance_turn_facts": {"version": "v1"},
        }
        kept, kept_runner = self.run_stance(GATE, **extra)
        omitted, omitted_runner = self.run_stance(GATE_OMIT, **extra)
        self.assertEqual(kept["offense_facts"]["reference"], FACTS["reference"])
        self.assertNotIn("reference", omitted["offense_facts"])
        self.assertIn("與一組對照樣本", kept_runner.prompts[0][1])
        self.assertNotIn("對照樣本", omitted_runner.prompts[0][1])
        self.assertEqual(FACTS["reference"]["event_dates"], ["2026-11-02"])  # 封存的事實不被簡報副本改動
        omitted["offense_facts"]["reference"] = kept["offense_facts"]["reference"]
        self.assertEqual(omitted["offense_facts"], kept["offense_facts"])

    def test_gate_with_reference_omit_needs_offense_gate(self):
        bundle = overnight_bundle()
        momentum = MomentumEngine(bundle).run()
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), FakeRunner({}), Path(directory) / "repo", log=lambda _: None)
            with self.assertRaises(Exception) as caught:
                pipeline.run_cash_stance(bundle, momentum, typed_reports(bundle), cash_stance_brief_gate=GATE_OMIT)
        self.assertIn("offense", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
