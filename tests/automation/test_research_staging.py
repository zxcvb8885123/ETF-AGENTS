"""研究階段輸出暫存（Codex 型執行器）：多空原始輸出在兩方全部完成前不得落地到磁碟。"""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from etf_agent.automation import team_stages
from etf_agent.automation.agent_runner import AgentCall, AgentTask, DailyPipelineError, TransientAgentError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline

from test_daily_pipeline import FakeRunner, PipelineWorld, ROOT, team_outputs
from test_daily_pipeline_parallel import one_symbol_batches, per_symbol_outputs


RESEARCH = ("bull", "bear")
RESIDUE_PATTERNS = ("bull_*", "bear_*", "research_debate*", "debate*", "intent*", "trade_decision*", "trader_*", "codex-agent-*")


def residue(run_dir):
    return sorted({path.name for pattern in RESIDUE_PATTERNS for path in run_dir.glob(pattern)})


class StagedRunner(FakeRunner):
    """模擬 Codex：不強制拒讀。研究員呼叫當下記錄 run 目錄裡看得到的多空輸出，並確認任務宣告了暫存隔離。"""

    enforces_deny_reads = False

    def __init__(self, outputs, run_dir, fail_on=None, delay=0.0):
        super().__init__(outputs)
        self.run_dir = run_dir
        self.fail_on = fail_on
        self.delay = delay
        self.seen = {}
        self.lock = threading.Lock()

    def run(self, task, run_dir):
        if task.name.split("_", 1)[0] in RESEARCH:
            if not task.stage_isolated or not task.deny_reads:
                raise AssertionError("研究任務必須在暫存內執行並帶 deny_reads：%s" % task.name)
            with self.lock:
                self.seen[task.name] = residue(self.run_dir)
            if self.delay:
                time.sleep(self.delay)
            if task.name == self.fail_on:
                raise TransientAgentError("%s 模擬暫時性失敗" % task.name)
        with self.lock:
            return super().run(task, run_dir)


def pipeline_for(world, runner, resume=False, max_workers=1, **kwargs):
    return DailyDecisionPipeline(
        ROOT, world.run_dir, runner, world.repository, log=lambda _: None, resume=resume,
        max_workers=max_workers, transient_retries=0, **kwargs,
    )


def run(world, pipeline):
    return pipeline.run(
        world.paths["snapshot"], world.paths["account"], world.paths["trading_status"],
        world.paths["rules"], world.paths["database"], world.paths["template"], world.paths["sectors"],
        "decision-fixture",
    )


class ResearchStagingTests(unittest.TestCase):
    def test_peer_outputs_do_not_exist_on_disk_while_researchers_run(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = StagedRunner(team_outputs(), world.run_dir)
            result = run(world, pipeline_for(world, runner))
            written = residue(world.run_dir)

        self.assertEqual(result.status, "completed", result.errors)
        # 兩個研究員呼叫當下，run 目錄都沒有任何多空輸出、辯論或後段產物，也沒有 Codex 暫存目錄。
        self.assertEqual(runner.seen, {"bull_b0": [], "bear_b0": []})
        # 兩方全部完成後才一次落地。
        self.assertIn("bull_b0_raw_1.json", written)
        self.assertIn("bear_b0_raw_1.json", written)
        self.assertIn("research_debate.json", written)

    def test_parallel_researchers_never_see_each_others_raw_output(self):
        with mock.patch.object(team_stages, "universe_batches", one_symbol_batches), \
                tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = StagedRunner(per_symbol_outputs(), world.run_dir, delay=0.05)
            result = run(world, pipeline_for(world, runner, max_workers=3))
            written = residue(world.run_dir)

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(sorted(runner.seen), ["bear_b0", "bear_b1", "bull_b0", "bull_b1"])
        self.assertTrue(all(files == [] for files in runner.seen.values()), runner.seen)
        for name in ("bull_b0", "bull_b1", "bear_b0", "bear_b1"):
            self.assertIn("%s_raw_1.json" % name, written)

    def test_failed_research_discards_every_staged_output(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = StagedRunner(team_outputs(), world.run_dir, fail_on="bear_b0")
            result = run(world, pipeline_for(world, runner))
            leftovers = residue(world.run_dir)

        self.assertEqual(result.status, "failed")
        # bull_b0 已成功，但 bear_b0 失敗：整批丟棄，run 目錄不留任何一方的輸出。
        self.assertEqual(leftovers, [])
        self.assertIn("bull_b0", [name for name, _ in runner.prompts])

    def test_resume_after_failed_research_reruns_both_sides(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            first = run(world, pipeline_for(world, StagedRunner(team_outputs(), world.run_dir, fail_on="bear_b0")))
            self.assertEqual(first.status, "failed")
            remaining = {name: items for name, items in team_outputs().items()
                         if name in {"bull_b0", "bear_b0", "trader_b0", "cash_stance", "review_r0"}}
            runner = StagedRunner(remaining, world.run_dir)
            result = run(world, pipeline_for(world, runner, resume=True))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(sorted(name for name, _ in runner.prompts),
                         ["bear_b0", "bull_b0", "cash_stance", "review_r0", "trader_b0"])
        self.assertEqual(runner.seen, {"bull_b0": [], "bear_b0": []})

    def test_resume_after_trader_failure_reuses_research_without_launching_researchers(self):
        class TraderFails(StagedRunner):
            def run(self, task, run_dir):
                if task.name == "trader_b0":
                    raise DailyPipelineError("trader_b0 模擬失敗")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            first = run(world, pipeline_for(world, TraderFails(team_outputs(), world.run_dir)))
            self.assertEqual(first.status, "failed")
            self.assertTrue((world.run_dir / "bull_b0_raw_1.json").exists())
            remaining = {name: items for name, items in team_outputs().items()
                         if name in {"trader_b0", "cash_stance", "review_r0"}}
            runner = StagedRunner(remaining, world.run_dir)
            result = run(world, pipeline_for(world, runner, resume=True))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(runner.seen, {})  # 研究員沒有被啟動
        self.assertNotIn("bull_b0", [name for name, _ in runner.prompts])
        self.assertNotIn("bear_b0", [name for name, _ in runner.prompts])

    def test_resume_with_only_one_side_on_disk_is_refused_without_launching(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            (world.run_dir / "decision_chain.json").write_text(
                json.dumps({"schema_version": "2.1", "event_analysis_mode": "integrated_analyst"}), encoding="utf-8")
            (world.run_dir / "bull_b0_raw_1.json").write_text(json.dumps({"items": []}), encoding="utf-8")
            runner = StagedRunner(team_outputs(), world.run_dir)
            result = run(world, pipeline_for(world, runner, resume=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("研究階段輸出暫存", result.errors[0])
        self.assertNotIn("bull_b0", [name for name, _ in runner.prompts])
        self.assertNotIn("bear_b0", [name for name, _ in runner.prompts])

    def test_resume_with_stale_downstream_files_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            (world.run_dir / "debate.json").write_text("{}", encoding="utf-8")
            runner = StagedRunner(team_outputs(), world.run_dir)
            result = run(world, pipeline_for(world, runner, resume=True))

        self.assertEqual(result.status, "failed")
        self.assertIn("debate.json", result.errors[0])
        self.assertNotIn("bull_b0", [name for name, _ in runner.prompts])

    def test_resume_refuses_to_rerun_when_existing_outputs_no_longer_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            first = run(world, pipeline_for(world, StagedRunner(team_outputs(), world.run_dir)))
            self.assertEqual(first.status, "completed", first.errors)
            stale = {"items": [{"symbol": "2317.TW", "strength": "none", "claims": [], "invalidation_conditions": []}]}
            (world.run_dir / "bull_b0_raw_1.json").write_text(json.dumps(stale), encoding="utf-8")
            runner = StagedRunner(team_outputs(), world.run_dir)
            result = run(world, pipeline_for(world, runner, resume=True))
            raw_files = sorted(path.name for path in world.run_dir.glob("bull_b0_raw_*.json"))

        self.assertEqual(result.status, "failed")
        self.assertIn("全部驗證失敗", result.errors[0])
        self.assertNotIn("bull_b0", [name for name, _ in runner.prompts])
        self.assertEqual(raw_files, ["bull_b0_raw_1.json"])

    def test_non_resume_run_in_directory_with_research_outputs_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            first = run(world, pipeline_for(world, StagedRunner(team_outputs(), world.run_dir)))
            self.assertEqual(first.status, "completed", first.errors)
            runner = StagedRunner(team_outputs(), world.run_dir)
            result = run(world, pipeline_for(world, runner))

        self.assertEqual(result.status, "failed")
        self.assertIn("非續跑", result.errors[0])
        self.assertNotIn("bull_b0", [name for name, _ in runner.prompts])

    def test_validation_retries_inside_staging_keep_raw_numbering(self):
        bad = {"items": [{"symbol": "2317.TW", "strength": "none", "claims": [], "invalidation_conditions": []}]}
        outputs = team_outputs()
        outputs["bull_b0"] = [bad, outputs["bull_b0"][0]]
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = StagedRunner(outputs, world.run_dir)
            result = run(world, pipeline_for(world, runner))
            raw_files = sorted(path.name for path in world.run_dir.glob("bull_b0_raw_*.json"))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(raw_files, ["bull_b0_raw_1.json", "bull_b0_raw_2.json"])

    def test_deny_reads_task_outside_staging_is_refused_for_non_enforcing_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = StagedRunner({}, world.run_dir)
            pipeline = pipeline_for(world, runner)
            task = AgentTask("bull_b0", "x", {"type": "object"}, deny_reads=("bear_*",))
            with self.assertRaises(DailyPipelineError) as raised:
                pipeline.agent_step(task, lambda output: output, lambda artifact: [])
            self.assertNotIsInstance(raised.exception, TransientAgentError)
            self.assertEqual(runner.prompts, [])

    def test_researchers_carry_deny_read_directories_and_others_do_not(self):
        from etf_agent.automation.agent_runner import RESEARCH_DENY_DIRECTORIES

        class Recording(StagedRunner):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.tasks = {}

            def run(self, task, run_dir):
                self.tasks[task.name] = task
                return super().run(task, run_dir)

        outputs = team_outputs()
        bad = {"items": [{"symbol": "2317.TW", "strength": "none", "claims": [], "invalidation_conditions": []}]}
        outputs["bull_b0"] = [bad, outputs["bull_b0"][0]]  # 驗證失敗後重試的任務仍須帶這個欄位
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = Recording(outputs, world.run_dir)
            result = run(world, pipeline_for(world, runner))

        self.assertEqual(result.status, "completed", result.errors)
        for name in ("bull_b0", "bear_b0"):
            self.assertEqual(tuple(runner.tasks[name].deny_read_directories), RESEARCH_DENY_DIRECTORIES)
            self.assertIn("pipeline.json", runner.tasks[name].deny_reads)
        others = [task for name, task in runner.tasks.items() if name.split("_", 1)[0] not in RESEARCH]
        self.assertTrue(others)
        for task in others:
            self.assertEqual(tuple(task.deny_read_directories), (), task.name)

    def test_directory_only_deny_task_outside_staging_is_refused_for_non_enforcing_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = StagedRunner({}, world.run_dir)
            pipeline = pipeline_for(world, runner)
            task = AgentTask("bull_b0", "x", {"type": "object"}, deny_read_directories=("artifacts/reports",))
            with self.assertRaises(DailyPipelineError):
                pipeline.agent_step(task, lambda output: output, lambda artifact: [])
            self.assertEqual(runner.prompts, [])

    def test_enforcing_runner_keeps_immediate_raw_files(self):
        """Claude／OpenCode 型（能強制拒讀）：行為不變，原始輸出照常即時落地、不經暫存。"""

        class Probe(FakeRunner):
            def __init__(self, outputs, run_dir):
                super().__init__(outputs)
                self.run_dir = run_dir
                self.bull_visible_to_bear = None

            def run(self, task, run_dir):
                if task.name == "bear_b0":
                    self.bull_visible_to_bear = (self.run_dir / "bull_b0_raw_1.json").exists()
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = Probe(team_outputs(), world.run_dir)
            result = run(world, pipeline_for(world, runner))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertTrue(runner.bull_visible_to_bear)


if __name__ == "__main__":
    unittest.main()
