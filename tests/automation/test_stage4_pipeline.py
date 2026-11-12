"""docs/plans/agent_autonomy_plan.md 階段 4 接入每日決策鏈的端到端測試（真實 pipeline＋真實 Finalizer／結果驗證）。

- ``held_weight_budget`` v2：交易 Agent 簡報附持股權重事實（citable=false）、team_inputs 存選配鍵 ``held_weight_facts``（版本不升）、
  Finalizer 雙向檢查並整份重算；缺少、竄改、沒有閘門卻帶事實一律拒絕；配置與 v1 對照組逐位相同；v1 的 team_inputs 逐鍵不變。
- ``reentry_cooldown`` v3：名單改成交易 Agent 簡報的提示；回應了的買回照常成交、沒回應的買單被取消並留旗標（決策仍完成，不耗盡修正循環）；
  v1 的舊樣板簡報、提示詞與 team_inputs 逐鍵不變。
- ``cash_fit_order`` v2／``high_conviction_cash_priority`` v2：整條鏈可跑、可由 Finalizer 重建。

冷卻名單需要決策庫的前次 run，這裡以 patch 提供固定名單（Finalizer 的重算比對也 patch 成同一份）；名單本身的重算已由
tests/decision/test_reentry_cooldown_v2.py 覆蓋。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from etf_agent.automation.agent_runner import LATER_STAGE_GLOBS
from etf_agent.decision import DecisionRepository

from test_daily_pipeline import BEAR, BULL, TRADE, FakeRunner, PipelineWorld, gateless_policy_template, team_outputs

LIST = (["2317.TW"], ["run-x"])


def template(**sizing):
    value = gateless_policy_template()
    value["position_sizing"].update(sizing)
    return value


def trade_with(*adopted):
    """2317 買回，adopted_claim_ids 另加 ``adopted``。"""
    value = copy.deepcopy(TRADE)
    value["items"][0]["adopted_claim_ids"] = list(value["items"][0]["adopted_claim_ids"]) + list(adopted)
    return value


class Run:
    def __init__(self, test, template_value, outputs=None, patch_list=False):
        self.directory = tempfile.TemporaryDirectory()
        test.addCleanup(self.directory.cleanup)
        self.world = PipelineWorld(Path(self.directory.name), approved_status=True, template=template_value)
        self.runner = FakeRunner(outputs or team_outputs())
        patches = []
        if patch_list:
            patches = [
                mock.patch("etf_agent.decision.reentry_cooldown.recent_exit_symbols", return_value=LIST),
                mock.patch("etf_agent.decision.finalization.recent_exit_symbols", return_value=LIST),
            ]
        for patch in patches:
            patch.start()
            test.addCleanup(patch.stop)
        self.result = self.world.run(self.runner)

    def load(self, name):
        return json.loads((self.world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))

    def team(self):
        run = DecisionRepository(self.world.repository).verify("decision-fixture")
        return json.loads((run / "team_inputs.json").read_text(encoding="utf-8"))

    def flags(self):
        return {(flag["symbol"], flag["code"]) for flag in self.load("proposal_r0")["constraint_flags"]}


class HeldWeightFactsPipelineTests(unittest.TestCase):
    def test_v2_adds_facts_to_the_trader_brief_and_team_inputs_and_is_rebuilt_by_the_finalizer(self):
        run = Run(self, template(held_weight_budget="v2"))
        self.assertEqual(run.result.status, "completed", run.result.errors)
        self.assertEqual(run.result.decision_status, "approved")
        facts = run.load("held_weight_facts")
        trader = run.load("brief_trader_b0")
        self.assertIs(trader["held_weight_background"]["citable"], False)
        self.assertEqual({row["symbol"] for row in trader["symbols"] if "held_weight" in row}, {"2330.TW"})  # 只有持股
        held = next(row for row in trader["symbols"] if row["symbol"] == "2330.TW")["held_weight"]
        self.assertEqual(held["current_weight"], next(row["current_weight"] for row in facts["holdings"] if row["symbol"] == "2330.TW"))
        team = run.team()
        self.assertEqual(team["held_weight_facts"], facts)
        self.assertIn(team["schema_version"], ("2.5", "2.6"))  # 選配鍵不升版本：版本由其他閘門決定
        self.assertIn("held_weight_background", dict(run.runner.prompts)["trader_b0"])
        for role in ("bull", "bear"):
            self.assertNotIn("held_weight", json.dumps(run.load("brief_%s_b0" % role), ensure_ascii=False))
        self.assertIn("held_weight_facts*", LATER_STAGE_GLOBS)

    def test_allocation_is_identical_to_the_v1_control(self):
        v2, v1 = Run(self, template(held_weight_budget="v2")), Run(self, template(held_weight_budget="v1"))
        self.assertEqual(v2.result.orders, v1.result.orders)
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(v2.load("proposal_r0")[key], v1.load("proposal_r0")[key], key)

    def test_v1_keeps_briefs_prompts_and_team_inputs_unchanged(self):
        run = Run(self, template(held_weight_budget="v1"))
        self.assertFalse((run.world.run_dir / "held_weight_facts.json").exists())
        self.assertNotIn("held_weight_facts", run.team())
        for name in ("brief_trader_b0", "brief_bull_b0"):
            self.assertNotIn("held_weight", json.dumps(run.load(name), ensure_ascii=False), name)
        for _, prompt in run.runner.prompts:
            self.assertNotIn("held_weight", prompt)


class ReentryCooldownV3PipelineTests(unittest.TestCase):
    TEMPLATE = {"reentry_cooldown": {"version": "v3", "days": 3}}

    def run_v3(self, trade):
        return Run(self, template(**self.TEMPLATE), team_outputs(trader_b0=[trade]), patch_list=True)

    def test_a_responded_buy_back_is_executed_and_the_brief_carries_the_hint(self):
        run = self.run_v3(trade_with("cooldown:2317.TW"))
        self.assertEqual(run.result.status, "completed", run.result.errors)
        self.assertEqual(run.result.decision_status, "approved")
        self.assertIn(("2317.TW", "buy"), {(o["symbol"], o["side"]) for o in run.result.orders})
        self.assertNotIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), run.flags())
        trader = run.load("brief_trader_b0")
        hinted = {row["symbol"]: row["reentry_cooldown"] for row in trader["symbols"] if "reentry_cooldown" in row}
        self.assertEqual(hinted, {"2317.TW": {"hint_id": "cooldown:2317.TW", "citable": False}})
        self.assertIn("reentry_cooldown", dict(run.runner.prompts)["trader_b0"])
        policy = run.load("policy")
        self.assertEqual(policy["position_sizing"]["reentry_cooldown"]["blocked_symbols"], ["2317.TW"])

    def test_an_unanswered_buy_back_is_cancelled_with_a_flag_and_the_decision_still_completes(self):
        run = self.run_v3(trade_with())
        self.assertEqual(run.result.status, "completed", run.result.errors)
        self.assertEqual(run.result.decision_status, "no_trade")  # 唯一的買單被取消，沒有訂單；不是拒絕，也不走備援
        self.assertNotIn(("2317.TW", "buy"), {(o["symbol"], o["side"]) for o in run.result.orders})
        self.assertIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), run.flags())
        calls = [name for name, _ in run.runner.prompts]
        self.assertEqual(calls.count("trader_b0"), 1)  # 沒回應不進修正循環

    def test_v2_still_blocks_and_ignores_the_response(self):
        run = Run(self, template(reentry_cooldown={"version": "v2", "days": 3}), team_outputs(trader_b0=[trade_with()]), patch_list=True)
        self.assertEqual(run.result.status, "completed", run.result.errors)
        self.assertIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), run.flags())
        trader = run.load("brief_trader_b0")
        self.assertNotIn("reentry_cooldown", json.dumps(trader, ensure_ascii=False))

    def test_an_empty_list_has_no_hint_and_changes_nothing(self):
        run = Run(self, template(**self.TEMPLATE))  # 不 patch：決策庫沒有前次 run，名單為空
        self.assertEqual(run.result.status, "completed", run.result.errors)
        self.assertNotIn("reentry_cooldown", json.dumps(run.load("brief_trader_b0"), ensure_ascii=False))
        self.assertNotIn("RISK_REENTRY_COOLDOWN", {code for _, code in run.flags()})
        self.assertNotIn("近期才由交易 Agent 自己出場", dict(run.runner.prompts)["trader_b0"])  # 沒有提示就不附規則文字


class CashGatesPipelineTests(unittest.TestCase):
    def test_v2_with_the_old_min_fill_ratio_is_stopped_before_any_agent_call(self):
        # 樣板原本帶 cash_fit_min_fill_ratio：v2 不允許，必須在 Agent 呼叫前就被 Policy 驗證擋下。
        run = Run(self, template(cash_fit_order="v2", high_conviction_cash_priority="v2"))
        self.assertNotEqual(run.result.status, "completed")
        self.assertEqual(run.runner.prompts, [])

    def test_cash_fit_v2_without_the_ratio_is_approved_and_matches_the_v1_allocation_when_nothing_is_clipped(self):
        def build(version, priority):
            value = gateless_policy_template()
            value["position_sizing"].pop("cash_fit_min_fill_ratio", None)
            value["position_sizing"].update(cash_fit_order=version, high_conviction_cash_priority=priority)
            return value
        v2 = Run(self, build("v2", "v2"))
        self.assertEqual(v2.result.status, "completed", v2.result.errors)
        self.assertEqual(v2.result.decision_status, "approved")
        v1 = Run(self, build("v1", "v1"))
        self.assertEqual(v2.result.orders, v1.result.orders)
        self.assertEqual(v2.load("proposal_r0")["effective_policy"]["cash_fit_order"], "v2")
        self.assertNotIn("cash_fit_min_fill_ratio", v2.load("proposal_r0")["effective_policy"])


if __name__ == "__main__":
    unittest.main()
