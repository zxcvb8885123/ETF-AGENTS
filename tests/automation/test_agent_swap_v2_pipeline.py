"""換股提名 v2（正式）接入每日決策鏈的端到端測試：docs/agent_nominated_rotation_plan.md 第 3 節、5.1「Policy 與重建」。

整條鏈用正式樣板（只拿掉 fixture 沒有資料的 cash_stance_recency，並依 v2 的互斥拿掉 forced_swap 與 conviction_gap_swap）跑真實
pipeline 與真實 Finalizer：

- v2：5c 提名的有序配對在 5c 之後綁進 policy，配置依提名換股（換出者賣單標 exit＋AGENT_SWAP、換入者 AGENT_SWAP_FILL，
  交易 Agent 的意圖不被改寫）；整份決策可被 Finalizer 與 DecisionResultValidator 重建驗證。
- 影子 v1 的配置（訂單與旗標）與沒有閘門時逐位相同（test_swap_nomination_pipeline.py 已涵蓋；這裡再對照 v2 的差異）。
- 提名 exhausted／unavailable：配置留 ``AGENT_SWAP_SKIPPED:nomination_*``、其餘訂單照常。
- fail-closed：policy 的配對與提名封存不一致、v2 未綁定提名、v2 與舊換股路徑同時出現、team_inputs 與閘門不同步一律拒絕。
- 舊 run：沒有 agent_swap 的 2.8 與 v1 影子的 2.9 照舊重建。

夾具沿用 test_swap_nomination_pipeline 的 ``Harness``（2330 持有、2317 新股、max_positions=1 造出名額缺口）。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import TransientAgentError
from etf_agent.decision import DecisionToolError, decision_policy_sha256

from test_daily_pipeline import ROOT, FakeRunner, PipelineWorld
from test_swap_nomination_pipeline import (
    BAD_NOMINATION, GATE, NOMINATION, Harness, flags, outputs, sealed, template,
)

V2 = dict(GATE, version="v2")


def v2_template():
    value = template(V2)
    value["position_sizing"]["rotation"].pop("forced_swap", None)  # v2 與舊換股路徑互斥
    value["position_sizing"]["exposure_rotation"].pop("conviction_gap_swap", None)
    value["max_turnover_rate"] = "1"
    return value


class V2Harness(Harness):
    """與 ``Harness`` 同一個世界，只是樣板為 v2（沒有 forced_swap、conviction_gap_swap）；換手上限放到 100%，讓一對換股的成交額不被縮減。"""

    def __init__(self, directory, tight=True):
        self.world = PipelineWorld(Path(directory), approved_status=True, template=v2_template())
        account = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        account.update(cash="1000000", settled_cash="1000000", nav="1164000")
        self.world.paths["account"].write_text(json.dumps(account, ensure_ascii=False), encoding="utf-8")
        if tight:
            rules = json.loads(self.world.paths["rules"].read_text(encoding="utf-8"))
            rules["max_positions"] = 1
            self.world.paths["rules"].write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")


def codes(proposal):
    return {flag["code"] for flag in proposal["constraint_flags"]}


class V2RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "control").mkdir()
        (base / "v2").mkdir()
        cls.control = Harness(base / "control", gate=None)
        cls.control_result, _ = cls.control.run(FakeRunner(outputs()))
        cls.v2 = V2Harness(base / "v2")
        cls.runner = FakeRunner(outputs(NOMINATION))
        cls.v2_result, cls.v2_pipeline = cls.v2.run(cls.runner)
        cls.v2_art = cls.v2.artifacts()
        cls.control_art = cls.control.artifacts()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_the_run_completes_and_the_sealed_decision_is_rebuilt_by_the_finalizer(self):
        self.assertEqual(self.v2_result.status, "completed", self.v2_result.errors)
        art = self.v2_art
        self.assertEqual(art["team_inputs"]["schema_version"], "2.9")
        self.assertEqual(art["decision"]["errors"], [])
        self.assertEqual(self.v2.validate(art), [])
        self.assertEqual(self.v2.rebuild_errors(art), [])

    def test_the_nominated_pair_is_bound_into_the_policy_and_used_by_the_allocation(self):
        art = self.v2_art
        gate = art["policy"]["position_sizing"]["agent_swap"]
        self.assertEqual(gate["version"], "v2")
        self.assertEqual(gate["nominated_pairs"], [{"buy_symbol": "2317.TW", "sell_symbol": "2330.TW"}])
        nomination = art["team_inputs"]["swap_nomination"]
        self.assertEqual(gate["nomination_ref"], {
            "nomination_id": nomination["nomination_id"], "sha256": nomination["content_sha256"], "status": "completed"})
        found = {(flag["symbol"], flag["code"]) for flag in art["proposal"]["constraint_flags"]}
        self.assertIn(("2330.TW", "AGENT_SWAP"), found)
        self.assertIn(("2317.TW", "AGENT_SWAP_FILL"), found)
        self.assertIn(("PORTFOLIO", "EXPOSURE_ROTATION_SKIPPED:agent_swap"), found)
        orders = {(o["symbol"], o["side"]): o["intent"] for o in art["decision"]["orders"]}
        self.assertEqual(orders, {("2330.TW", "sell"): "exit", ("2317.TW", "buy"): "buy"})
        for item in art["intent"]["items"]:  # 交易 Agent 的原意圖沒被改寫
            self.assertEqual(item["intent"], {"2330.TW": "hold", "2317.TW": "buy"}[item["symbol"]])

    def test_without_the_gate_the_same_world_has_no_swap_and_a_2_8_team_input(self):
        control = self.control_art
        self.assertEqual(control["team_inputs"]["schema_version"], "2.8")
        self.assertEqual(self.control.validate(control), [])
        self.assertFalse([code for code in codes(control["proposal"]) if code.startswith("AGENT_SWAP")])
        self.assertNotEqual(control["decision"]["orders"], self.v2_art["decision"]["orders"])  # v2 的提名在這個世界確實換了股

    def test_the_policy_slots_are_1_1_in_the_brief_and_in_team_inputs(self):
        slots = self.v2_art["team_inputs"]["portfolio_slots"]
        self.assertEqual(slots["schema_version"], "1.1")
        self.assertIn("swap_nomination_background", slots)
        brief = (self.v2.world.run_dir / "brief_trader_b0.json").read_text(encoding="utf-8")
        self.assertIn("swap_nomination_background", brief)

    # -- fail-closed：Finalizer
    def assertRejected(self, errors, token):
        self.assertTrue(any(token in error for error in errors), (token, errors))

    def rejects(self, art, **replace):
        """重建錯誤；Policy 本身就不合法時引擎建構即拋 DecisionToolError，同樣算拒絕。"""
        try:
            return self.v2.rebuild_errors(art, **replace)
        except DecisionToolError as error:
            return [str(error)]

    def test_pairs_that_differ_from_the_sealed_nomination_are_rejected(self):
        art = self.v2_art
        for label, mutate in {
            "配對被改寫": lambda gate: gate.update(nominated_pairs=[{"buy_symbol": "2317.TW", "sell_symbol": "9999.TW"}]),
            "配對被清空": lambda gate: gate.update(nominated_pairs=[]),
            "狀態被改寫": lambda gate: gate["nomination_ref"].update(status="not_needed"),
            "雜湊被改寫": lambda gate: gate["nomination_ref"].update(sha256="f" * 64),
        }.items():
            with self.subTest(label):
                tampered = sealed(art["policy"], lambda policy: mutate(policy["position_sizing"]["agent_swap"]))
                self.assertTrue(self.rejects(art, policy=tampered), label)
        cleared = sealed(art["policy"], lambda policy: policy["position_sizing"]["agent_swap"].update(nominated_pairs=[]))
        self.assertRejected(self.rejects(art, policy=cleared), "agent_swap.nominated_pairs 與 team_inputs.swap_nomination 不一致")

    def test_a_v2_policy_without_the_binding_is_rejected(self):
        art = self.v2_art

        def unbind(policy):
            for field in ("nominated_pairs", "nomination_ref"):
                policy["position_sizing"]["agent_swap"].pop(field)

        unbound = sealed(art["policy"], unbind)
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定換股提名"):
            self.v2.rebuild_errors(art, policy=unbound)

    def test_v2_together_with_the_old_swap_paths_is_rejected(self):
        art = self.v2_art
        both = sealed(art["policy"], lambda policy: policy["position_sizing"]["rotation"].update(
            forced_swap={"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 2}))
        with self.assertRaises(DecisionToolError):  # 引擎建構時 Policy 驗證即拒絕
            self.v2.rebuild_errors(art, policy=both)

    def test_a_forged_order_is_rejected(self):
        art = self.v2_art
        forged = copy.deepcopy(art["proposal"])
        forged["order_proposal"]["orders"] = [o for o in forged["order_proposal"]["orders"] if o["side"] == "buy"]  # 只買不賣
        self.assertTrue(self.v2.validate(art, proposal=forged))

    def test_the_team_inputs_version_must_stay_in_sync_with_the_gate(self):
        art = self.v2_art
        stuck = copy.deepcopy(art["team_inputs"])
        stuck.pop("swap_nomination")
        stuck["schema_version"] = "2.8"
        self.assertRejected(self.v2.rebuild_errors(art, team_inputs=stuck), "agent_swap 必須與 team_inputs 2.9 同時存在")


class V2DegradedRunTests(unittest.TestCase):
    def baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness(directory)
            runner = FakeRunner(outputs(BAD_NOMINATION, BAD_NOMINATION, BAD_NOMINATION))
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            return harness.artifacts()

    def test_exhausted_nomination_leaves_a_skip_flag_and_the_rest_of_the_orders_are_as_without_nominations(self):
        art = self.baseline()
        self.assertEqual(art["team_inputs"]["swap_nomination"]["status"], "exhausted")
        self.assertIn("AGENT_SWAP_SKIPPED:nomination_exhausted", codes(art["proposal"]))
        self.assertFalse(codes(art["proposal"]) & {"AGENT_SWAP", "AGENT_SWAP_FILL"})
        self.assertEqual(art["decision"]["orders"], [])  # 沒有提名就不換，程式不補挑
        self.assertEqual(art["policy"]["position_sizing"]["agent_swap"]["nominated_pairs"], [])

    def test_unavailable_nomination_leaves_a_skip_flag(self):
        class Flaky(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "swap_nominator":
                    self.prompts.append((task.name, task.prompt))
                    raise TransientAgentError("逾時")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness(directory)
            result, _ = harness.run(Flaky(outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            self.assertEqual(art["team_inputs"]["swap_nomination"]["status"], "unavailable")
            self.assertIn("AGENT_SWAP_SKIPPED:nomination_unavailable", codes(art["proposal"]))
            self.assertEqual(harness.validate(art), [])

    def test_not_needed_binds_an_empty_list_and_leaves_no_agent_swap_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness(directory, tight=False)  # 沒有名額缺口：程式直接封存 not_needed
            runner = FakeRunner(outputs())
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertNotIn("swap_nominator", [name for name, _ in runner.prompts])
            art = harness.artifacts()
            gate = art["policy"]["position_sizing"]["agent_swap"]
            self.assertEqual((gate["nominated_pairs"], gate["nomination_ref"]["status"]), ([], "not_needed"))
            self.assertFalse([code for code in codes(art["proposal"]) if code.startswith("AGENT_SWAP")])
            self.assertEqual(harness.validate(art), [])


if __name__ == "__main__":
    unittest.main()
