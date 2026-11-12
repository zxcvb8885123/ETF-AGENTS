"""交易背景封存（position_sizing.trader_background_record）接入每日決策鏈的端到端測試：真實 pipeline 與真實 Finalizer。

- 開閘門時 run 目錄多一個 ``trader_background.json``，team_inputs 多選配鍵 ``trader_background``（不升版），簡報的策略教訓由封存紀錄產生。
- 續跑時沿用封存檔，來源檔之後被改不影響簡報；封存檔被改壞則拒絕。
- 閘門與 team_inputs 鍵必須雙向同時存在（Finalizer 拒絕）。
- 沒有閘門時沒有封存檔、team_inputs 沒有該鍵、簡報照舊讀檔。
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, build_team_inputs
from etf_agent.decision import DecisionFinalizer
from etf_agent.decision.strategy_lessons import lessons_brief, load_strategy_lessons, seal_lessons
from etf_agent.decision.thesis_verdicts import seal_verdicts

from test_daily_pipeline import ROOT, FakeRunner, PipelineWorld, gateless_policy_template, team_outputs

LESSON_A = "買進後連續下跌時先檢查買進理由是否仍成立"
LESSON_B = "加碼前先確認多頭論點仍有新的依據支持"
BACKGROUND = "etf_agent.decision.trader_background"


def background_template():
    template = gateless_policy_template()
    template["position_sizing"]["trader_background_record"] = {"version": "v1"}
    return template


def finalizer_errors():
    """Finalizer 驗證不過時只把決策標成 rejected、不丟例外：以旁路收集它的錯誤訊息。"""
    captured = []
    original = DecisionFinalizer.validate_inputs

    def spy(self, *args, **kwargs):
        errors = original(self, *args, **kwargs)
        captured.extend(errors)
        return errors

    return captured, patch.object(DecisionFinalizer, "validate_inputs", spy)


def lessons(text):
    return seal_lessons("lessons-x", "2026-09-10T00:00:00+00:00", [{"text": text, "evidence_ids": ["e1"]}])


def verdicts():
    return seal_verdicts("verdicts-x", "2026-09-15T00:00:00+00:00", {"a": {"symbol": "2317.TW", "verdict": "weakened", "summary": "毛利率連續下滑，買進理由轉弱"}})


class BackgroundPipelineTests(unittest.TestCase):
    def load(self, world, name):
        return json.loads((world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))

    def sources(self, lesson_text):
        return (patch(BACKGROUND + ".load_strategy_lessons_file", return_value=lessons(lesson_text)),
                patch(BACKGROUND + ".load_thesis_verdicts_file", return_value=verdicts()))

    def run_world(self, directory, template, lesson_text=LESSON_A):
        world = PipelineWorld(Path(directory), approved_status=True, template=template)
        first, second = self.sources(lesson_text)
        with first, second:
            result = world.run(FakeRunner(team_outputs()))
        return world, result

    def test_the_gate_seals_the_sources_and_the_brief_uses_the_sealed_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            world, result = self.run_world(directory, background_template())
            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual(result.decision_status, "approved")
            record = self.load(world, "trader_background")
            self.assertEqual(record["strategy_lessons"], lessons(LESSON_A))
            self.assertEqual(record["thesis_verdicts"], verdicts())
            team = self.load(world, "team_inputs")
            self.assertEqual(team["trader_background"], record)
            self.assertEqual(team["schema_version"], "2.6")  # 選配鍵，不升版
            brief = self.load(world, "brief_trader_b0")
            self.assertEqual(brief["strategy_lessons"], lessons_brief({"texts": [LESSON_A]}))
            self.assertNotIn("lessons-x", json.dumps(brief, ensure_ascii=False))  # 簡報沒有識別碼

    def test_without_the_gate_nothing_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True, template=gateless_policy_template())
            first, second = self.sources(LESSON_A)
            with first as lessons_patch, second as verdicts_patch:
                result = world.run(FakeRunner(team_outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            self.assertFalse((world.run_dir / "trader_background.json").exists())
            self.assertNotIn("trader_background", self.load(world, "team_inputs"))
            lessons_patch.assert_not_called()  # 沒有閘門時封存程式完全不走
            verdicts_patch.assert_not_called()
            brief = self.load(world, "brief_trader_b0")
            live = lessons_brief(load_strategy_lessons(ROOT, "2026-09-19T00:00:00+00:00"))
            self.assertEqual(brief.get("strategy_lessons"), live)  # 簡報照舊讀檔

    def test_the_gate_without_the_team_inputs_key_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True, template=background_template())

            def without_key(*args, **kwargs):
                return {key: value for key, value in build_team_inputs(*args, **kwargs).items() if key != "trader_background"}

            captured, spy = finalizer_errors()
            first, second = self.sources(LESSON_A)
            with first, second, spy, patch("etf_agent.automation.daily_pipeline.build_team_inputs", side_effect=without_key):
                result = world.run(FakeRunner(team_outputs()))
            self.assertEqual(result.decision_status, "rejected")
            self.assertTrue(any("team_inputs.trader_background 同時存在" in error for error in captured), captured)

    def test_the_team_inputs_key_without_the_gate_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True, template=gateless_policy_template())

            def with_key(*args, **kwargs):
                team = build_team_inputs(*args, **kwargs)
                team["trader_background"] = {"version": "v1", "strategy_lessons": None, "thesis_verdicts": None, "content_sha256": "0" * 64}
                return team

            captured, spy = finalizer_errors()
            with spy, patch("etf_agent.automation.daily_pipeline.build_team_inputs", side_effect=with_key):
                result = world.run(FakeRunner(team_outputs()))
            self.assertEqual(result.decision_status, "rejected")
            self.assertTrue(any("team_inputs.trader_background 同時存在" in error for error in captured), captured)

    def test_resume_keeps_the_sealed_copy_after_the_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            world, result = self.run_world(directory, background_template(), LESSON_A)
            self.assertEqual(result.status, "completed", result.errors)
            sealed = self.load(world, "trader_background")
            first, second = self.sources(LESSON_B)  # 來源檔被改
            with first, second:
                pipeline = DailyDecisionPipeline(ROOT, world.run_dir, FakeRunner(team_outputs()), world.repository / "again",
                                                 log=lambda _: None, resume=True)
                result = pipeline.run(
                    world.paths["snapshot"], world.paths["account"], world.paths["trading_status"], world.paths["rules"],
                    world.paths["database"], world.paths["template"], world.paths["sectors"], "decision-fixture-2",
                )
            self.assertEqual(self.load(world, "trader_background"), sealed)
            self.assertEqual(self.load(world, "brief_trader_b0")["strategy_lessons"], lessons_brief({"texts": [LESSON_A]}))
            self.assertEqual(result.status, "completed", result.errors)

    def test_a_damaged_sealed_file_stops_the_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            world, result = self.run_world(directory, background_template(), LESSON_A)
            self.assertEqual(result.status, "completed", result.errors)
            record = self.load(world, "trader_background")
            record["strategy_lessons"]["lessons"][0]["text"] = "被改過的另一條教訓文字內容"
            (world.run_dir / "trader_background.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            first, second = self.sources(LESSON_A)
            with first, second:
                pipeline = DailyDecisionPipeline(ROOT, world.run_dir, FakeRunner(team_outputs()), world.repository / "again",
                                                 log=lambda _: None, resume=True)
                result = pipeline.run(
                    world.paths["snapshot"], world.paths["account"], world.paths["trading_status"], world.paths["rules"],
                    world.paths["database"], world.paths["template"], world.paths["sectors"], "decision-fixture-2",
                )
            self.assertEqual(result.status, "failed")
            self.assertTrue(any("trader_background.json" in error for error in result.errors), result.errors)


if __name__ == "__main__":
    unittest.main()
