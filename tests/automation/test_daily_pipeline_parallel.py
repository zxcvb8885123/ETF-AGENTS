import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from etf_agent.automation.agent_runner import AgentCall
from etf_agent.automation import daily_pipeline
from etf_agent.automation.batching import universe_batches
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline

from test_daily_pipeline import FakeRunner, PipelineWorld, ROOT, team_outputs


def run_pipeline(world, runner, max_workers=None):
    kwargs = {} if max_workers is None else {"max_workers": max_workers}
    pipeline = DailyDecisionPipeline(
        ROOT, world.run_dir, runner, world.repository, log=lambda _: None, **kwargs
    )
    return pipeline.run(
        world.paths["snapshot"], world.paths["account"], world.paths["trading_status"],
        world.paths["rules"], world.paths["database"], world.paths["template"], world.paths["sectors"],
        "decision-fixture",
    )


SYMBOLS = ("2317.TW", "2330.TW")


def one_symbol_batches(symbols, batch_size=50):
    """每批一檔，讓兩檔的 fixture 產生兩個批次，才能驗證批次之間的平行。"""
    return universe_batches(symbols, 1)


def per_symbol_outputs():
    """把每個分批 Agent 的預備輸出依股票拆成 b0（2317）與 b1（2330）。"""
    outputs = team_outputs()
    for stage in ("technical", "fundamental", "event", "bull", "bear", "trader"):
        full = outputs.pop("%s_b0" % stage)[0]
        for index, symbol in enumerate(SYMBOLS):
            outputs["%s_b%d" % (stage, index)] = [{"items": [item for item in full["items"] if item["symbol"] == symbol]}]
    return outputs


class SlowRunner(FakeRunner):
    """記錄同時執行中的呼叫數；技術分析師刻意最慢，驗證結果順序不依完成先後。"""

    def __init__(self, outputs, delay=0.05, fail_on=None):
        super().__init__(outputs)
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.trader_active = 0
        self.max_trader_active = 0
        self.delay = delay
        self.fail_on = fail_on

    def run(self, task, run_dir):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if task.name.startswith("trader"):
                self.trader_active += 1
                self.max_trader_active = max(self.max_trader_active, self.trader_active)
        try:
            time.sleep(self.delay * (3 if task.name == "technical_b0" else 1))
            if task.name == self.fail_on:
                raise OSError("%s 模擬執行失敗" % task.name)
            with self.lock:
                return super().run(task, run_dir)
        finally:
            with self.lock:
                self.active -= 1
                if task.name.startswith("trader"):
                    self.trader_active -= 1


class ParallelPipelineTests(unittest.TestCase):
    def test_parallel_matches_serial_artifacts(self):
        with tempfile.TemporaryDirectory() as serial_dir, tempfile.TemporaryDirectory() as parallel_dir:
            serial_world = PipelineWorld(Path(serial_dir), approved_status=True)
            parallel_world = PipelineWorld(Path(parallel_dir), approved_status=True)
            serial = run_pipeline(serial_world, FakeRunner(team_outputs()), max_workers=1)
            parallel = run_pipeline(parallel_world, SlowRunner(team_outputs()), max_workers=3)

            self.assertEqual(serial.status, "completed", serial.errors)
            self.assertEqual(parallel.status, "completed", parallel.errors)
            self.assertEqual(serial.decision_status, parallel.decision_status)
            self.assertEqual(serial.orders, parallel.orders)
            for name in (
                "analyst_technical", "analyst_fundamental", "analyst_event", "bull_stance", "bear_stance",
                "research_debate", "trade_decision",
            ):
                self.assertEqual(
                    json.loads((serial_world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8")),
                    json.loads((parallel_world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8")),
                    name,
                )

    def test_parallel_is_bounded_and_actually_concurrent(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = SlowRunner(team_outputs())
            result = run_pipeline(world, runner, max_workers=2)

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(runner.max_active, 2)

    def test_default_is_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = SlowRunner(team_outputs(), delay=0.01)
            result = run_pipeline(world, runner)

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(runner.max_active, 1)
        self.assertEqual(
            [name for name, _ in runner.prompts],
            ["technical_b0", "fundamental_b0", "bull_b0", "bear_b0", "trader_b0", "cash_stance", "review_r0"],
        )

    def test_agent_calls_keep_submission_order_not_finish_order(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            result = run_pipeline(world, SlowRunner(team_outputs()), max_workers=3)

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(
            [call["name"] for call in result.agent_calls],
            ["technical_b0", "fundamental_b0", "bull_b0", "bear_b0", "trader_b0", "cash_stance", "review_r0"],
        )

    def test_failure_in_one_task_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            result = run_pipeline(world, SlowRunner(team_outputs(), fail_on="fundamental_b0"), max_workers=3)
            decision_saved = (world.repository / "decision-fixture").exists()

        self.assertEqual(result.status, "failed")
        self.assertIn("fundamental_b0", result.errors[0])
        self.assertFalse(decision_saved)

    def test_trader_batches_run_in_parallel_and_match_serial(self):
        with mock.patch.object(daily_pipeline, "universe_batches", one_symbol_batches), \
                tempfile.TemporaryDirectory() as serial_dir, tempfile.TemporaryDirectory() as parallel_dir:
            serial_world = PipelineWorld(Path(serial_dir), approved_status=True)
            parallel_world = PipelineWorld(Path(parallel_dir), approved_status=True)
            serial = run_pipeline(serial_world, FakeRunner(per_symbol_outputs()), max_workers=1)
            runner = SlowRunner(per_symbol_outputs(), fail_on=None)
            parallel = run_pipeline(parallel_world, runner, max_workers=2)
            trader_names = [call["name"] for call in parallel.agent_calls if call["name"].startswith("trader")]
            serial_trade = json.loads((serial_world.run_dir / "trade_decision.json").read_text(encoding="utf-8"))
            parallel_trade = json.loads((parallel_world.run_dir / "trade_decision.json").read_text(encoding="utf-8"))

        self.assertEqual(serial.status, "completed", serial.errors)
        self.assertEqual(parallel.status, "completed", parallel.errors)
        self.assertEqual(serial_trade, parallel_trade)
        self.assertEqual(serial.orders, parallel.orders)
        self.assertEqual(trader_names, ["trader_b0", "trader_b1"])
        self.assertEqual(runner.max_trader_active, 2)

    def test_trader_stage_is_serial_when_workers_is_one(self):
        with mock.patch.object(daily_pipeline, "universe_batches", one_symbol_batches), \
                tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = SlowRunner(per_symbol_outputs(), delay=0.01)
            result = run_pipeline(world, runner, max_workers=1)

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(runner.max_trader_active, 1)

    def test_stage_durations_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            result = run_pipeline(world, FakeRunner(team_outputs()), max_workers=1)

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(
            list(result.stage_durations), ["analysts", "research", "trader", "cash_stance", "allocation_review"]
        )
        self.assertTrue(all(isinstance(value, float) and value >= 0 for value in result.stage_durations.values()))

    def test_max_workers_must_be_between_one_and_eight(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            for value in (0, 9, -1):
                with self.assertRaises(ValueError):
                    DailyDecisionPipeline(
                        ROOT, world.run_dir, FakeRunner({}), world.repository, log=lambda _: None, max_workers=value
                    )

    def test_resume_works_with_parallel_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            first = run_pipeline(world, SlowRunner(team_outputs(), fail_on="bear_b0"), max_workers=3)
            self.assertEqual(first.status, "failed")

            remaining = FakeRunner({
                name: items for name, items in team_outputs().items()
                if name in {"bear_b0", "trader_b0", "cash_stance", "review_r0"}
            })
            pipeline = DailyDecisionPipeline(
                ROOT, world.run_dir, remaining, world.repository, log=lambda _: None, resume=True, max_workers=3,
            )
            result = pipeline.run(
                world.paths["snapshot"], world.paths["account"], world.paths["trading_status"],
                world.paths["rules"], world.paths["database"], world.paths["template"], world.paths["sectors"],
                "decision-fixture",
            )

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(
            sorted(name for name, _ in remaining.prompts), ["bear_b0", "cash_stance", "review_r0", "trader_b0"]
        )


if __name__ == "__main__":
    unittest.main()
