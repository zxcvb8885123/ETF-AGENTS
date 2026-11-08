"""Skill／指令版本指紋：每次 Agent 呼叫的 skill_sha256／prompt_sha256 與整個 run 的 skill_versions。

只新增執行紀錄欄位：Decision artifact、manifest 與內容雜湊不含這些欄位，舊 run 重建不受影響。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import AgentTask
from etf_agent.automation.daily_pipeline import AgentCall, DailyDecisionPipeline
from etf_agent.automation.skill_digest import (
    ROOT_PLACEHOLDER,
    RUN_DIR_PLACEHOLDER,
    linked_references,
    prompt_sha256,
    skill_digest,
    skill_paths_in_prompt,
    skill_versions,
)
from etf_agent.core import canonical_sha256
from etf_agent.decision import DecisionRepository

from test_daily_pipeline import ROOT, FakeRunner, PipelineWorld, team_outputs


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class SkillDigestUnitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.skill_dir = self.root / "tradeagent" / "trading" / "trader"
        (self.skill_dir / "references").mkdir(parents=True)
        self.skill = self.skill_dir / "SKILL.md"
        self.skill.write_text(
            "# trader\n見 [資料規則](references/data-rules.md) 與 [排序](references/ranking.md)，"
            "再提一次 [資料規則](references/data-rules.md)。\n",
            encoding="utf-8",
        )
        self.rules = self.skill_dir / "references" / "data-rules.md"
        self.rules.write_text("規則 v1\n", encoding="utf-8")
        # ranking.md 刻意不建立：連結存在但檔案缺少時雜湊為 None，不隱藏。

    def tearDown(self):
        self.directory.cleanup()

    def test_prompt_paths_are_found_in_order_and_deduplicated(self):
        other = self.root / "tradeagent" / "trading" / "trader" / "references" / "ranking.md"
        prompt = "你是 $trader。先讀 %s 與 %s，再讀 %s（第二次）。" % (self.skill, other, self.skill)
        self.assertEqual(skill_paths_in_prompt(self.root, prompt), [self.skill, other])
        self.assertEqual(skill_paths_in_prompt(self.root, "沒有任何 Skill 路徑"), [])
        # 專案外或非 tradeagent/ 的 .md 不算 Skill 檔。
        self.assertEqual(skill_paths_in_prompt(self.root, "先讀 %s" % (self.root / "docs" / "x.md")), [])

    def test_linked_references_follow_markdown_links_once(self):
        self.assertEqual(
            linked_references(self.skill),
            [self.skill_dir / "references" / "data-rules.md", self.skill_dir / "references" / "ranking.md"],
        )
        self.assertEqual(linked_references(self.root / "missing" / "SKILL.md"), [])

    def test_digest_hashes_skill_and_linked_references(self):
        digest = skill_digest(self.root, "先讀 %s，再讀摘要。" % self.skill)
        files = digest["skill_files"]
        self.assertEqual(
            list(files),
            ["tradeagent/trading/trader/SKILL.md", "tradeagent/trading/trader/references/data-rules.md",
             "tradeagent/trading/trader/references/ranking.md"],
        )
        self.assertEqual(files["tradeagent/trading/trader/SKILL.md"], sha256_of(self.skill))
        self.assertEqual(files["tradeagent/trading/trader/references/data-rules.md"], sha256_of(self.rules))
        self.assertIsNone(files["tradeagent/trading/trader/references/ranking.md"])
        self.assertEqual(digest["skill_sha256"], canonical_sha256(files))

    def test_digest_changes_when_a_reference_changes(self):
        prompt = "先讀 %s。" % self.skill
        before = skill_digest(self.root, prompt)
        self.rules.write_text("規則 v2\n", encoding="utf-8")
        after = skill_digest(self.root, prompt)
        self.assertNotEqual(before["skill_sha256"], after["skill_sha256"])
        self.assertEqual(before["skill_files"]["tradeagent/trading/trader/SKILL.md"],
                         after["skill_files"]["tradeagent/trading/trader/SKILL.md"])

    def test_prompt_without_skill_paths_has_no_digest(self):
        digest = skill_digest(self.root, "只讀 brief，回覆 JSON")
        self.assertIsNone(digest["skill_sha256"])
        self.assertEqual(digest["skill_files"], {})

    def test_prompt_sha256_is_stable_across_run_dirs_and_roots(self):
        template = "你是 $trader。先讀 %s/tradeagent/trading/trader/SKILL.md，再讀 %s/brief_trader_b0.json。"
        root_a, run_a = "/srv/a", "/srv/a/artifacts/daily/2026-11-05/daily-1"
        root_b, run_b = "/home/b/etf", "/home/b/etf/artifacts/daily/2026-11-06/daily-2"
        digest_a = prompt_sha256(template % (root_a, run_a), {run_a: RUN_DIR_PLACEHOLDER, root_a: ROOT_PLACEHOLDER})
        digest_b = prompt_sha256(template % (root_b, run_b), {run_b: RUN_DIR_PLACEHOLDER, root_b: ROOT_PLACEHOLDER})
        self.assertEqual(digest_a, digest_b)
        expected = hashlib.sha256((template % (ROOT_PLACEHOLDER, RUN_DIR_PLACEHOLDER)).encode("utf-8")).hexdigest()
        self.assertEqual(digest_a, expected)
        # 指令文字改變就改變。
        self.assertNotEqual(digest_a, prompt_sha256(template % (root_a, run_a) + "多一句。", {run_a: RUN_DIR_PLACEHOLDER, root_a: ROOT_PLACEHOLDER}))

    def test_skill_versions_merges_calls_and_flags_inconsistent_files(self):
        calls = [
            {"name": "technical_b0", "skill_files": {"tradeagent/a/SKILL.md": "aa"}},
            {"name": "failed", "attempt": 1, "transient_error": "timeout"},  # 沒有 skill_files 的舊紀錄照樣略過
            {"name": "trader_b0", "skill_files": {"tradeagent/t/SKILL.md": "t1", "tradeagent/t/references/data-rules.md": "r1"}},
            {"name": "trader_b1", "skill_files": {"tradeagent/t/SKILL.md": "t2", "tradeagent/t/references/data-rules.md": "r1"}},
        ]
        versions = skill_versions(calls)
        self.assertEqual(versions["files"], {
            "tradeagent/a/SKILL.md": "aa", "tradeagent/t/SKILL.md": "t2", "tradeagent/t/references/data-rules.md": "r1",
        })
        self.assertEqual(versions["sha256"], canonical_sha256(versions["files"]))
        self.assertEqual(versions["inconsistent"], ["tradeagent/t/SKILL.md"])
        self.assertEqual(skill_versions([]), {"files": {}, "sha256": None})
        self.assertNotIn("inconsistent", skill_versions(calls[:3]))


class PipelineSkillVersionTests(unittest.TestCase):
    """每日鏈每次呼叫都帶 Skill／指令指紋；整個 run 彙整在 PipelineResult.skill_versions；Decision artifact 不含它們。"""

    def test_agent_calls_carry_skill_and_prompt_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = FakeRunner(team_outputs())
            result = world.run(runner)
            self.assertEqual(result.status, "completed", result.errors)

            calls = {call["name"]: call for call in result.agent_calls}
            self.assertEqual(
                sorted(calls), ["bear_b0", "bull_b0", "cash_stance", "fundamental_b0", "review_r0", "technical_b0", "trader_b0"],
            )
            for call in result.agent_calls:
                self.assertRegex(call["prompt_sha256"], r"^[0-9a-f]{64}$")
                self.assertRegex(call["skill_sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(call["skill_files"], call["name"])
                self.assertEqual(call["skill_sha256"], canonical_sha256(call["skill_files"]))
            trader = calls["trader_b0"]
            self.assertEqual(
                trader["skill_files"]["tradeagent/trading/trader/SKILL.md"],
                sha256_of(ROOT / "tradeagent" / "trading" / "trader" / "SKILL.md"),
            )
            self.assertEqual(
                trader["skill_files"]["tradeagent/trading/trader/references/data-rules.md"],
                sha256_of(ROOT / "tradeagent" / "trading" / "trader" / "references" / "data-rules.md"),
            )
            self.assertEqual(
                calls["bull_b0"]["skill_files"]["tradeagent/research/stock-research/bull-researcher/SKILL.md"],
                sha256_of(ROOT / "tradeagent" / "research" / "stock-research" / "bull-researcher" / "SKILL.md"),
            )
            # 指令雜湊不含 run 目錄與專案根目錄：同一份基礎指令在另一個 run 目錄算出相同值。
            prompt = next(text for name, text in runner.prompts if name == "trader_b0")
            self.assertIn(str(world.run_dir), prompt)
            self.assertEqual(
                trader["prompt_sha256"],
                prompt_sha256(prompt, {str(world.run_dir): RUN_DIR_PLACEHOLDER, str(ROOT): ROOT_PLACEHOLDER}),
            )
            self.assertNotEqual(trader["prompt_sha256"], hashlib.sha256(prompt.encode("utf-8")).hexdigest())

            # run 層級彙整：每個被讀的 Skill 檔一個版本，與逐次呼叫一致。
            versions = result.skill_versions
            self.assertEqual(versions["sha256"], canonical_sha256(versions["files"]))
            self.assertNotIn("inconsistent", versions)
            for call in result.agent_calls:
                for relative, digest in call["skill_files"].items():
                    self.assertEqual(versions["files"][relative], digest)

            # Decision run 的任何 artifact 都不含這些欄位：manifest／內容雜湊與舊 run 重建不受影響。
            run_dir = DecisionRepository(world.repository).verify("decision-fixture")
            for path in sorted(run_dir.glob("*.json")):
                text = path.read_text(encoding="utf-8")
                for forbidden in ("skill_sha256", "prompt_sha256", "skill_files", "skill_versions"):
                    self.assertNotIn(forbidden, text, "%s 不應含 %s" % (path.name, forbidden))

    def test_transient_failures_and_prompts_without_skills_still_record(self):
        class ScriptedRunner:
            model = "stub-model"

            def run(self, task, run_dir):
                return AgentCall(task.name, 0, {"items": []}, 0.0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = DailyDecisionPipeline(root, root, ScriptedRunner(), root / "decisions", log=lambda _: None)
            task = AgentTask("trader_b0", "只讀 brief，回覆 JSON", {"type": "object"})
            pipeline.agent_step(task, lambda output: dict(output), lambda artifact: [])
            (call,) = pipeline.calls
            self.assertIsNone(call["skill_sha256"])
            self.assertEqual(call["skill_files"], {})
            self.assertEqual(call["prompt_sha256"], hashlib.sha256("只讀 brief，回覆 JSON".encode("utf-8")).hexdigest())
            self.assertEqual(pipeline.skill_versions(), {"files": {}, "sha256": None})


class LegacyRecordCompatibilityTests(unittest.TestCase):
    """舊 pipeline.json 的 agent_calls 沒有這些欄位：彙整與讀取都不得因此失敗。"""

    def test_old_agent_calls_without_digests_aggregate_to_empty(self):
        legacy = json.loads(json.dumps([
            {"name": "trader_b0", "attempt": 1, "estimated_usage_usd": 0.1, "duration_seconds": 3.0, "model": "x"},
        ]))
        self.assertEqual(skill_versions(legacy), {"files": {}, "sha256": None})


if __name__ == "__main__":
    unittest.main()
