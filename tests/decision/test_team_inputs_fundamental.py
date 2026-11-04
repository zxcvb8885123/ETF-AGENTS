"""team_inputs 2.6：bundle 有 fundamental_period_policy 時封存基本面指標，Finalizer 整份重算比對；舊 run 不受影響。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import (
    DecisionFinalizer,
    DecisionRepository,
    DecisionResultValidator,
    DecisionToolError,
    build_team_inputs,
    canonical_sha256,
    compute_fundamental_metrics,
    decision_bundle_sha256,
)
from etf_agent.decision.finalization import FUNDAMENTAL_TEAM_VERSION
from etf_agent.decision.input_validator import FUNDAMENTAL_PERIOD_PER_SYMBOL, FUNDAMENTAL_PERIOD_POLICY_FIELD

from test_daily_pipeline import FakeRunner, PipelineWorld, team_outputs
from test_fundamental_period import complete_period
from test_team_chain import finalize, team_world, tradable_bundle


def gated_bundle():
    bundle = tradable_bundle()
    bundle[FUNDAMENTAL_PERIOD_POLICY_FIELD] = FUNDAMENTAL_PERIOD_PER_SYMBOL
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def load(run_dir, name):
    return json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8"))


class TeamInputsVersionTests(unittest.TestCase):
    def test_fundamental_metrics_requires_selection_ranking_and_object(self):
        _, _, _, _, _, _, reports, research = team_world()
        from test_trader import STANCE
        with self.assertRaisesRegex(DecisionToolError, "2.6"):
            build_team_inputs(reports, None, STANCE, {"status": "unavailable"}, fundamental_metrics=None)
        with self.assertRaises(DecisionToolError):
            build_team_inputs(reports, None, STANCE, {"s": 1}, {"p": 1}, {"r": 1}, {"k": 1}, fundamental_metrics="bad")
        team = build_team_inputs(reports, None, STANCE, {"s": 1}, {"p": 1}, {"r": 1}, {"k": 1}, fundamental_metrics=None)
        self.assertEqual(team["schema_version"], FUNDAMENTAL_TEAM_VERSION)
        self.assertIsNone(team["fundamental_metrics"])
        self.assertEqual(build_team_inputs(reports, None, STANCE, {"s": 1}, {"p": 1}, {"r": 1}, {"k": 1})["schema_version"], "2.5")

    def test_legacy_team_inputs_are_rejected_when_bundle_has_the_period_policy(self):
        bundle, momentum, debate, trade, policy, team, _, _ = team_world(bundle=gated_bundle())
        _, result = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("2.6" in error and "fundamental_period_policy" in error for error in result["errors"]), result["errors"])

    def test_legacy_bundle_without_the_policy_is_unchanged(self):
        bundle, momentum, debate, trade, policy, team, _, _ = team_world()
        _, result = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])


class PipelineFundamentalMetricsTests(unittest.TestCase):
    def world(self, directory):
        world = PipelineWorld(Path(directory), approved_status=True)
        documents = complete_period(11, "2330.TW", 2026, 2) + complete_period(21, "2317.TW", 2026, 2)
        for document in documents:
            world.snapshot["documents"].append(document)
            world.snapshot["source_evidence"].append({
                "evidence_id": document["source_evidence_id"], "source": document["source"],
                "authority": "mops", "data_type": "financial_statement",
                "url": document["source_url"],
                "content_as_of": document["published_at"],
                "fetched_at": document["available_at"],
                "content_sha256": document["content_sha256"],
                "raw_payload_id": document["document_id"],
            })
        world.paths["snapshot"].write_text(json.dumps(world.snapshot, ensure_ascii=False), encoding="utf-8")
        return world

    def test_metrics_are_sealed_in_team_inputs_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            world = self.world(directory)
            result = world.run(FakeRunner(team_outputs()))
            self.assertEqual(result.status, "completed", result.errors)
            run_dir = DecisionRepository(world.repository).verify("decision-fixture")
            bundle = load(run_dir, "decision_input")
            self.assertEqual(bundle[FUNDAMENTAL_PERIOD_POLICY_FIELD], FUNDAMENTAL_PERIOD_PER_SYMBOL)
            names = ("policy", "momentum", "debate", "intent", "team_inputs", "proposal", "scenario", "guard", "risk_review", "revision_history", "decision")
            art = {name: load(run_dir, name) for name in names}
            team = art["team_inputs"]
            self.assertEqual(team["schema_version"], FUNDAMENTAL_TEAM_VERSION)
            self.assertEqual(team["fundamental_metrics"], compute_fundamental_metrics(bundle))
            self.assertEqual(team["fundamental_metrics"], load(world.run_dir, "fundamental_metrics"))

            def validate(team_inputs):
                return DecisionResultValidator(bundle, art["policy"], art["momentum"], art["debate"], art["intent"], team_inputs).validate(
                    art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"], art["decision"])

            self.assertEqual(validate(team), [])
            tampered = copy.deepcopy(team)
            for item in tampered["fundamental_metrics"]["items"]:
                for metric in item["metrics"]:
                    if metric["status"] == "available":
                        metric["value"] = "999"
            self.assertTrue(validate(tampered))
            finalizer = DecisionFinalizer(bundle, art["policy"], art["momentum"], art["debate"], art["intent"], tampered)
            errors = finalizer.validate_inputs(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"])
            self.assertTrue(any("fundamental_metrics" in error for error in errors), errors)
            missing = copy.deepcopy(team)
            missing["fundamental_metrics"] = None
            self.assertTrue(validate(missing))
            downgraded = {key: value for key, value in team.items() if key != "fundamental_metrics"}
            downgraded["schema_version"] = "2.5"
            self.assertTrue(validate(downgraded))
            self.assertEqual(art["decision"]["team_inputs_sha256"], canonical_sha256(team))


if __name__ == "__main__":
    unittest.main()
