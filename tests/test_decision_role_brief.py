import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision import (
    BuyIntentPacketValidator,
    DecisionToolError,
    MomentumEngine,
    artifact_content_sha256,
    build_role_brief,
    build_role_input_artifact,
)

from test_portfolio_decision_agent import decision_bundle, intent_packet


def role_input(role="buy"):
    bundle = decision_bundle()
    momentum = MomentumEngine(bundle).run()
    return bundle, momentum, build_role_input_artifact(bundle, momentum, role)


class RoleBriefTests(unittest.TestCase):
    def test_brief_lists_universe_with_cited_ids_and_no_peer_data(self):
        bundle, _, artifact = role_input("sell")
        brief = build_role_brief(artifact)

        self.assertEqual(brief["role"], "sell")
        self.assertEqual({item["symbol"] for item in brief["symbols"]}, {"2330.TW", "2317.TW"})
        held = {item["symbol"]: item["held_shares"] for item in brief["symbols"]}
        self.assertEqual(held, {"2330.TW": 1000, "2317.TW": 0})
        tsmc = next(item for item in brief["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(tsmc["momentum"]["evidence_ids"], ["price-2330"])
        self.assertEqual(brief["account"]["source_evidence_id"], "account-evidence-1")
        self.assertNotIn("price_series", json.dumps(brief))
        self.assertNotIn("buy", json.dumps(brief["packet_envelope"]))

    def test_packet_filled_from_envelope_passes_existing_validator(self):
        bundle, momentum, artifact = role_input("buy")
        brief = build_role_brief(artifact)
        packet = intent_packet(bundle, momentum, "buy")
        packet.pop("content_sha256")
        packet.update(brief["packet_envelope"])
        packet["content_sha256"] = artifact_content_sha256(packet)

        self.assertEqual(BuyIntentPacketValidator(bundle, momentum).validate(packet), [])

    def test_tampered_role_input_is_rejected(self):
        _, _, artifact = role_input("buy")
        artifact["decision_bundle"]["rules"]["max_positions"] = 99
        with self.assertRaisesRegex(DecisionToolError, "role_input_sha256"):
            build_role_brief(artifact)

    def test_cli_writes_brief_without_reading_bundle_path(self):
        _, _, artifact = role_input("buy")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1]
            input_path = Path(directory) / "role_input.json"
            output_path = Path(directory) / "brief.json"
            input_path.write_text(json.dumps(artifact), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, str(root / "cli" / "portfolio_decision.py"),
                    "--bundle", str(Path(directory) / "missing.json"),
                    "build-role-brief", "--role-input", str(input_path),
                    "--output", str(output_path),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["role"], "buy")


if __name__ == "__main__":
    unittest.main()
