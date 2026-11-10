"""reentry_cooldown v2（只計交易 Agent 自己的出場）的測試。

v2 在 v1 的賣單 exit／forced_exit 之外，還要前次 run 的 intent.json 對該股也是 exit／forced_exit；程式把 hold／trim
賣成 exit（回撤減碼、強制換股等）不計。v1 與沒有閘門的舊 Policy 逐位元維持原行為；intent.json 不可用、版本非法、
Policy 預填名單與決策庫重算不符一律拒絕。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    ProposalValidator,
    bind_reentry_cooldown,
    decision_policy_sha256,
    recent_exit_symbols,
)
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.finalization import DecisionFinalizer, DecisionRepository
from etf_agent.decision.sizing import reentry_cooldown_agent_only

from test_position_sizing import sized_inputs, sized_policy
from test_risk_limit_gates import flags, orders, session

CUTOFF = "2026-09-18T12:00:00+00:00"
START = "2026-09-19T01:00:00+00:00"
EXIT = lambda symbol, intent="exit": {"symbol": symbol, "side": "sell", "intent": intent}


class ReentryCooldownV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle, self.intent, self.stance, self.policy = sized_inputs("medium", "low")
        self.session_bundle = session(copy.deepcopy(self.bundle), "2026-09-21T01:00:00+00:00")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, run_id, orders_, agent_intents, day="2026-09-19", write_intent=True, intent_id=None):
        """封存一個 approved run；agent_intents 是 {代號: 交易 Agent 意圖}。"""
        run = self.root / day / run_id
        run.mkdir(parents=True)
        decision_id = "trade-decision:%s" % run_id
        artifacts = {
            "decision": {"status": "approved", "decision_cutoff": CUTOFF, "orders": orders_, "trade_intent_result_id": decision_id},
            "decision_input": {
                "decision_cutoff": CUTOFF, "account_snapshot": {"account_id": "account-1"},
                "trading_status_bundle": {"target_session": {"start": START}},
            },
        }
        if write_intent:
            artifacts["intent"] = {
                "decision_id": intent_id or decision_id,
                "items": [{"symbol": symbol, "intent": intent} for symbol, intent in agent_intents.items()],
            }
        for name, payload in artifacts.items():
            (run / ("%s.json" % name)).write_text(json.dumps(payload), encoding="utf-8")
        manifest = DecisionRepository(self.root).build_manifest(run_id, artifacts)
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return run

    def mixed_run(self):
        # 2317：Agent exit；2330：Agent hold 但程式賣成 exit；1101：Agent forced_exit；2454：Agent trim 被程式賣成 exit。
        self.archive(
            "run-1",
            [EXIT("2317.TW"), EXIT("2330.TW"), EXIT("1101.TW", "forced_exit"), EXIT("2454.TW"), EXIT("2603.TW", "trim")],
            {"2317.TW": "exit", "2330.TW": "hold", "1101.TW": "forced_exit", "2454.TW": "trim", "2603.TW": "exit"},
        )

    def test_agent_exits_count_but_program_sells_do_not(self):
        self.mixed_run()
        self.assertEqual(recent_exit_symbols(self.root, self.session_bundle, 3)[0],
                         ["1101.TW", "2317.TW", "2330.TW", "2454.TW"])  # v1：程式賣出也算
        self.assertEqual(recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True), (["1101.TW", "2317.TW"], ["run-1"]))
        # 2603：Agent 說 exit 但實際賣單只是 trim（沒有出場）：v2 也不算，因為賣單意圖不是 exit。

    def test_symbol_with_both_sources_is_still_blocked(self):
        self.archive("run-a", [EXIT("2330.TW")], {"2330.TW": "hold"})  # 程式賣
        self.archive("run-b", [EXIT("2330.TW")], {"2330.TW": "exit"}, day="2026-09-20")  # Agent 自己出場
        symbols, runs = recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True)
        self.assertEqual((symbols, runs), (["2330.TW"], ["run-b"]))

    def test_program_only_run_is_not_listed_as_a_source(self):
        self.archive("run-prog", [EXIT("2330.TW")], {"2330.TW": "hold"})
        self.assertEqual(recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True), ([], []))

    def test_missing_or_mismatched_intent_is_rejected_not_treated_as_no_exit(self):
        self.archive("run-nointent", [EXIT("2317.TW")], {}, write_intent=False)
        with self.assertRaises(DecisionToolError):
            recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True)
        self.assertEqual(recent_exit_symbols(self.root, self.session_bundle, 3)[0], ["2317.TW"])  # v1 不讀 intent，行為不變

    def test_intent_of_another_decision_is_rejected(self):
        self.archive("run-x", [EXIT("2317.TW")], {"2317.TW": "exit"}, intent_id="trade-decision:other")
        with self.assertRaises(DecisionToolError):
            recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True)

    def test_corrupted_intent_or_manifest_mismatch_is_rejected(self):
        run = self.archive("run-bad", [EXIT("2317.TW")], {"2317.TW": "exit"})
        (run / "intent.json").write_text("{壞掉", encoding="utf-8")
        with self.assertRaises(DecisionToolError):
            recent_exit_symbols(self.root, self.session_bundle, 3, agent_only=True)

    def test_bind_v2_uses_agent_only_and_v1_is_unchanged(self):
        self.mixed_run()
        for version, expected in (("v1", ["1101.TW", "2317.TW", "2330.TW", "2454.TW"]), ("v2", ["1101.TW", "2317.TW"])):
            template = copy.deepcopy(self.policy)
            template["position_sizing"]["reentry_cooldown"] = {"version": version, "days": 3}
            bound = bind_reentry_cooldown(template, self.root, self.session_bundle)
            self.assertEqual(bound["position_sizing"]["reentry_cooldown"],
                             {"version": version, "days": 3, "blocked_symbols": expected, "source_run_ids": ["run-1"]})

    def test_bind_rejects_unknown_version(self):
        template = copy.deepcopy(self.policy)
        template["position_sizing"]["reentry_cooldown"] = {"version": "v4", "days": 3}
        with self.assertRaises(DecisionToolError):
            bind_reentry_cooldown(template, self.root, self.session_bundle)

    def test_policy_validation_accepts_v2_and_rejects_bad_versions(self):
        for version, ok in (("v1", True), ("v2", True), ("v3", True), ("v4", False), ("V2", False), (2, False), (None, False)):
            with self.subTest(version=version):
                policy = copy.deepcopy(self.policy)
                policy["position_sizing"]["reentry_cooldown"] = {"version": version, "days": 3}
                policy["content_sha256"] = decision_policy_sha256(policy)
                errors = DecisionPolicyValidator(self.bundle).validate(sized_policy(policy, self.intent, self.stance, self.bundle))
                self.assertEqual(not any("reentry_cooldown" in e for e in errors), ok, errors)

    def test_gate_helper(self):
        self.assertTrue(reentry_cooldown_agent_only({"reentry_cooldown": {"version": "v2", "days": 3}}))
        self.assertFalse(reentry_cooldown_agent_only({"reentry_cooldown": {"version": "v1", "days": 3}}))
        self.assertFalse(reentry_cooldown_agent_only({}))

    def bound_policy(self, version):
        self.mixed_run()
        template = copy.deepcopy(self.policy)
        template["position_sizing"]["reentry_cooldown"] = {"version": version, "days": 3}
        bound = bind_reentry_cooldown(template, self.root, self.session_bundle)
        bound["content_sha256"] = decision_policy_sha256(bound)
        return sized_policy(bound, self.intent, self.stance, self.bundle)

    def test_allocation_reads_only_the_bound_list(self):
        """v2 名單只含 Agent 出場：2317 被擋；v1 才把程式賣出的 2330 一併擋住（allocation 只讀 Policy，兩版同一套）。"""
        policy = self.bound_policy("v2")
        self.assertEqual(policy["position_sizing"]["reentry_cooldown"]["blocked_symbols"], ["1101.TW", "2317.TW"])
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy), [])
        body = AllocationOrderEngine(self.bundle, policy).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, policy, self.intent).validate(body), [])
        self.assertIn(("2317.TW", "RISK_REENTRY_COOLDOWN"), flags(body))
        self.assertNotIn(("2317.TW", "buy"), orders(body))

    def finalizer(self, policy, repository):
        """只為了呼叫冷卻名單重算：該方法只讀 policy、bundle 與 decision_repository。"""
        finalizer = object.__new__(DecisionFinalizer)
        finalizer.policy, finalizer.bundle, finalizer.decision_repository = policy, self.session_bundle, repository
        return finalizer

    def test_finalizer_recomputes_the_list_and_rejects_a_forged_one(self):
        policy = self.bound_policy("v2")
        self.assertEqual(self.finalizer(policy, self.root)._validate_reentry_cooldown(), [])
        forged = copy.deepcopy(policy)
        forged["position_sizing"]["reentry_cooldown"]["blocked_symbols"] = ["1101.TW"]  # 偷偷解除 2317 的冷卻
        self.assertTrue(self.finalizer(forged, self.root)._validate_reentry_cooldown())
        forged = copy.deepcopy(policy)
        forged["position_sizing"]["reentry_cooldown"]["source_run_ids"] = ["run-zzz"]
        self.assertTrue(self.finalizer(forged, self.root)._validate_reentry_cooldown())
        # 把程式賣出的 2330 塞回名單（v1 口徑）也不符 v2 重算。
        forged = copy.deepcopy(policy)
        forged["position_sizing"]["reentry_cooldown"]["blocked_symbols"] = ["1101.TW", "2317.TW", "2330.TW"]
        self.assertTrue(self.finalizer(forged, self.root)._validate_reentry_cooldown())

    def test_finalizer_rejects_unbound_list_and_unreadable_history(self):
        policy = self.bound_policy("v2")
        unbound = copy.deepcopy(policy)
        for key in ("blocked_symbols", "source_run_ids"):
            unbound["position_sizing"]["reentry_cooldown"].pop(key)
        self.assertTrue(self.finalizer(unbound, self.root)._validate_reentry_cooldown())
        (self.root / "2026-09-19" / "run-1" / "intent.json").unlink()
        errors = self.finalizer(policy, self.root)._validate_reentry_cooldown()
        self.assertTrue(errors and "重算" in errors[0], errors)

    def test_finalizer_leaves_v1_and_ungated_policies_alone(self):
        v1 = self.bound_policy("v1")
        self.assertEqual(self.finalizer(v1, None)._validate_reentry_cooldown(), [])  # v1 不碰決策庫，連名單都不重算
        self.assertEqual(self.finalizer(self.policy, None)._validate_reentry_cooldown(), [])


if __name__ == "__main__":
    unittest.main()
