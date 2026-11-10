"""估值 V2（position_sizing.valuation_facts）接入每日決策鏈的端到端測試：真實 pipeline 與真實 Finalizer。

- 估值資料夾與決策庫同在一個 artifacts 底下（``valuation_root_for``）；V1 收集器以假端點建立一個 run，模擬使用者日後核准
  「背景進簡報」的授權範圍。開閘門時基本面分析師與交易 Agent 的簡報多附 ``valuation_background``（citable=false），
  team_inputs 多一個選配鍵 ``valuation_facts``（不升版），Finalizer 以同一個資料夾整份重算後封存。
- 資料夾沒有 run 時事實整份 unavailable，決策照常完成（不讓估值缺漏拖垮當天決策）。
- 沒有閘門時簡報與 team_inputs 不含這些鍵。
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from etf_agent.data.valuation import ValuationCollector, reconcile
from etf_agent.decision.valuation_facts import APPROVED_LICENSE_SCOPES

from test_daily_pipeline import FakeRunner, PipelineWorld, gateless_policy_template, team_outputs
from test_valuation_collection import FakeFinMind, FakeOfficial, finmind, official


def valuation_template():
    template = gateless_policy_template()
    template["position_sizing"]["valuation_facts"] = {"version": "v1"}
    return template


def seed_store(root: Path) -> None:
    raw = official([("2330", "20.00", "5.00", "2.00"), ("2317", "12.00", "1.50", "4.00")], day="1150918")
    responses = {
        "2330": finmind("2330", [("2026-09-16", "21.00", "5.10", "2.0"), ("2026-09-17", "19.00", "4.90", "2.0"),
                                 ("2026-09-18", "20.00", "5.00", "2.00")]),
        "2317": finmind("2317", [("2026-09-17", "13.00", "1.60", "4.0"), ("2026-09-18", "12.00", "1.50", "4.00")]),
    }
    with patch.object(reconcile, "LICENSE_SCOPE", APPROVED_LICENSE_SCOPES[0]):
        ValuationCollector(root, FakeOfficial(raw), FakeFinMind(responses),
                           lambda: datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)).collect(["2330", "2317"])


class ValuationPipelineTests(unittest.TestCase):
    def run_world(self, directory: Path, template, seeded: bool):
        world = PipelineWorld(directory, approved_status=True, template=template)
        if seeded:
            seed_store(directory / "valuation_collection")
        # Finalizer 重建時同樣會以原始回應重建 run；授權範圍的模擬要涵蓋整個 pipeline。
        with patch.object(reconcile, "LICENSE_SCOPE", APPROVED_LICENSE_SCOPES[0]):
            result = world.run(FakeRunner(team_outputs()))
        return world, result

    def load(self, world, name):
        return json.loads((world.run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))

    def test_the_gate_puts_background_into_both_briefs_and_the_finalizer_rebuilds_it(self):
        with tempfile.TemporaryDirectory() as directory:
            world, result = self.run_world(Path(directory), valuation_template(), seeded=True)
            self.assertEqual(result.status, "completed", result.errors)
            facts = self.load(world, "valuation_facts")
            self.assertEqual(facts["data_status"], "available")
            self.assertEqual(facts["source"]["data_date"], "2026-09-18")
            fundamental = self.load(world, "brief_fundamental_b0")
            self.assertFalse(fundamental["valuation_background"]["citable"])
            self.assertEqual(sorted(fundamental["valuation_background"]["by_symbol"]), ["2317.TW", "2330.TW"])
            self.assertNotIn("valuation_background", self.load(world, "brief_technical_b0"))  # 只給基本面分析師
            trader = self.load(world, "brief_trader_b0")
            self.assertIn("valuation_background", trader)
            self.assertNotIn(facts["facts_id"], json.dumps(trader, ensure_ascii=False))
            team = self.load(world, "team_inputs")
            self.assertEqual(team["valuation_facts"], facts)
            self.assertEqual(team["schema_version"], "2.6")  # 選配鍵，不升版

    def test_without_any_run_the_facts_are_unavailable_and_the_decision_still_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            world, result = self.run_world(Path(directory), valuation_template(), seeded=False)
            self.assertEqual(result.status, "completed", result.errors)
            facts = self.load(world, "valuation_facts")
            self.assertEqual((facts["data_status"], facts["data_gaps"]), ("unavailable", ["NO_VALUATION_RUN"]))

    def test_without_the_gate_nothing_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            world, result = self.run_world(Path(directory), gateless_policy_template(), seeded=True)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertFalse((world.run_dir / "valuation_facts.json").exists())
            self.assertNotIn("valuation_background", self.load(world, "brief_fundamental_b0"))
            self.assertNotIn("valuation_facts", self.load(world, "team_inputs"))


if __name__ == "__main__":
    unittest.main()
