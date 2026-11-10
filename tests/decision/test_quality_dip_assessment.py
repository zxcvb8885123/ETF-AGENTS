"""position_sizing.quality_dip_assessment v1（優質股階段 2：交易 Agent 的 dip_assessment 列舉，紀錄用）的測試。

覆蓋：閘門形狀與註冊（前提 quality_dip_facts、人專用）、欄位白名單（沒有閘門出現即拒絕）、buy／add 必填與值域、
非 buy／add 只能為 null、價格事實 unavailable 時只能 undetermined、缺 dip_facts／缺 typed_evidence_v1 即拒絕、
列舉與意圖不設對應、真實 Finalizer 重建驗證，以及 Schema 只在閘門開啟時才多這個欄位。
"""

import copy
import tempfile
import unittest

from etf_agent.automation.agent_schemas import TRADE_SCHEMA, trade_schema_with_dip_assessment
from etf_agent.decision import TradeDecisionValidator, artifact_content_sha256
from etf_agent.decision.dip_facts import (
    DIP_ASSESSMENTS,
    build_dip_facts,
    quality_dip_assessment_gate,
    validate_dip_assessment,
    validate_quality_dip_assessment_gate,
)
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_gate_prerequisites
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

import test_exposure_rotation_e2e as e2e
from test_quality_dip_facts import GATE, dip_bundle, event_report, gated_world

SEALED = "content_sha256"


def reseal(trade):
    trade = copy.deepcopy(trade)
    trade[SEALED] = artifact_content_sha256(trade)
    return trade


def with_assessment(trade, value, other=None):
    """buy／add 的 dip_assessment 都填 value；其餘（hold／no_trade 等）填 other（預設 None）。"""
    trade = copy.deepcopy(trade)
    for row in trade["items"]:
        row["dip_assessment"] = value if row["intent"] in ("buy", "add") else other
    return reseal(trade)


class GateTests(unittest.TestCase):
    def test_gate_is_registered_human_only_and_requires_quality_dip_facts(self):
        self.assertIn("quality_dip_assessment", GATE_FIELDS)
        self.assertIn("quality_dip_assessment", POSITION_SIZING_FIELDS)
        self.assertIn("quality_dip_assessment", FORBIDDEN_PREFIXES)
        errors = []
        validate_gate_prerequisites({"quality_dip_assessment": GATE}, errors)
        self.assertTrue(any("quality_dip_facts" in error for error in errors), errors)
        errors = []
        validate_gate_prerequisites({"quality_dip_assessment": GATE, "quality_dip_facts": GATE}, errors)
        self.assertEqual(errors, [])

    def test_shape_is_closed(self):
        for gate in ({"version": "v2"}, {"version": "v1", "extra": 1}, "v1"):
            errors = []
            validate_quality_dip_assessment_gate({"quality_dip_assessment": gate}, errors)
            self.assertTrue(errors, gate)
        self.assertIsNone(quality_dip_assessment_gate({}))
        self.assertIsNone(quality_dip_assessment_gate(None))
        self.assertEqual(quality_dip_assessment_gate({"quality_dip_assessment": GATE}), GATE)

    def test_schema_only_has_the_field_when_the_gate_is_on(self):
        plain = TRADE_SCHEMA["properties"]["items"]["items"]
        gated = trade_schema_with_dip_assessment()["properties"]["items"]["items"]
        self.assertNotIn("dip_assessment", plain["properties"])
        self.assertIn("dip_assessment", gated["properties"])
        self.assertIn("dip_assessment", gated["required"])
        self.assertEqual(gated["properties"]["dip_assessment"]["enum"], list(DIP_ASSESSMENTS) + [None])


class ItemValidationTests(unittest.TestCase):
    def setUp(self):
        self.facts = build_dip_facts(dip_bundle(), GATE, None, event_report())

    def check(self, value, symbol="S0.TW", facts="default"):
        errors = []
        validate_dip_assessment(
            {"dip_assessment": value}, symbol, "items[0]", self.facts if facts == "default" else facts, errors,
        )
        return errors

    def test_every_enum_value_is_accepted_when_facts_are_available(self):
        for value in DIP_ASSESSMENTS:
            self.assertEqual(self.check(value), [], value)

    def test_missing_or_unknown_value_is_rejected(self):
        for value in (None, "", "cheap", "buy"):
            self.assertTrue(self.check(value), value)

    def test_unavailable_price_facts_only_allow_undetermined(self):
        unavailable = copy.deepcopy(self.facts)
        unavailable["items"][0]["price_pattern"] = "unavailable"
        symbol = unavailable["items"][0]["symbol"]
        self.assertEqual(self.check("undetermined", symbol, unavailable), [])
        errors = self.check("market_driven", symbol, unavailable)
        self.assertTrue(any("只能填 undetermined" in error for error in errors), errors)
        whole = dict(self.facts, data_status="unavailable")
        self.assertTrue(self.check("market_driven", facts=whole))
        self.assertEqual(self.check("undetermined", facts=whole), [])

    def test_unknown_symbol_in_facts_is_treated_as_unavailable(self):
        self.assertTrue(self.check("market_driven", symbol="ZZZ.TW"))
        self.assertEqual(self.check("undetermined", symbol="ZZZ.TW"), [])

    def test_missing_dip_facts_is_rejected(self):
        errors = self.check("undetermined", facts=None)
        self.assertTrue(any("需要 team_inputs.dip_facts" in error for error in errors), errors)


class ChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.world = gated_world(cls.directory.name)
        cls.facts = build_dip_facts(cls.world.bundle, GATE, cls.world.metrics, cls.world.reports["event"])

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def validate(self, trade, typed=True, **options):
        world = self.world
        validator = TradeDecisionValidator(
            world.bundle, world.momentum, world.debate, hold_conviction=True, dip_facts=self.facts, **options,
        )
        if typed:
            validator.context.evidence_policy = "typed_evidence_v1"  # 夾具的決策輸入是舊版；交易意圖驗證只讀這個旗標
        return validator.validate(trade)

    def test_a_sized_intent_exists_in_the_fixture(self):
        self.assertTrue(any(row["intent"] in ("buy", "add") for row in self.world.trade["items"]))

    def test_without_the_gate_the_field_is_rejected_and_old_intents_are_unchanged(self):
        self.assertEqual(self.validate(self.world.trade), [])
        errors = self.validate(with_assessment(self.world.trade, "market_driven"))
        self.assertTrue(any("dip_assessment" in error for error in errors), errors)

    def test_with_the_gate_the_field_is_required_for_buy_and_add(self):
        errors = self.validate(self.world.trade, dip_assessment=True)
        self.assertTrue(any("dip_assessment 必須是" in error for error in errors), errors)
        trade = with_assessment(self.world.trade, "undetermined")
        self.assertEqual(self.validate(trade, dip_assessment=True), [])
        # 夾具的交易池與合成行情不同，事實裡沒有這些股票：視同價格事實不可用，只能 undetermined。
        errors = self.validate(with_assessment(self.world.trade, "market_driven"), dip_assessment=True)
        self.assertTrue(any("只能填 undetermined" in error for error in errors), errors)

    def test_the_value_is_not_tied_to_intent_or_conviction(self):
        # 列舉只受價格事實可用性限制：夾具事實不含這些股票，所以只有 undetermined 通過；其他值被拒的原因一律是價格事實，不是意圖。
        for value in DIP_ASSESSMENTS:
            errors = self.validate(with_assessment(self.world.trade, value), dip_assessment=True)
            if value == "undetermined":
                self.assertEqual(errors, [], value)
            else:
                self.assertTrue(errors and all("只能填 undetermined" in error for error in errors), (value, errors))

    def test_non_sized_items_cannot_carry_a_value(self):
        errors = self.validate(with_assessment(self.world.trade, "undetermined", other="market_driven"), dip_assessment=True)
        self.assertTrue(any("只有 buy／add 可以給 dip_assessment" in error for error in errors), errors)

    def test_missing_typed_evidence_policy_is_rejected(self):
        errors = self.validate(with_assessment(self.world.trade, "undetermined"), typed=False, dip_assessment=True)
        self.assertTrue(any("typed_evidence_v1" in error for error in errors), errors)

    def test_missing_dip_facts_with_the_gate_is_rejected(self):
        world = self.world
        errors = TradeDecisionValidator(
            world.bundle, world.momentum, world.debate, hold_conviction=True, dip_assessment=True,
        ).validate(with_assessment(world.trade, "undetermined"))
        self.assertTrue(any("需要 team_inputs.dip_facts" in error for error in errors), errors)


class FinalizerTests(unittest.TestCase):
    def test_finalizer_rebuilds_the_assessment_check_from_the_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            world = e2e.ChainWorld(directory)
            self.assertEqual(world.finalize()["status"], "approved")
        # 有閘門的整條鏈需要 dip_assessment 在 intent 內；沒有填就必須被拒（見 gated_world）。
        with tempfile.TemporaryDirectory() as directory:
            from unittest import mock

            original = e2e.exposure_sizing

            def with_gates(*args, **kwargs):
                sizing = original(*args, **kwargs)
                sizing["quality_dip_facts"] = dict(GATE)
                sizing["quality_dip_assessment"] = dict(GATE)
                return sizing

            with mock.patch.object(e2e, "exposure_sizing", with_gates):
                world = e2e.ChainWorld(directory)
            facts = build_dip_facts(world.bundle, GATE, world.metrics, world.reports["event"])
            team = world.tampered_team(lambda value: value.update(dip_facts=facts))
            result = world.finalize(team=team)
            self.assertEqual(result["status"], "rejected")
            self.assertTrue(any("dip_assessment 必須是" in error for error in result["errors"]), result["errors"])


class PipelineTests(unittest.TestCase):
    """每日 pipeline 整條鏈（typed_evidence_v1 的 PipelineWorld）：兩個閘門都開時 Schema、驗證與 Finalizer 重建都接得起來。"""

    def run_world(self, trade, attempts=1):
        from pathlib import Path

        from test_daily_pipeline import FakeRunner, PipelineWorld, team_outputs
        from test_quality_dip_facts import gated_template

        template = gated_template()
        template["position_sizing"]["quality_dip_assessment"] = dict(GATE)
        template["position_sizing"]["event_direction"] = dict(GATE)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        world = PipelineWorld(Path(directory.name), approved_status=True, template=template)

        class Recording(FakeRunner):
            def __init__(self, outputs):
                super().__init__(outputs)
                self.schemas = {}

            def run(self, task, run_dir):
                from etf_agent.automation.agent_runner import AgentCall

                self.schemas[task.name] = task.schema
                self.prompts.append((task.name, task.prompt))
                if not self.outputs.get(task.name):
                    raise AssertionError("未預期的 Agent 呼叫：%s" % task.name)
                return AgentCall(task.name, 0, self.outputs[task.name].pop(0), 0.01)  # 不補 dip_assessment：照夾具原樣

        runner = Recording(team_outputs(trader_b0=[copy.deepcopy(trade) for _ in range(attempts)]))
        return world, runner, world.run(runner)

    @staticmethod
    def trade_with(value):
        from test_daily_pipeline import TRADE

        trade = copy.deepcopy(TRADE)
        for row in trade["items"]:
            row["dip_assessment"] = value if row["intent"] in ("buy", "add") else None
        return trade

    def test_gated_run_is_approved_with_assessment_in_schema_prompt_and_intent(self):
        world, runner, result = self.run_world(self.trade_with("undetermined"))
        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(result.decision_status, "approved")
        item = runner.schemas["trader_b0"]["properties"]["items"]["items"]
        self.assertIn("dip_assessment", item["properties"])
        self.assertIn("dip_assessment", dict(runner.prompts)["trader_b0"])
        import json

        intent = json.loads((world.run_dir / "intent.json").read_text(encoding="utf-8")) if (world.run_dir / "intent.json").exists() else None
        trade = json.loads((world.run_dir / "trade_decision.json").read_text(encoding="utf-8"))
        self.assertEqual({row["symbol"]: row["dip_assessment"] for row in trade["items"]}, {"2317.TW": "undetermined", "2330.TW": None})
        del intent

    def test_a_buy_without_the_assessment_exhausts_the_revision_loop(self):
        from test_daily_pipeline import TRADE

        _, _, result = self.run_world(copy.deepcopy(TRADE), attempts=6)  # 沒有 dip_assessment：每次修正都被拒，用盡即失敗
        self.assertNotEqual(result.status, "completed")
        self.assertIn("dip_assessment 必須是", "；".join(result.errors))

    def test_unavailable_price_facts_cannot_claim_market_driven(self):
        # PipelineWorld 的行情歷史太短，dip_facts 的價格事實是 unavailable：只能 undetermined。
        _, _, result = self.run_world(self.trade_with("market_driven"), attempts=6)
        self.assertNotEqual(result.status, "completed")
        self.assertIn("只能填 undetermined", "；".join(result.errors))


if __name__ == "__main__":
    unittest.main()
