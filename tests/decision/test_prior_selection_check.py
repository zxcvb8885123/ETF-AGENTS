"""前次 run 三項修正的重現測試（審查 D-4／D-5／D-6）。

- D-5：決策庫中損壞的 run 不得靜默跳過而退回更早的 run，``find_prior_decision_run`` 必須拒絕（fail-closed）。
- D-4：``position_sizing.cash_ratio_range.selection_check: "v1"``（版本閘門，預設不開）開啟時，Finalizer 以決策庫重跑
  前次 run 挑選，背景指向更早時段、不是該時段 cutoff 最新者，或標 unavailable 卻有候選，一律拒絕；沒有閘門時維持原行為。
- D-6：policy 有 ``cash_ratio_range`` 時，anchor 驗證必須拿到前次決策背景，且 ``selection`` 也要指向同一個 run 的同一挑法。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import (
    DecisionPolicyValidator,
    DecisionRepository,
    DecisionToolError,
    PriorDecisionContextValidator,
    build_prior_decision_context,
    find_prior_decision_run,
    read_decision_run,
)
from etf_agent.decision.cash_ratio import validate_cash_ratio_anchor
from etf_agent.decision.prior_decision import seal_prior_decision_context

from test_cash_ratio_range import GatedWorld, finalize, gate, gate_errors
from test_prior_decision import S1, S2, S3, sealed_run, session_bundle, tradable_bundle


ROOT = Path(__file__).resolve().parents[2]
CURRENT_CUTOFF = "2026-09-20T04:00:00+00:00"


def save_run(root, run_id, snapshot_id, cutoff, session):
    artifacts = sealed_run(bundle=session_bundle(snapshot_id, cutoff, session))
    DecisionRepository(root).save(run_id, artifacts, day="2026-09-20")
    return DecisionRepository(root).verify(run_id)


class DamagedPriorRunTests(unittest.TestCase):
    """D-5：昨天的 run 損壞時不能悄悄改用前天的 run。"""

    def test_unreadable_decision_input_of_usable_run_is_refused(self):
        current = session_bundle("snap-current", CURRENT_CUTOFF, S2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_run(root, "older", "snap-older", "2026-09-20T01:00:00+00:00", S1)
            run_dir = save_run(root, "newer", "snap-newer", "2026-09-20T02:00:00+00:00", S1)
            self.assertEqual(find_prior_decision_run(root, current)["run_id"], "newer")
            (run_dir / "decision_input.json").unlink()
            with self.assertRaises(DecisionToolError) as raised:
                find_prior_decision_run(root, current)
            self.assertIn("newer", str(raised.exception))

    def test_corrupted_decision_json_is_refused(self):
        current = session_bundle("snap-current", CURRENT_CUTOFF, S2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_run(root, "older", "snap-older", "2026-09-20T01:00:00+00:00", S1)
            run_dir = save_run(root, "newer", "snap-newer", "2026-09-20T02:00:00+00:00", S1)
            (run_dir / "decision.json").write_text("{not json", encoding="utf-8")
            with self.assertRaises(DecisionToolError) as raised:
                find_prior_decision_run(root, current)
            self.assertIn("newer", str(raised.exception))

    def test_cutoff_mismatch_between_decision_and_input_is_refused(self):
        current = session_bundle("snap-current", CURRENT_CUTOFF, S2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_run(root, "older", "snap-older", "2026-09-20T01:00:00+00:00", S1)
            run_dir = save_run(root, "newer", "snap-newer", "2026-09-20T02:00:00+00:00", S1)
            decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
            decision["decision_cutoff"] = "2026-09-20T02:30:00+00:00"
            (run_dir / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
            with self.assertRaises(DecisionToolError):
                find_prior_decision_run(root, current)

    def test_rejected_run_with_damaged_input_is_still_skipped(self):
        # 狀態不是 approved／no_trade 的 run 永遠不是候選，不讀它的 decision_input，損壞也不擋流程。
        current = session_bundle("snap-current", CURRENT_CUTOFF, S2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_run(root, "older", "snap-older", "2026-09-20T01:00:00+00:00", S1)
            run_dir = save_run(root, "rejected", "snap-r", "2026-09-20T02:00:00+00:00", S1)
            decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
            decision["status"] = "rejected"
            (run_dir / "decision.json").write_text(json.dumps(decision), encoding="utf-8")
            (run_dir / "decision_input.json").unlink()
            self.assertEqual(find_prior_decision_run(root, current)["run_id"], "older")


class SelectionCheckGateTests(unittest.TestCase):
    """D-4：selection_check 閘門開啟時重跑挑選；沒有閘門時只驗來源內容（原行為）。"""

    def populate(self, root):
        save_run(root, "p-old", "snap-old", "2026-09-20T01:00:00+00:00", S1)
        save_run(root, "p-mid-a", "snap-mid-a", "2026-09-20T02:00:00+00:00", S2)
        save_run(root, "p-mid-b", "snap-mid-b", "2026-09-20T03:00:00+00:00", S2)

    def test_gate_field_is_validated_and_enabled_in_live_config(self):
        self.assertEqual(gate_errors(gate(selection_check="v1")), [])
        self.assertTrue(any("selection_check" in e for e in gate_errors(gate(selection_check="v2"))))
        template = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(template["position_sizing"]["cash_ratio_range"]["selection_check"], "v1")

    def test_stale_session_passes_without_gate_but_fails_with_gate(self):
        current = session_bundle("snap-current", CURRENT_CUTOFF, S3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.populate(root)
            stale = build_prior_decision_context(current, read_decision_run(root, "p-old"))
            self.assertEqual(PriorDecisionContextValidator(current, root).validate(stale), [])
            errors = PriorDecisionContextValidator(current, root, selection_check=True).validate(stale)
            self.assertTrue(any("最晚" in e for e in errors), errors)
            # ledger_applied 只能驗「在最晚前一時段內」；latest_cutoff 要剛好是該時段 cutoff 最新者。
            applied = build_prior_decision_context(current, read_decision_run(root, "p-mid-a"), selection="ledger_applied")
            self.assertEqual(PriorDecisionContextValidator(current, root, selection_check=True).validate(applied), [])
            wrong = build_prior_decision_context(current, read_decision_run(root, "p-mid-a"), selection="latest_cutoff")
            errors = PriorDecisionContextValidator(current, root, selection_check=True).validate(wrong)
            self.assertTrue(any("p-mid-b" in e for e in errors), errors)
            right = build_prior_decision_context(current, read_decision_run(root, "p-mid-b"))
            self.assertEqual(PriorDecisionContextValidator(current, root, selection_check=True).validate(right), [])

    def test_unavailable_with_candidates_or_without_repository_is_rejected(self):
        current = session_bundle("snap-current", CURRENT_CUTOFF, S3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unavailable = build_prior_decision_context(current, None)
            self.assertEqual(PriorDecisionContextValidator(current, root, selection_check=True).validate(unavailable), [])
            self.populate(root)
            errors = PriorDecisionContextValidator(current, root, selection_check=True).validate(unavailable)
            self.assertTrue(any("unavailable" in e for e in errors), errors)
            errors = PriorDecisionContextValidator(current, None, selection_check=True).validate(unavailable)
            self.assertTrue(any("決策庫" in e for e in errors), errors)
            no_session = build_prior_decision_context(tradable_bundle(), None, reason="NO_TARGET_SESSION")
            self.assertEqual(PriorDecisionContextValidator(tradable_bundle(), root, selection_check=True).validate(no_session), [])
            lying = seal_prior_decision_context({**unavailable, "reason": "NO_TARGET_SESSION"})
            self.assertTrue(PriorDecisionContextValidator(current, root, selection_check=True).validate(lying))

    def test_damaged_repository_fails_rebuild_under_gate_only(self):
        current = session_bundle("snap-current", CURRENT_CUTOFF, S3)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.populate(root)
            context = build_prior_decision_context(current, read_decision_run(root, "p-mid-b"))
            (DecisionRepository(root).verify("p-old") / "decision_input.json").unlink()
            self.assertEqual(PriorDecisionContextValidator(current, root).validate(context), [])
            errors = PriorDecisionContextValidator(current, root, selection_check=True).validate(context)
            self.assertTrue(any("p-old" in e for e in errors), errors)

    def test_finalizer_rejects_stale_prior_only_when_gate_is_on(self):
        for gate_value, expected in ((gate(), "approved"), (gate(selection_check="v1"), "rejected")):
            with self.subTest(gate=gate_value), tempfile.TemporaryDirectory() as directory:
                world = GatedWorld().build(directory, gate_value=gate_value)
                # 決策庫在同一前一時段多一個 cutoff 更新的 run：背景記的挑法是 latest_cutoff，pipeline 本應挑它。
                self.assertEqual(world.prior["source"]["selection"], "latest_cutoff")
                save_run(world.repo, "decision-later", "snap-later", "2026-09-20T01:30:00+00:00", S1)
                _, result = finalize(world.bundle, world.momentum, world.debate, world.trade, world.policy, world.team, world.repo)
                self.assertEqual(result["status"], expected, result["errors"])
                if expected == "rejected":
                    self.assertTrue(any("prior_decision_context" in e for e in result["errors"]), result["errors"])

    def test_finalizer_approves_consistent_repository_under_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            world = GatedWorld().build(directory, gate_value=gate(selection_check="v1"))
            self.assertEqual(DecisionPolicyValidator(world.bundle).validate(world.policy), [])
            _, result = finalize(world.bundle, world.momentum, world.debate, world.trade, world.policy, world.team, world.repo)
            self.assertEqual(result["status"], "approved", result["errors"])
            _, result = finalize(world.bundle, world.momentum, world.debate, world.trade, world.policy, world.team, None)
            self.assertEqual(result["status"], "rejected")


class AnchorRequiresPriorContextTests(unittest.TestCase):
    """D-6：anchor 驗證沒有前次決策背景就不能宣稱「同一個 run」。"""

    def test_missing_prior_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            world = GatedWorld().build(directory)
            self.assertEqual(validate_cash_ratio_anchor(world.policy, world.bundle, world.repo, world.prior), [])
            errors = validate_cash_ratio_anchor(world.policy, world.bundle, world.repo, None)
            self.assertTrue(any("前次決策背景" in e for e in errors), errors)

    def test_selection_must_match_prior_context(self):
        with tempfile.TemporaryDirectory() as directory:
            world = GatedWorld().build(directory)
            other = copy.deepcopy(world.prior)
            other["source"]["selection"] = "latest_cutoff" if world.prior["source"]["selection"] == "ledger_applied" else "ledger_applied"
            other = seal_prior_decision_context(other)
            errors = validate_cash_ratio_anchor(world.policy, world.bundle, world.repo, other)
            self.assertTrue(any("selection" in e for e in errors), errors)

    def test_without_gate_prior_context_is_not_required(self):
        # 沒有 cash_ratio_range 的 policy 不讀前次背景，也不要求它（舊 run 原行為）。
        self.assertEqual(validate_cash_ratio_anchor({"position_sizing": {}}, tradable_bundle(), None, None), [])


if __name__ == "__main__":
    unittest.main()
