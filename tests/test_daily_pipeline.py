import json
import tempfile
from decimal import Decimal
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import (
    AgentCall,
    DailyDecisionPipeline,
    degraded_research_result,
    validate_research_result,
)
from etf_agent.data import MarketDataDatabase, TradingStatusBundleBuilder, TradingStatusRequest, next_trading_session
from etf_agent.data.trading_status import DEFAULT_REQUIRED_CATEGORIES
from etf_agent.decision import DecisionRepository

from test_decision_input_builder import builder_inputs, seed_history


ROOT = Path(__file__).resolve().parents[1]


class FakeRunner:
    """依 Agent 名稱依序回傳預先準備的結構化輸出，並記錄提示詞。"""

    def __init__(self, outputs):
        self.outputs = {name: list(items) for name, items in outputs.items()}
        self.prompts = []

    def run(self, task, run_dir):
        self.prompts.append((task.name, task.prompt))
        if not self.outputs.get(task.name):
            raise AssertionError("未預期的 Agent 呼叫：%s" % task.name)
        return AgentCall(task.name, 0, self.outputs[task.name].pop(0), 0.01)


def finding(finding_id, evidence):
    return {"finding_id": finding_id, "text": "依摘要判斷。", "evidence_ids": [evidence]}


def stance_claim(claim_id, evidence, findings=()):
    return {"claim_id": claim_id, "text": "依分析報告。", "evidence_ids": [evidence], "finding_ids": list(findings)}


# 分析團隊新鏈各子 Agent 的預備輸出；fixture 帳戶持有 2330、未持有 2317。
TECHNICAL = {"items": [
    {"symbol": "2317.TW", "outlook": "positive", "findings": [finding("tech-2317-1", "price-2317")], "data_gaps": []},
    {"symbol": "2330.TW", "outlook": "neutral", "findings": [finding("tech-2330-1", "price-2330")], "data_gaps": []},
]}
FUNDAMENTAL = {"items": [
    {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
    for symbol in ("2317.TW", "2330.TW")
]}
EVENT = {"items": [
    {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_MATERIAL_EVENT_IN_WINDOW"], "events": []}
    for symbol in ("2317.TW", "2330.TW")
]}
BULL = {"items": [
    {"symbol": "2317.TW", "strength": "moderate", "claims": [stance_claim("bull-2317-1", "price-2317", ["tech-2317-1"])], "invalidation_conditions": ["跌破均線"]},
    {"symbol": "2330.TW", "strength": "weak", "claims": [stance_claim("bull-2330-1", "price-2330")], "invalidation_conditions": []},
]}
BEAR = {"items": [
    {"symbol": "2317.TW", "strength": "none", "claims": [], "invalidation_conditions": []},
    {"symbol": "2330.TW", "strength": "moderate", "claims": [stance_claim("bear-2330-1", "price-2330")], "invalidation_conditions": ["趨勢轉強"]},
]}
TRADE = {"items": [
    {"symbol": "2317.TW", "intent": "buy", "conviction": "medium", "rationale": "多頭論點成立。",
     "adopted_claim_ids": ["bull-2317-1"], "rejected_claim_ids": [], "unresolved_questions": [], "invalidation_conditions": ["跌破均線"]},
    {"symbol": "2330.TW", "intent": "hold", "conviction": None, "rationale": "多空相當，續抱。",
     "adopted_claim_ids": ["bull-2330-1"], "rejected_claim_ids": ["bear-2330-1"], "unresolved_questions": [], "invalidation_conditions": []},
]}
STANCE = {"level": "neutral", "rationale": "市場中性。", "evidence_ids": ["price-2330"]}
APPROVE = {
    "decision": "approve", "rationale": "現金、集中與情境均可接受。", "evidence_ids": ["price-2317"],
    "risk_flags": [], "unresolved_questions": [], "revision_actions": [],
}


def team_outputs(**overrides):
    outputs = {
        "technical_b0": [TECHNICAL], "fundamental_b0": [FUNDAMENTAL], "event_b0": [EVENT],
        "bull_b0": [BULL], "bear_b0": [BEAR], "trader_b0": [TRADE], "cash_stance": [STANCE],
        "review_r0": [APPROVE],
    }
    outputs.update(overrides)
    return outputs


class PipelineWorld:
    """在暫存目錄建立 Snapshot、行情資料庫、帳戶、規則、policy 樣板與產業分類。"""

    def __init__(self, directory: Path, approved_status: bool):
        snapshot, rules, history, account = builder_inputs()
        snapshot["universe_version"] = "universe-fixture"
        self.snapshot = snapshot
        database = MarketDataDatabase(directory / "market.db")
        seed_history(database, [dict(row, source="TWSE_STOCK_DAY") for row in history])
        template = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        template["available_at"] = "2026-09-19T00:00:00+00:00"
        sectors = {
            "version": "sector:fixture",
            "available_at": "2026-09-19T00:00:00+00:00",
            "sector_by_symbol": {"2330.TW": "industry:24", "2317.TW": "industry:31"},
        }
        session = next_trading_session(snapshot["decision_cutoff"], set())
        request = TradingStatusRequest.from_snapshot(snapshot, session["start"], session["end"])
        coverage = [] if not approved_status else [
            {
                "source_id": "TWSE_%s" % category.upper(), "market": "TWSE", "category": category,
                "approval_status": "approved", "coverage_status": "complete",
                "semantics": "complete_current_state", "as_of": snapshot["decision_cutoff"],
                "query_start": "2026-09-19T00:00:00+00:00", "query_end": snapshot["decision_cutoff"],
                "row_count": 0, "page_count": 1, "expected_page_count": 1,
                "raw_payload_id": 1, "raw_payload_sha256": "a" * 64,
            }
            for category in DEFAULT_REQUIRED_CATEGORIES
        ]
        status_bundle, assessment = TradingStatusBundleBuilder(request, [], coverage).build()
        self.paths = {}
        for name, payload in (
            ("snapshot", snapshot), ("rules", rules), ("account", account), ("template", template),
            ("sectors", sectors), ("trading_status", {"bundle": status_bundle, "assessment": assessment}),
        ):
            self.paths[name] = directory / ("%s.json" % name)
            self.paths[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.paths["database"] = directory / "market.db"
        self.run_dir = directory / "run"
        self.run_dir.mkdir()
        self.repository = directory / "decisions"

    def run(self, runner):
        pipeline = DailyDecisionPipeline(ROOT, self.run_dir, runner, self.repository, log=lambda _: None)
        return pipeline.run(
            self.paths["snapshot"], self.paths["account"], self.paths["trading_status"],
            self.paths["rules"], self.paths["database"],
            self.paths["template"], self.paths["sectors"], "decision-fixture",
        )


class DailyPipelineTests(unittest.TestCase):
    def test_team_chain_seals_approved_decision_with_team_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = FakeRunner(team_outputs())
            result = world.run(runner)

            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual(result.decision_status, "approved")
            self.assertEqual(result.cash_stance, "neutral")
            self.assertEqual([order["symbol"] for order in result.orders], ["2317.TW"])
            self.assertEqual(
                [name for name, _ in runner.prompts],
                ["technical_b0", "fundamental_b0", "event_b0", "bull_b0", "bear_b0", "trader_b0", "cash_stance", "review_r0"],
            )
            run_dir = DecisionRepository(world.repository).verify("decision-fixture")
            team = json.loads((run_dir / "team_inputs.json").read_text(encoding="utf-8"))
            self.assertEqual(team["cash_stance"]["level"], "neutral")
            self.assertEqual(json.loads((run_dir / "debate.json").read_text(encoding="utf-8"))["schema_version"], "2.0")
            research = json.loads(result.research_result_path.read_text(encoding="utf-8"))
            self.assertEqual((research["status"], research["items"]), ("completed", []))
            policy = json.loads((world.run_dir / "policy.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["cash_buffer_rate"], "0.10")
            self.assertEqual(policy["position_sizing"]["conviction_by_symbol"], {"2317.TW": "medium"})

    def test_invalid_agent_output_is_retried_with_validator_errors(self):
        broken = json.loads(json.dumps(TRADE))
        broken["items"][1]["rejected_claim_ids"] = []
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = FakeRunner(team_outputs(trader_b0=[broken, TRADE]))
            result = world.run(runner)

        self.assertEqual(result.status, "completed", result.errors)
        trader_prompts = [prompt for name, prompt in runner.prompts if name == "trader_b0"]
        self.assertEqual(len(trader_prompts), 2)
        self.assertIn("上一次輸出未通過確定性驗證", trader_prompts[1])

    def test_repeated_invalid_output_stops_without_fallback(self):
        wrong = {"items": TECHNICAL["items"][:1]}
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            result = world.run(FakeRunner({"technical_b0": [wrong, wrong]}))
            saved = list(world.repository.glob("*")) if world.repository.exists() else []

        self.assertEqual(result.status, "failed")
        self.assertTrue(any("technical_b0 Agent 輸出驗證失敗" in error for error in result.errors), result.errors)
        self.assertEqual(saved, [])

    def test_unknown_trading_status_is_rejected_without_review_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=False)
            outputs = team_outputs()
            outputs.pop("review_r0")
            runner = FakeRunner(outputs)
            result = world.run(runner)
            review = json.loads((world.run_dir / "risk_review.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(result.decision_status, "rejected")
        self.assertEqual(result.orders, [])
        self.assertNotIn("review_r0", [name for name, _ in runner.prompts])
        self.assertEqual(review["decision"], "reject")
        self.assertTrue(any(flag.startswith("GUARD_FAILED") for flag in review["risk_flags"]))


class SectorExposureTests(unittest.TestCase):
    def test_review_sees_sector_weights_computed_from_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = FakeRunner(team_outputs())
            result = world.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            self.assertIn("sector_exposure_review_input_r0", dict(runner.prompts)["review_r0"])
            exposure = json.loads((world.run_dir / "sector_exposure_review_input_r0.json").read_text(encoding="utf-8"))
            proposal = json.loads((world.run_dir / "proposal_r0.json").read_text(encoding="utf-8"))
            self.assertEqual(exposure["proposal_id"], proposal["proposal_id"])
            sectors = {"2330.TW": "industry:24", "2317.TW": "industry:31"}
            expected = {}
            for position in proposal["allocation_proposal"]["positions"]:
                expected[sectors[position["symbol"]]] = position["weight"]
            self.assertEqual({item["sector"]: item["weight"] for item in exposure["items"]}, expected)
            weights = [Decimal(item["weight"]) for item in exposure["items"]]
            self.assertEqual(weights, sorted(weights, reverse=True))
            for item in exposure["items"]:
                self.assertEqual(Decimal(item["headroom"]), Decimal(item["limit"]) - Decimal(item["weight"]))


class ResumeTests(unittest.TestCase):
    def test_resume_reuses_validated_outputs_after_interruption(self):
        from etf_agent.automation.agent_runner import DailyPipelineError

        class LimitRunner(FakeRunner):
            def run(self, task, run_dir):
                if task.name == "bull_b0":
                    raise DailyPipelineError("bull_b0 Agent 未產生結構化輸出：You've hit your session limit")
                return super().run(task, run_dir)

        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            first = world.run(LimitRunner(team_outputs()))
            self.assertEqual(first.status, "failed")
            self.assertIn("session limit", first.errors[0])

            remaining = {name: value for name, value in team_outputs().items() if name not in {"technical_b0", "fundamental_b0", "event_b0"}}
            runner = FakeRunner(remaining)
            pipeline = DailyDecisionPipeline(ROOT, world.run_dir, runner, world.repository, log=lambda _: None, resume=True)
            result = pipeline.run(
                world.paths["snapshot"], world.paths["account"], world.paths["trading_status"],
                world.paths["rules"], world.paths["database"], world.paths["template"], world.paths["sectors"],
                "decision-fixture",
            )
            raw_files = sorted(path.name for path in world.run_dir.glob("technical_b0_raw_*.json"))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(result.decision_status, "approved")
        self.assertEqual([name for name, _ in runner.prompts][0], "bull_b0")
        self.assertNotIn("technical_b0", [name for name, _ in runner.prompts])
        self.assertEqual(raw_files, ["technical_b0_raw_1.json"])

    def test_resume_does_not_reuse_output_that_no_longer_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            stale = json.loads(json.dumps(TECHNICAL))
            stale["items"] = stale["items"][:1]
            (world.run_dir / "technical_b0_raw_1.json").write_text(json.dumps(stale), encoding="utf-8")
            runner = FakeRunner(team_outputs())
            pipeline = DailyDecisionPipeline(ROOT, world.run_dir, runner, world.repository, log=lambda _: None, resume=True)
            result = pipeline.run(
                world.paths["snapshot"], world.paths["account"], world.paths["trading_status"],
                world.paths["rules"], world.paths["database"], world.paths["template"], world.paths["sectors"],
                "decision-fixture",
            )
            saved = sorted(path.name for path in world.run_dir.glob("technical_b0_raw_*.json"))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual([name for name, _ in runner.prompts][0], "technical_b0")
        self.assertEqual(saved, ["technical_b0_raw_1.json", "technical_b0_raw_2.json"])


class DailyPipelineHelperTests(unittest.TestCase):
    def test_degraded_research_is_valid_and_states_research_not_run(self):
        snapshot = builder_inputs()[0]
        result = degraded_research_result(snapshot, "research-1")
        self.assertEqual(validate_research_result(snapshot, result), [])
        self.assertEqual(result["status"], "degraded")
        self.assertIn("不代表沒有事件", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
