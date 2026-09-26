import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import (
    AgentCall,
    DailyDecisionPipeline,
    degraded_research_result,
    next_weekday_session,
    validate_research_result,
)
from etf_agent.data import MarketDataDatabase, TradingStatusBundleBuilder, TradingStatusRequest
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


def claim(claim_id, text, evidence):
    return {"claim_id": claim_id, "text": text, "evidence_ids": evidence}


BUY = {
    "items": [
        {
            "symbol": "2317.TW", "intent": "buy", "rationale": "動能完整。", "status_reason": "新買候選。",
            "catalyst_summary": "中期趨勢向上。", "horizon": "short", "evidence_ids": ["price-2317"],
            "risk_flags": [], "invalidation_conditions": ["跌破中期趨勢"],
            "claims": [claim("buy-2317-1", "截至 cutoff 價格趨勢向上。", ["price-2317"])],
        }
    ]
}
SELL = {
    "items": [
        {
            "symbol": "2330.TW", "intent": "hold", "rationale": "無退出證據。", "status_reason": "續抱。",
            "thesis_status": "intact", "horizon": "short", "evidence_ids": ["price-2330", "account-evidence-1"],
            "risk_flags": [], "invalidation_conditions": [],
            "claims": [claim("sell-2330-1", "目前持股且無強制退出證據。", ["price-2330", "account-evidence-1"])],
        }
    ]
}
ADJUDICATE = {
    "items": [
        {
            "symbol": "2317.TW", "intent": "buy", "rationale": "採納買方動能。", "status_reason": "交配置層。",
            "evidence_ids": ["price-2317"], "adopted_claim_ids": ["buy-2317-1"], "rejected_claim_ids": [],
            "unresolved_questions": [], "invalidation_conditions": ["跌破中期趨勢"],
        },
        {
            "symbol": "2330.TW", "intent": "hold", "rationale": "採納續抱。", "status_reason": "維持持股。",
            "evidence_ids": ["price-2330", "account-evidence-1"], "adopted_claim_ids": ["sell-2330-1"],
            "rejected_claim_ids": [], "unresolved_questions": [], "invalidation_conditions": [],
        },
    ]
}
SIZING = {
    "cash_stance": {"level": "neutral", "rationale": "市場中性。", "evidence_ids": ["price-2330"]},
    "items": [{"symbol": "2317.TW", "conviction": "medium", "rationale": "流動性足。", "evidence_ids": ["price-2317"]}],
}
APPROVE = {
    "decision": "approve", "rationale": "現金、集中與情境均可接受。", "evidence_ids": ["price-2317"],
    "risk_flags": [], "unresolved_questions": [], "revision_actions": [],
}


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
        session = next_weekday_session(snapshot["decision_cutoff"])
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
            ("research", degraded_research_result(snapshot, "research-fixture")),
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
            self.paths["research"], self.paths["rules"], self.paths["database"],
            self.paths["template"], self.paths["sectors"], "decision-fixture",
        )


class DailyPipelineTests(unittest.TestCase):
    def test_approved_run_seals_decision_with_agent_judgement(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = FakeRunner({"buy": [BUY], "sell": [SELL], "adjudicate": [ADJUDICATE], "sizing": [SIZING], "review_r0": [APPROVE]})
            result = world.run(runner)

            self.assertEqual(result.status, "completed", result.errors)
            self.assertEqual(result.decision_status, "approved")
            self.assertEqual(result.cash_stance, "neutral")
            self.assertEqual([order["symbol"] for order in result.orders], ["2317.TW"])
            self.assertEqual([name for name, _ in runner.prompts], ["buy", "sell", "adjudicate", "sizing", "review_r0"])
            DecisionRepository(world.repository).verify("decision-fixture")
            policy = json.loads((world.run_dir / "policy.json").read_text(encoding="utf-8"))
            self.assertEqual(policy["cash_buffer_rate"], "0.10")

    def test_invalid_agent_output_is_retried_with_validator_errors(self):
        wrong = json.loads(json.dumps(BUY))
        wrong["items"][0]["claims"][0]["evidence_ids"] = ["price-2330"]
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            runner = FakeRunner({"buy": [wrong, BUY], "sell": [SELL], "adjudicate": [ADJUDICATE], "sizing": [SIZING], "review_r0": [APPROVE]})
            result = world.run(runner)

        self.assertEqual(result.status, "completed", result.errors)
        buy_prompts = [prompt for name, prompt in runner.prompts if name == "buy"]
        self.assertEqual(len(buy_prompts), 2)
        self.assertIn("上一次輸出未通過確定性驗證", buy_prompts[1])

    def test_repeated_invalid_output_stops_without_fallback(self):
        wrong = {"items": [dict(BUY["items"][0], evidence_ids=["no-such-evidence"])]}
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=True)
            result = world.run(FakeRunner({"buy": [wrong, wrong]}))
            saved = list(world.repository.glob("*")) if world.repository.exists() else []

        self.assertEqual(result.status, "failed")
        self.assertTrue(any("buy Agent 輸出驗證失敗" in error for error in result.errors))
        self.assertEqual(saved, [])

    def test_unknown_trading_status_is_rejected_without_review_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            world = PipelineWorld(Path(directory), approved_status=False)
            runner = FakeRunner({"buy": [BUY], "sell": [SELL], "adjudicate": [ADJUDICATE], "sizing": [SIZING]})
            result = world.run(runner)
            review = json.loads((world.run_dir / "risk_review.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "completed", result.errors)
        self.assertEqual(result.decision_status, "rejected")
        self.assertEqual(result.orders, [])
        self.assertNotIn("review_r0", [name for name, _ in runner.prompts])
        self.assertEqual(review["decision"], "reject")
        self.assertTrue(any(flag.startswith("GUARD_FAILED") for flag in review["risk_flags"]))


class DailyPipelineHelperTests(unittest.TestCase):
    def test_next_weekday_session_skips_weekend(self):
        session = next_weekday_session("2026-09-25T10:00:00+00:00")  # 週五台北 18:00
        self.assertEqual(session["start"], "2026-09-28T09:00:00+08:00")
        self.assertEqual(session["end"], "2026-09-28T13:30:00+08:00")

    def test_degraded_research_is_valid_and_states_research_not_run(self):
        snapshot = builder_inputs()[0]
        result = degraded_research_result(snapshot, "research-1")
        self.assertEqual(validate_research_result(snapshot, result), [])
        self.assertEqual(result["status"], "degraded")
        self.assertIn("不代表沒有事件", result["errors"][0])


if __name__ == "__main__":
    unittest.main()
