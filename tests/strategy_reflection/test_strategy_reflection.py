import copy
import json
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from etf_agent.automation.agent_runner import AgentCall
from etf_agent.core import canonical_sha256
from etf_agent.strategy_reflection import (
    ReflectionError, ReflectionRepository, apply_proposals, apply_reflection, build_brief, check_proposal,
    current_knobs, from_agent_output,
    observe_only, replay_gate, rollback, run_auto, save_reflection, validate_reflection,
)
from etf_agent.strategy_reflection import service

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 20, 12, 30, tzinfo=timezone.utc)


def report(days=12, status="ok"):
    ok = {"status": "ok", "complete_days": days, "n_obs": 100,
          "by_conviction": {"high": {"n_obs": 30, "mean_return": "0.01", "mean_excess_return": "0.004"}},
          "rank_ic": {"conviction": {"mean": "0.1", "n_days": days}}}
    return {"decision_days": days, "horizons": {"1": ok if status == "ok" else {"status": "insufficient_sample", "complete_days": days},
                                                 "5": {"status": "insufficient_sample", "complete_days": days},
                                                 "20": {"status": "insufficient_sample", "complete_days": days}}}


def replay_body(end_nav="100", rejected=0, status="completed", days=3):
    return {"status": status, "metrics": {"end_nav": end_nav, "trading_days": days},
            "days": [{"decision_status": "rejected"}] * rejected + [{"decision_status": "approved"}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "config").mkdir()
        for name in ("decision_policy.json", "decision_rules.json"):
            shutil.copy(ROOT / "config" / name, self.tmp / "config" / name)
        self.template = json.loads((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.rules = json.loads((self.tmp / "config" / "decision_rules.json").read_text(encoding="utf-8"))
        self.brief = build_brief(report(), self.template, NOW.isoformat())
        self.repository = ReflectionRepository(self.tmp)

    def output(self, **overrides):
        item = {"knob": "conviction_multipliers.high", "proposed": "1.25", "rationale": "high 信心超額報酬為正，但幅度小", "evidence_ids": ["cal.h1.by_conviction.high.mean_excess_return"]}
        item.update(overrides)
        return {"summary": "一則提案", "observations": [{"text": "high 平均超額報酬 0.4%", "evidence_ids": ["cal.h1.by_conviction.high.mean_excess_return"]}], "proposals": [item]}

    def reflection(self, **overrides):
        return from_agent_output(self.output(**overrides), self.brief, self.template, self.rules, NOW)

    def live(self):
        return json.loads((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8"))


class KnobTests(Fixture):
    def test_valid_proposal_returns_normalised_values(self):
        self.assertEqual(check_proposal("conviction_multipliers.high", "1.25", self.template, self.rules), {"current": "1.5", "proposed": "1.25"})

    def test_forbidden_and_unknown_knobs_are_rejected(self):
        for knob in ("slippage_bps", "max_stock_weight", "stress_price_decline_rate", "cash_weight_ceiling", "policy_id", "nope.nope"):
            with self.assertRaises(ReflectionError, msg=knob):
                check_proposal(knob, "1", self.template, self.rules)

    def test_step_range_direction_and_no_op_are_rejected(self):
        cases = [("conviction_multipliers.high", "1.9"), ("cash_buffer_by_stance.defensive", "0.25"), ("positions_by_breadth.diversified", "31"),
                 ("positions_by_breadth.concentrated", "18"), ("drawdown_control.neutral_at", "0.06"), ("conviction_multipliers.high", "1.5"),
                 ("trim_fraction", "abc"), ("positions_by_breadth.balanced", "26.5")]
        for knob, value in cases:
            with self.assertRaises(ReflectionError, msg=(knob, value)):
                check_proposal(knob, value, self.template, self.rules)

    def test_version_gates_stay_forbidden(self):
        """版本閘門是切換程式路徑，不是調整幅度；反思員不得提案。"""
        for knob in ("held_add_priority", "rotation.version", "method", "stock_weight_fit"):
            with self.assertRaises(ReflectionError, msg=knob):
                check_proposal(knob, "v2", self.template, self.rules)

    def test_volatility_exponent_is_adjustable_within_limits(self):
        """低波動偏好強度是正式旋鈕：反思員可依校準報告調整，但受變幅與範圍限制。"""
        template = copy.deepcopy(self.template)
        template["position_sizing"]["method"] = "conviction_volatility_v2"
        template["position_sizing"]["volatility_exponent"] = "0.5"
        self.assertEqual(
            check_proposal("volatility_exponent", "0.25", template, self.rules),
            {"current": "0.5", "proposed": "0.25"},
        )
        for bad in ("0.1", "1.1", "-0.1", "0.5"):  # 超過 0.25 變幅、超出範圍、與現值相同
            with self.assertRaises(ReflectionError, msg=bad):
                check_proposal("volatility_exponent", bad, template, self.rules)

    def test_max_swaps_is_adjustable_and_not_clamped_by_position_rules(self):
        """換股上限是整數旋鈕，但範圍是自己的 0～10，不該被持股檔數規則夾成 20～30。"""
        template = copy.deepcopy(self.template)
        template["position_sizing"]["rotation"] = {
            "version": "v1", "max_swaps_per_run": 10, "min_new_conviction": "low",
        }
        self.assertEqual(
            check_proposal("rotation.max_swaps_per_run", "8", template, self.rules),
            {"current": "10", "proposed": "8"},
        )
        for bad in ("5", "11", "8.5"):  # 超過 2 檔變幅、超出上限、不是整數
            with self.assertRaises(ReflectionError, msg=bad):
                check_proposal("rotation.max_swaps_per_run", bad, template, self.rules)

    def test_current_knobs_skips_optional_knobs_absent_from_template(self):
        """舊樣板（v1、未啟用 rotation）的選配旋鈕必須略過，而不是讓反思員整個失敗。"""
        legacy = copy.deepcopy(self.template)
        legacy["position_sizing"]["method"] = "conviction_volatility_v1"
        legacy["position_sizing"].pop("volatility_exponent", None)
        legacy["position_sizing"].pop("rotation", None)
        values = current_knobs(legacy)
        self.assertNotIn("volatility_exponent", values)
        self.assertNotIn("rotation.max_swaps_per_run", values)
        self.assertIn("conviction_multipliers.high", values)

    def test_current_knobs_includes_optional_knobs_once_enabled(self):
        """正式樣板已啟用：反思員必須看得到這兩個旋鈕才可能依校準報告調整。"""
        values = current_knobs(self.template)
        self.assertEqual(values["volatility_exponent"], "0")
        self.assertEqual(values["rotation.max_swaps_per_run"], "10")

    def test_drawdown_can_only_tighten(self):
        self.assertEqual(check_proposal("drawdown_control.neutral_at", "0.04", self.template, self.rules)["proposed"], "0.04")

    def test_ordering_violation_is_rejected_by_policy_rules(self):
        with self.assertRaises(ReflectionError):
            apply_proposals(self.template, [{"knob": "conviction_multipliers.high", "proposed": "0.9"}], self.rules)

    def test_apply_proposals_does_not_mutate_the_input(self):
        before = canonical_sha256(self.template)
        candidate = apply_proposals(self.template, [{"knob": "conviction_multipliers.high", "proposed": "1.25"}], self.rules)
        self.assertEqual(canonical_sha256(self.template), before)
        self.assertEqual(candidate["position_sizing"]["conviction_multipliers"]["high"], "1.25")


class BriefAndValidatorTests(Fixture):
    def test_gate_needs_an_ok_short_horizon(self):
        self.assertTrue(self.brief["gate_open"])
        closed = build_brief(report(status="insufficient"), self.template, NOW.isoformat())
        self.assertFalse(closed["gate_open"])
        self.assertNotIn("cal.h1.by_conviction.high.mean_excess_return", closed["evidence"])

    def test_valid_reflection_passes(self):
        validate_reflection(self.reflection(), self.brief, self.template, self.rules, [])

    def test_observe_only_with_proposals_is_rejected(self):
        reflection = self.reflection()
        reflection["status"] = "observe_only"
        reflection["content_sha256"] = service.reflection_sha256(reflection)
        with self.assertRaises(ReflectionError):
            validate_reflection(reflection, self.brief, self.template, self.rules, [])

    def test_proposal_with_closed_gate_is_rejected(self):
        closed = build_brief(report(status="insufficient"), self.template, NOW.isoformat())
        reflection = from_agent_output(self.output(evidence_ids=["policy.trim_fraction"]), closed, self.template, self.rules, NOW)
        with self.assertRaises(ReflectionError):
            validate_reflection(reflection, closed, self.template, self.rules, [])

    def test_tampered_content_missing_evidence_and_stale_policy_are_rejected(self):
        tampered = self.reflection()
        tampered["summary"] = "改過"
        with self.assertRaises(ReflectionError):
            validate_reflection(tampered, self.brief, self.template, self.rules, [])
        with self.assertRaises(ReflectionError):
            validate_reflection(self.reflection(evidence_ids=["cal.h1.not_there"]), self.brief, self.template, self.rules, [])
        changed = dict(self.template, trim_fraction="0.4")
        with self.assertRaises(ReflectionError):
            validate_reflection(self.reflection(), self.brief, changed, self.rules, [])

    def test_too_many_or_duplicate_proposals_are_rejected(self):
        output = self.output()
        output["proposals"] = output["proposals"] * 2
        reflection = from_agent_output(output, self.brief, self.template, self.rules, NOW)
        with self.assertRaises(ReflectionError):
            validate_reflection(reflection, self.brief, self.template, self.rules, [])

    def test_cooldown_blocks_a_recent_knob_until_enough_new_days(self):
        history = [{"action": "apply", "knobs": ["conviction_multipliers.high"], "decision_days": 5}]
        with self.assertRaises(ReflectionError):
            validate_reflection(self.reflection(), self.brief, self.template, self.rules, history)
        history[0]["decision_days"] = 2
        validate_reflection(self.reflection(), self.brief, self.template, self.rules, history)


class EvidenceTests(Fixture):
    def test_policy_value_alone_is_not_evidence(self):
        reflection = self.reflection(evidence_ids=["policy.conviction_multipliers.high"])
        with self.assertRaises(ReflectionError):
            validate_reflection(reflection, self.brief, self.template, self.rules, [])


class ApplyTests(Fixture):
    def save(self, reflection=None):
        return save_reflection(self.repository, reflection or self.reflection(), self.brief)

    def replayer(self, candidate_nav="101", candidate_rejected=0):
        def replay(path, replay_id):
            return replay_body(candidate_nav, candidate_rejected) if replay_id.endswith("candidate") else replay_body()
        return replay

    def test_replay_gate(self):
        self.assertTrue(replay_gate(replay_body(), replay_body("100"))["passed"])
        self.assertFalse(replay_gate(replay_body(), replay_body("99"))["passed"])
        self.assertFalse(replay_gate(replay_body(), replay_body(rejected=1))["passed"])
        self.assertFalse(replay_gate(replay_body(), replay_body(status="failed"))["passed"])
        self.assertFalse(replay_gate(replay_body(), replay_body(days=0))["passed"])

    def test_apply_writes_new_policy_atomically_and_records_history(self):
        reflection_id = self.save()
        result = apply_reflection(self.tmp, reflection_id, self.replayer(), NOW, self.repository)
        self.assertTrue(result["applied"])
        live = self.live()
        self.assertEqual(live["position_sizing"]["conviction_multipliers"]["high"], "1.25")
        self.assertEqual(live["policy_id"], "strategy-2026-11-20-r1")
        self.assertTrue(live["available_at"].startswith("2026-11-20T20:30"))
        self.assertEqual(self.repository.history()[0]["policy_id_before"], self.template["policy_id"])
        self.assertEqual(list((self.tmp / "config").glob("*.tmp")), [])

    def test_failed_gate_leaves_config_untouched(self):
        reflection_id = self.save()
        before = (self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8")
        result = apply_reflection(self.tmp, reflection_id, self.replayer(candidate_nav="90"), NOW, self.repository)
        self.assertFalse(result["applied"])
        self.assertEqual((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8"), before)
        self.assertEqual(self.repository.history(), [])

    def test_replay_error_propagates_without_changing_config(self):
        reflection_id = self.save()
        before = (self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8")

        def broken(path, replay_id):
            raise ReflectionError("回放失敗")

        with self.assertRaises(ReflectionError):
            apply_reflection(self.tmp, reflection_id, broken, NOW, self.repository)
        self.assertEqual((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8"), before)

    def test_window_blocks_late_evening_apply_and_keeps_the_reflection(self):
        reflection_id = self.save()
        late = datetime(2026, 11, 20, 13, 0, tzinfo=timezone.utc)  # 台北 21:00
        result = apply_reflection(self.tmp, reflection_id, self.replayer(), NOW, self.repository, clock=lambda: late, enforce_window=True)
        self.assertFalse(result["applied"])
        self.assertEqual(self.live()["policy_id"], self.template["policy_id"])
        manual = apply_reflection(self.tmp, reflection_id, self.replayer(), NOW, self.repository, clock=lambda: late)
        self.assertTrue(manual["applied"])

    def test_template_changed_during_replay_is_not_overwritten(self):
        reflection_id = self.save()

        def replay(path, replay_id):
            changed = self.live()
            changed["trim_fraction"] = "0.4"
            (self.tmp / "config" / "decision_policy.json").write_text(json.dumps(changed), encoding="utf-8")
            return replay_body("101") if replay_id.endswith("candidate") else replay_body()

        result = apply_reflection(self.tmp, reflection_id, replay, NOW, self.repository)
        self.assertFalse(result["applied"])
        self.assertEqual(self.live()["trim_fraction"], "0.4")

    def test_busy_daily_lock_skips_apply_without_error(self):
        def busy():
            raise BlockingIOError("locked")

        result = apply_reflection(self.tmp, self.save(), self.replayer(), NOW, self.repository, write_lock=busy)
        self.assertFalse(result["applied"])
        self.assertEqual(self.live()["policy_id"], self.template["policy_id"])

    def test_only_one_apply_per_day(self):
        reflection_id = self.save()
        apply_reflection(self.tmp, reflection_id, self.replayer(), NOW, self.repository)
        second_brief = build_brief(report(), self.live(), NOW.isoformat())
        second = from_agent_output(self.output(knob="trim_fraction", proposed="0.4"),
                                   second_brief, self.live(), self.rules, datetime(2026, 11, 20, 12, 31, tzinfo=timezone.utc))
        save_reflection(self.repository, second, second_brief)
        result = apply_reflection(self.tmp, str(second["reflection_id"]), self.replayer(), NOW, self.repository)
        self.assertFalse(result["applied"])

    def test_rollback_restores_the_previous_file_and_refuses_after_manual_edit(self):
        original = (self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8")
        apply_reflection(self.tmp, self.save(), self.replayer(), NOW, self.repository)
        result = rollback(self.tmp, NOW, self.repository)
        self.assertEqual(result["policy_id"], self.template["policy_id"])
        self.assertEqual(json.loads((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8")), json.loads(original))
        with self.assertRaises(ReflectionError):
            rollback(self.tmp, NOW, self.repository)

    def test_rollback_refuses_when_live_file_was_edited_by_hand(self):
        apply_reflection(self.tmp, self.save(), self.replayer(), NOW, self.repository)
        live = self.live()
        live["trim_fraction"] = "0.4"  # 內容被改但 policy_id 沒變
        (self.tmp / "config" / "decision_policy.json").write_text(json.dumps(live), encoding="utf-8")
        with self.assertRaises(ReflectionError):
            rollback(self.tmp, NOW, self.repository)
        live["policy_id"] = "strategy-manual"
        (self.tmp / "config" / "decision_policy.json").write_text(json.dumps(live), encoding="utf-8")
        with self.assertRaises(ReflectionError):
            rollback(self.tmp, NOW, self.repository)


class RunAutoTests(Fixture):
    class Runner:
        def __init__(self, output=None):
            self.calls = 0
            self.output = output

        def run(self, task, run_dir):
            self.calls += 1
            return AgentCall(task.name, 0, self.output, 0.5)

    def test_closed_gate_never_calls_the_agent(self):
        closed = build_brief(report(status="insufficient"), self.template, NOW.isoformat())
        runner = self.Runner()
        with mock.patch.object(service, "make_brief", return_value=closed):
            result = run_auto(self.tmp, self.tmp / "db", self.tmp / "decisions", runner, NOW)
        self.assertEqual((result["status"], runner.calls), ("observe_only", 0))
        self.assertEqual(self.live()["position_sizing"]["conviction_multipliers"]["high"], "1.5")

    def test_open_gate_applies_a_valid_proposal(self):
        runner = self.Runner(self.output())
        with mock.patch.object(service, "make_brief", return_value=self.brief):
            result = run_auto(self.tmp, self.tmp / "db", self.tmp / "decisions", runner, NOW,
                              replayer=lambda path, replay_id: replay_body("101") if replay_id.endswith("candidate") else replay_body(),
                              clock=lambda: NOW)
        self.assertEqual(runner.calls, 1)
        self.assertTrue(result["apply"]["applied"])
        run_dir = next((self.tmp / "var" / "strategy_reflection" / "agent_inputs").iterdir())
        self.assertTrue((run_dir / "agent_output_raw.json").exists())

    def test_rejected_agent_output_is_kept_with_the_reason(self):
        runner = self.Runner(self.output(knob="slippage_bps", proposed="5"))
        with mock.patch.object(service, "make_brief", return_value=self.brief), self.assertRaises(ReflectionError):
            run_auto(self.tmp, self.tmp / "db", self.tmp / "decisions", runner, NOW)
        run_dir = next((self.tmp / "var" / "strategy_reflection" / "agent_inputs").iterdir())
        self.assertIn("slippage_bps", json.loads((run_dir / "rejected.json").read_text(encoding="utf-8"))["reason"])

    def test_forbidden_proposal_from_agent_stops_before_any_write(self):
        runner = self.Runner(self.output(knob="slippage_bps", proposed="5"))
        before = (self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8")
        with mock.patch.object(service, "make_brief", return_value=self.brief), self.assertRaises(ReflectionError):
            run_auto(self.tmp, self.tmp / "db", self.tmp / "decisions", runner, NOW)
        self.assertEqual((self.tmp / "config" / "decision_policy.json").read_text(encoding="utf-8"), before)

    def test_observe_only_reflection_is_sealed_and_verifiable(self):
        reflection = observe_only(self.brief, NOW)
        run_id = save_reflection(self.repository, reflection, self.brief)
        loaded, _ = service.load_reflection(self.repository, run_id)
        self.assertEqual(loaded["status"], "observe_only")


class IsolationTests(unittest.TestCase):
    def test_decision_chain_never_imports_strategy_reflection(self):
        pattern = re.compile(r"(import|from)\s+(etf_agent\.|\.)*strategy_reflection|from etf_agent import .*strategy_reflection")
        paths = [*(ROOT / "src" / "etf_agent" / "decision").rglob("*.py"), *(ROOT / "src" / "etf_agent" / "automation").rglob("*.py"),
                 ROOT / "scripts" / "run_daily_pipeline.py"]
        self.assertGreater(len(paths), 5)
        for path in paths:
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), str(path))


if __name__ == "__main__":
    unittest.main()
