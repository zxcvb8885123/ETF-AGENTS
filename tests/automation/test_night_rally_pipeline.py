"""夜盤大漲放行進攻（position_sizing.night_rally_override）的整條每日決策鏈測試。

情境：全市場情緒（恐懼貪婪指標）為 negative、台指期夜盤（overnight-market 1.3 口徑）+9%。
- 有閘門、風險 Agent 選 aggressive 並引用夜盤證據：即時驗證、Finalizer 與重建都通過。
- 有閘門但沒引用夜盤證據：aggressive 仍被護欄擋下（修正循環用盡即失敗）。
- 沒有閘門：aggressive 照舊被擋（逐位元維持舊行為）。
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from etf_agent.decision import MARKET_REGIME_EVIDENCE_ID, DecisionRepository

from test_daily_pipeline import STANCE, FakeRunner, PipelineWorld, team_outputs

GATE = {"version": "v1", "min_night_change_pct": "5"}


def _negative_fear_greed(directory, world):
    """恐懼貪婪：沿用 test_market_fear_greed 的抓取；若 fixture 不是 negative 就略過（本測試要的是負面情緒）。"""
    from etf_agent.perception.market_fear_greed.service import _store
    from test_market_fear_greed import approvals, captures
    root, approval_path = Path(directory) / "fear_greed", Path(directory) / "fear_greed_sources.json"
    _store(root).save("fear-greed-20260919T120000Z-fixture", {"captures": {"items": captures()}})
    approval_path.write_text(json.dumps({"sources": approvals()}), encoding="utf-8")
    return root, approval_path


def _overnight_with_night(directory, world, change="9"):
    """週五 2026-09-18 收盤後的隔夜背景，加上週五夜盤（CDate=開盤日 20260918、CTime 045959，週六 05:00 結束）。"""
    from etf_agent.perception.overnight_market.service import _store
    from test_overnight_market import NIGHT_ROWS, approvals, captures, night_capture
    world.snapshot["latest_trade_date"] = "2026-09-18"
    for row in world.snapshot["latest_prices"]:
        row["trade_date"] = "2026-09-18"
    world.paths["snapshot"].write_text(json.dumps(world.snapshot, ensure_ascii=False), encoding="utf-8")
    cutoff = datetime.fromisoformat(world.snapshot["decision_cutoff"])
    available = (cutoff - timedelta(minutes=5)).isoformat()
    last = "%.2f" % (46000 * (1 + float(change) / 100))
    night = dict(NIGHT_ROWS[0], CDate="20260918", CTime="045959", CLastPrice=last, CRefPrice="46000.00")
    rows = captures(available=available) + [night_capture([night], available=available)]
    root, approval_path = Path(directory) / "overnight", Path(directory) / "overnight_sources.json"
    _store(root).save("overnight-20260919T120000Z-fixture", {"captures": {"items": rows}})
    approval_path.write_text(json.dumps({"sources": approvals()}), encoding="utf-8")
    return root, approval_path


def _with_gate(world, version="v1"):
    template = json.loads(world.paths["template"].read_text(encoding="utf-8"))
    template["position_sizing"]["night_rally_override"] = dict(GATE, version=version)
    world.paths["template"].write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")


class NightRallyPipelineTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.directory = self._dir.name

    def world(self, gate=True, change="9", version="v1"):
        world = PipelineWorld(Path(self.directory), approved_status=True)
        if gate:
            _with_gate(world, version)
        overnight = _overnight_with_night(self.directory, world, change)
        fear_greed = _negative_fear_greed(self.directory, world)
        return world, overnight, fear_greed

    def night_id(self, world, overnight):
        from etf_agent.perception.overnight_market import build_overnight_market_input
        pair = build_overnight_market_input(overnight[0], overnight[1], world.snapshot["snapshot_id"],
                                            world.snapshot["decision_cutoff"], "2026-09-18")
        night = [row for row in pair["result"]["components"] if row["symbol"] == "TXF_NIGHT"][0]
        self.assertEqual(night["status"], "available", pair["result"]["data_gaps"])
        return night["evidence_id"]

    def require_negative(self, world):
        bundle = json.loads((world.run_dir / "decision_input.json").read_text(encoding="utf-8"))
        result = bundle["market_fear_greed_input"]["result"]
        if result.get("outlook") != "negative":
            self.skipTest("恐懼貪婪 fixture 不是 negative（%s）" % result.get("outlook"))

    def test_aggressive_citing_the_night_rally_passes_the_whole_chain(self):
        world, overnight, fear_greed = self.world()
        stance = dict(STANCE, level="aggressive",
                      rationale="恐懼指標仍是前一交易日資料，但台指期夜盤大漲，判斷今日偏多。",
                      evidence_ids=[self.night_id(world, overnight), MARKET_REGIME_EVIDENCE_ID])
        result = world.run(FakeRunner(team_outputs(cash_stance=[stance])), fear_greed=fear_greed, overnight=overnight)
        self.require_negative(world)
        self.assertEqual(result.status, "completed", result.errors)
        brief = json.loads((world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["night_rally_override"]["min_night_change_pct"], "5")
        self.assertEqual(brief["night_rally_override"]["night_change_pct"], "9")
        team = json.loads((world.run_dir / "team_inputs.json").read_text(encoding="utf-8"))
        self.assertEqual(team["cash_stance"]["level"], "aggressive")
        DecisionRepository(world.repository).verify("decision-fixture")

    def test_aggressive_without_citing_the_night_is_still_refused(self):
        world, overnight, fear_greed = self.world()
        self.night_id(world, overnight)
        stance = dict(STANCE, level="aggressive")
        result = world.run(FakeRunner(team_outputs(cash_stance=[stance] * 4)), fear_greed=fear_greed, overnight=overnight)
        self.require_negative(world)
        self.assertEqual(result.status, "failed")
        self.assertIn("aggressive 未通過一致性護欄", " ".join(result.errors))

    def test_night_below_threshold_is_refused(self):
        world, overnight, fear_greed = self.world(change="3")
        stance = dict(STANCE, level="aggressive", evidence_ids=[self.night_id(world, overnight), MARKET_REGIME_EVIDENCE_ID])
        result = world.run(FakeRunner(team_outputs(cash_stance=[stance] * 4)), fear_greed=fear_greed, overnight=overnight)
        self.require_negative(world)
        self.assertEqual(result.status, "failed")

    def test_without_the_gate_aggressive_is_refused_and_the_brief_is_unchanged(self):
        world, overnight, fear_greed = self.world(gate=False)
        stance = dict(STANCE, level="aggressive", evidence_ids=[self.night_id(world, overnight), MARKET_REGIME_EVIDENCE_ID])
        result = world.run(FakeRunner(team_outputs(cash_stance=[stance] * 4)), fear_greed=fear_greed, overnight=overnight)
        self.require_negative(world)
        self.assertEqual(result.status, "failed")
        brief = json.loads((world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertNotIn("night_rally_override", brief)


class NightRallyV2Tests(NightRallyPipelineTests):
    """v2：夜盤漲幅達門檻時「要求」aggressive 並引用夜盤證據；未達門檻或夜盤不可用時同 v1、不強制。"""

    def test_neutral_is_refused_when_the_night_rally_reaches_the_threshold(self):
        world, overnight, fear_greed = self.world(version="v2")
        self.night_id(world, overnight)
        result = world.run(FakeRunner(team_outputs(cash_stance=[dict(STANCE)] * 4)), fear_greed=fear_greed, overnight=overnight)
        self.assertEqual(result.status, "failed")
        self.assertIn("night_rally_override v2", " ".join(result.errors))

    def test_aggressive_without_citing_the_night_is_refused(self):
        world, overnight, fear_greed = self.world(version="v2")
        self.night_id(world, overnight)
        stance = dict(STANCE, level="aggressive")
        result = world.run(FakeRunner(team_outputs(cash_stance=[stance] * 4)), fear_greed=fear_greed, overnight=overnight)
        self.assertEqual(result.status, "failed")
        self.assertIn("night_rally_override v2", " ".join(result.errors))

    def test_revision_to_aggressive_passes_and_the_brief_says_required(self):
        world, overnight, fear_greed = self.world(version="v2")
        night = self.night_id(world, overnight)
        aggressive = dict(STANCE, level="aggressive", rationale="夜盤大漲達人設門檻，依規則改為進攻。",
                          evidence_ids=[night, MARKET_REGIME_EVIDENCE_ID])
        result = world.run(FakeRunner(team_outputs(cash_stance=[dict(STANCE), aggressive])), fear_greed=fear_greed, overnight=overnight)
        self.assertEqual(result.status, "completed", result.errors)
        brief = json.loads((world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["night_rally_override"]["version"], "v2")
        self.assertTrue(brief["night_rally_override"]["aggressive_required"])
        team = json.loads((world.run_dir / "team_inputs.json").read_text(encoding="utf-8"))
        self.assertEqual(team["cash_stance"]["level"], "aggressive")
        DecisionRepository(world.repository).verify("decision-fixture")

    def test_below_threshold_does_not_force_aggressive(self):
        world, overnight, fear_greed = self.world(change="3", version="v2")
        self.night_id(world, overnight)
        result = world.run(FakeRunner(team_outputs(cash_stance=[dict(STANCE)])), fear_greed=fear_greed, overnight=overnight)
        self.assertEqual(result.status, "completed", result.errors)
        brief = json.loads((world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertFalse(brief["night_rally_override"]["aggressive_required"])


class NightRallyV4Tests(unittest.TestCase):
    """v4（2026-11-24 11:30）：情緒 negative 不再擋 aggressive，夜盤也不強迫；姿態完全由風險 Agent 判斷。"""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.directory = self._dir.name

    def run_with(self, stance, change="9"):
        world = PipelineWorld(Path(self.directory), approved_status=True)
        _with_gate(world, "v4")
        overnight = _overnight_with_night(self.directory, world, change)
        fear_greed = _negative_fear_greed(self.directory, world)
        runner = FakeRunner(team_outputs(cash_stance=[stance] * 4))
        result = world.run(runner, fear_greed=fear_greed, overnight=overnight)
        bundle = json.loads((world.run_dir / "decision_input.json").read_text(encoding="utf-8"))
        if bundle["market_fear_greed_input"]["result"].get("outlook") != "negative":
            self.skipTest("恐懼貪婪 fixture 不是 negative")
        prompts = " ".join(str(prompt) for prompt in runner.prompts)
        self.assertNotIn("全市場情緒 negative 時姿態為 neutral 或 defensive", prompts)  # v4 提示不再叫 Agent 避開 aggressive
        self.assertIn("night_rally_override（v4）", prompts)
        return world, result

    def test_aggressive_without_the_night_evidence_passes_under_negative_sentiment(self):
        stance = dict(STANCE, level="aggressive", rationale="恐懼指標是前一交易日資料，綜合判斷今日偏多。")
        world, result = self.run_with(stance, change="3")
        self.assertEqual(result.status, "completed", result.errors)
        team = json.loads((world.run_dir / "team_inputs.json").read_text(encoding="utf-8"))
        self.assertEqual(team["cash_stance"]["level"], "aggressive")
        brief = json.loads((world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["night_rally_override"]["version"], "v4")
        self.assertNotIn("aggressive_required", brief["night_rally_override"])
        DecisionRepository(world.repository).verify("decision-fixture")

    def test_neutral_is_not_forced_up_by_a_big_night_rally(self):
        world, result = self.run_with(dict(STANCE, rationale="夜盤大漲但可能只是一日行情，維持中性。"), change="9")
        self.assertEqual(result.status, "completed", result.errors)
        team = json.loads((world.run_dir / "team_inputs.json").read_text(encoding="utf-8"))
        self.assertEqual(team["cash_stance"]["level"], "neutral")


class NightRallyGateShapeTests(unittest.TestCase):
    def test_v1_to_v4_are_accepted_and_other_versions_refused(self):
        from etf_agent.decision.night_rally import validate_night_rally_override_gate
        for version, ok in (("v1", True), ("v2", True), ("v3", True), ("v4", True), ("v5", False)):
            errors = []
            validate_night_rally_override_gate({"night_rally_override": dict(GATE, version=version)}, errors)
            self.assertEqual(errors == [], ok, (version, errors))

    def test_unavailable_night_never_forces(self):
        from etf_agent.decision.night_rally import night_rally_required_evidence
        self.assertIsNone(night_rally_required_evidence(dict(GATE, version="v2"), {}))
        self.assertIsNone(night_rally_required_evidence(dict(GATE, version="v1"), {}))


if __name__ == "__main__":
    unittest.main()
