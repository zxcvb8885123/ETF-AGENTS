"""reentry_cooldown v4（換股換出也算交易 Agent 自己的出場，並綁出場理由）的測試。

起因：11-25 交易 Agent 第二輪提名換出 2609，v3 名單只計第一輪 exit，所以 11-26 沒有任何提示就把 2609 換回。
v4 名單另含 proposal 帶 ``AGENT_SWAP`` 旗標且賣單意圖 exit 的股票，並為每檔綁 ``exit_reasons``（種類、交易時段與理由文字），
只提示、不擋買單，也不再排除換股換入候選。v1～v3 逐位元不變；proposal／intent／team_inputs 不可用或對不上一律拒絕。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.decision import DecisionPolicyValidator, bind_reentry_cooldown, decision_policy_sha256
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.finalization import DecisionFinalizer, DecisionRepository
from etf_agent.decision.reentry_cooldown import recent_exit_records
from etf_agent.decision.sizing import (
    reentry_cooldown_agent_only,
    reentry_cooldown_hint_only,
    reentry_cooldown_v4,
    reentry_exit_reasons,
    reentry_hint_symbols,
)

from test_position_sizing import sized_inputs, sized_policy
from test_risk_limit_gates import session

CUTOFF = "2026-09-18T12:00:00+00:00"
START = "2026-09-19T01:00:00+00:00"
EXIT = lambda symbol, intent="exit": {"symbol": symbol, "side": "sell", "intent": intent}


class ReentryCooldownV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundle, self.intent, self.stance, self.policy = sized_inputs("medium", "low")
        self.session_bundle = session(copy.deepcopy(self.bundle), "2026-09-21T01:00:00+00:00")

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, run_id, orders_, agent_items, swaps=(), day="2026-09-19", start=START,
                write_team=True, team_sha=None, proposal_id=None):
        """封存一個 approved run。agent_items：{代號: (意圖, 理由)}；swaps：[(換出, 換入, 提名理由)]。"""
        run = self.root / day / run_id
        run.mkdir(parents=True)
        decision_id = "trade-decision:%s" % run_id
        team = {"swap_nomination": {"nominations": [
            {"sell_symbol": sell, "buy_symbol": buy, "reason": reason} for sell, buy, reason in swaps]}}
        artifacts = {
            "decision": {
                "status": "approved", "decision_cutoff": CUTOFF, "orders": orders_, "trade_intent_result_id": decision_id,
                "proposal_id": "proposal:%s" % run_id, "team_inputs_sha256": team_sha or canonical_sha256(team),
            },
            "decision_input": {
                "decision_cutoff": CUTOFF, "account_snapshot": {"account_id": "account-1"},
                "trading_status_bundle": {"target_session": {"start": start}},
            },
            "intent": {
                "decision_id": decision_id,
                "items": [{"symbol": s, "intent": i, "rationale": r} for s, (i, r) in agent_items.items()],
            },
            "proposal": {
                "proposal_id": proposal_id or "proposal:%s" % run_id,
                "constraint_flags": [{"symbol": sell, "code": "AGENT_SWAP", "detail": "換出"} for sell, _, _ in swaps]
                + [{"symbol": buy, "code": "AGENT_SWAP_FILL", "detail": "換入"} for _, buy, _ in swaps],
            },
        }
        if write_team:
            artifacts["team_inputs"] = team
        for name, payload in artifacts.items():
            (run / ("%s.json" % name)).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        manifest = DecisionRepository(self.root).build_manifest(run_id, artifacts)
        (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return run

    def swap_run(self, **kwargs):
        # 2609：第一輪 hold、第二輪被提名換出；2317：第一輪就 exit；2330：程式賣成 exit（不是 Agent 決定）。
        return self.archive(
            "run-1",
            [EXIT("2609.TW"), EXIT("2317.TW"), EXIT("2330.TW")],
            {"2609.TW": ("hold", "續抱理由"), "2317.TW": ("exit", "營收轉弱，出場"), "2330.TW": ("hold", "續抱")},
            swaps=[("2609.TW", "2404.TW", "跌幅超出大盤能解釋的部分，換成營運較完整的一檔")],
            **kwargs,
        )

    # ------------------------------------------------------------------ 名單與理由

    def test_swap_victims_now_count_with_their_reasons(self):
        self.swap_run()
        symbols, runs, reasons = recent_exit_records(self.root, self.session_bundle, 3)
        self.assertEqual((symbols, runs), (["2317.TW", "2609.TW"], ["run-1"]))
        self.assertEqual(reasons["2609.TW"], {
            "run_id": "run-1", "session_start": "2026-09-19T01:00:00+00:00", "kind": "agent_swap",
            "reason": "跌幅超出大盤能解釋的部分，換成營運較完整的一檔"})
        self.assertEqual(reasons["2317.TW"]["kind"], "agent_exit")
        self.assertEqual(reasons["2317.TW"]["reason"], "營收轉弱，出場")
        self.assertNotIn("2330.TW", reasons)  # 程式賣出仍不算

    def test_latest_exit_wins_when_a_symbol_exits_twice(self):
        self.archive("run-a", [EXIT("2609.TW")], {"2609.TW": ("exit", "舊理由")})
        self.archive("run-b", [EXIT("2609.TW")], {"2609.TW": ("hold", "續抱")},
                     swaps=[("2609.TW", "2404.TW", "新理由")], day="2026-09-20", start="2026-09-20T01:00:00+00:00")
        _, runs, reasons = recent_exit_records(self.root, self.session_bundle, 3)
        self.assertEqual(runs, ["run-a", "run-b"])
        self.assertEqual((reasons["2609.TW"]["run_id"], reasons["2609.TW"]["reason"]), ("run-b", "新理由"))

    def test_v3_list_is_unchanged_by_the_new_code(self):
        self.swap_run()
        template = copy.deepcopy(self.policy)
        template["position_sizing"]["reentry_cooldown"] = {"version": "v3", "days": 3}
        bound = bind_reentry_cooldown(template, self.root, self.session_bundle)
        self.assertEqual(bound["position_sizing"]["reentry_cooldown"],
                         {"version": "v3", "days": 3, "blocked_symbols": ["2317.TW"], "source_run_ids": ["run-1"]})

    def test_missing_or_mismatched_sources_are_rejected(self):
        cases = (
            ("no-team", dict(write_team=False)),
            ("bad-team", dict(team_sha="team-other")),
            ("bad-proposal", dict(proposal_id="proposal:other")),
        )
        for label, kwargs in cases:
            with self.subTest(label):
                self.temp.cleanup()
                self.temp = tempfile.TemporaryDirectory()
                self.root = Path(self.temp.name)
                self.swap_run(**kwargs)
                with self.assertRaises(DecisionToolError):
                    recent_exit_records(self.root, self.session_bundle, 3)

    def test_swap_flag_without_a_nomination_reason_is_rejected(self):
        self.archive("run-1", [EXIT("2609.TW")], {"2609.TW": ("hold", "續抱")}, swaps=[("2609.TW", "2404.TW", "  ")])
        with self.assertRaises(DecisionToolError):
            recent_exit_records(self.root, self.session_bundle, 3)

    def test_stop_exits_are_listed_with_the_flag_text_as_reason(self):
        run = self.archive("run-1", [EXIT("2317.TW")], {"2317.TW": ("hold", "續抱")})
        proposal = json.loads((run / "proposal.json").read_text(encoding="utf-8"))
        proposal["constraint_flags"].append({"symbol": "2317.TW", "code": "RISK_STOP_EXIT", "detail": "自進場虧損達停損線"})
        (run / "proposal.json").write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        artifacts = {name: json.loads((run / ("%s.json" % name)).read_text(encoding="utf-8"))
                     for name in ("decision", "decision_input", "intent", "proposal", "team_inputs")}
        (run / "manifest.json").write_text(json.dumps(DecisionRepository(self.root).build_manifest("run-1", artifacts)), encoding="utf-8")
        self.assertEqual(recent_exit_records(self.root, self.session_bundle, 3)[0], [])  # 沒開 stop_exit 不算
        symbols, _, reasons = recent_exit_records(self.root, self.session_bundle, 3, include_stop_exits=True)
        self.assertEqual(symbols, ["2317.TW"])
        self.assertEqual((reasons["2317.TW"]["kind"], reasons["2317.TW"]["reason"]), ("stop_exit", "自進場虧損達停損線"))

    # ------------------------------------------------------------------ Policy 綁定、驗證與 Finalizer

    def bound_policy(self):
        if not (self.root / "2026-09-19" / "run-1").exists():
            self.swap_run()
        template = copy.deepcopy(self.policy)
        template["position_sizing"]["reentry_cooldown"] = {"version": "v4", "days": 3}
        bound = bind_reentry_cooldown(template, self.root, self.session_bundle)
        bound["content_sha256"] = decision_policy_sha256(bound)
        return sized_policy(bound, self.intent, self.stance, self.bundle)

    def test_bind_and_helpers(self):
        policy = self.bound_policy()
        sizing = policy["position_sizing"]
        cooldown = sizing["reentry_cooldown"]
        self.assertEqual(cooldown["blocked_symbols"], ["2317.TW", "2609.TW"])
        self.assertEqual(sorted(cooldown["exit_reasons"]), ["2317.TW", "2609.TW"])
        self.assertTrue(reentry_cooldown_v4(sizing) and reentry_cooldown_hint_only(sizing) and reentry_cooldown_agent_only(sizing))
        self.assertEqual(reentry_hint_symbols(sizing), ["2317.TW", "2609.TW"])
        self.assertEqual(reentry_exit_reasons(sizing)["2609.TW"]["kind"], "agent_swap")
        self.assertIsNone(reentry_exit_reasons({"reentry_cooldown": {"version": "v3", "days": 3, "blocked_symbols": []}}))
        self.assertEqual(DecisionPolicyValidator(self.bundle).validate(policy), [])

    def validation_errors(self, mutate):
        policy = self.bound_policy()
        mutate(policy["position_sizing"]["reentry_cooldown"], policy["position_sizing"])
        policy["content_sha256"] = decision_policy_sha256(policy)
        return [e for e in DecisionPolicyValidator(self.bundle).validate(policy) if "reentry_cooldown" in e]

    def test_policy_validation_rejects_malformed_reasons(self):
        def drop(c, s): c.pop("exit_reasons")
        def extra_symbol(c, s): c["exit_reasons"]["9999.TW"] = dict(c["exit_reasons"]["2609.TW"])
        def bad_kind(c, s): c["exit_reasons"]["2609.TW"]["kind"] = "program"
        def empty_reason(c, s): c["exit_reasons"]["2609.TW"]["reason"] = ""
        def unknown_field(c, s): c["exit_reasons"]["2609.TW"]["weight"] = "0.1"
        def on_v3(c, s): c["version"] = "v3"
        for mutate in (drop, extra_symbol, bad_kind, empty_reason, unknown_field, on_v3):
            with self.subTest(mutate.__name__):
                self.assertTrue(self.validation_errors(mutate), mutate.__name__)

    def finalizer(self, policy):
        finalizer = object.__new__(DecisionFinalizer)
        finalizer.policy, finalizer.bundle, finalizer.decision_repository = policy, self.session_bundle, self.root
        return finalizer

    def test_finalizer_recomputes_reasons_and_rejects_forgery(self):
        policy = self.bound_policy()
        self.assertEqual(self.finalizer(policy)._validate_reentry_cooldown(), [])
        forged = copy.deepcopy(policy)
        forged["position_sizing"]["reentry_cooldown"]["exit_reasons"]["2609.TW"]["reason"] = "改寫過的理由"
        self.assertTrue(self.finalizer(forged)._validate_reentry_cooldown())
        forged = copy.deepcopy(policy)
        forged["position_sizing"]["reentry_cooldown"]["blocked_symbols"] = ["2317.TW"]  # 偷偷拿掉換股換出者
        self.assertTrue(self.finalizer(forged)._validate_reentry_cooldown())


if __name__ == "__main__":
    unittest.main()
