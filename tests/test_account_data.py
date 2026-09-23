import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.accounts import AccountDataError, AccountImporter, AccountRunRepository, AccountValidator
from etf_agent.accounts.integration import canonical_sha256, export_decision_account, reconcile


CUTOFF = "2026-09-22T14:21:56+00:00"


class AccountDataIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "account.json"
        self.payload = {
            "schema_version": "1.0", "provider": "fixture-platform", "account_id": "anon-1", "currency": "TWD",
            "ledger_at": CUTOFF, "valuation_at": CUTOFF, "available_at": "2026-09-22T14:00:00+00:00",
            "reported_cash": "300000", "settled_cash": "300000", "unsettled_receivable": "0",
            "unsettled_payable": "0", "restricted_cash": "0", "available_cash": "300000",
            "reported_nav": "400000", "positions": [{"symbol": "2330.TW", "shares": 1000, "average_cost": "90"}],
            "settlements": [], "source_version": "fixture-1", "source_url": "fixture://account",
        }
        self.input.write_text(json.dumps(self.payload), encoding="utf-8")
        self.bundle = AccountImporter().import_file(self.input, CUTOFF, "acct-test-1", "fixture")
        self.snapshot = {
            "snapshot_id": "snapshot-1", "decision_cutoff": CUTOFF,
            "universe_symbols": ["2330.TW"],
            "latest_prices": [{"symbol": "2330.TW", "analysis_close_price": "100"}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_import_and_reconcile_recompute_nav(self):
        self.assertEqual(AccountValidator().validate(self.bundle), [])
        result = reconcile(self.bundle, self.snapshot, "0")
        self.assertEqual(result["calculated_nav"], "400000")
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["decision_compatible"] is False)

    def test_cutoff_future_account_is_rejected(self):
        self.payload["available_at"] = "2026-09-22T14:22:00+00:00"
        self.input.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(AccountDataError, "不得晚於"):
            AccountImporter().import_file(self.input, CUTOFF, "acct-test-2", "fixture")

    def test_official_mode_requires_approved_provider_and_version(self):
        approved = {"fixture-platform": {"status": "approved", "versions": ["fixture-1"]}}
        bundle = AccountImporter().import_file(self.input, CUTOFF, "acct-test-4", "official", approved)
        self.assertEqual(bundle["source"]["execution_mode"], "official")
        with self.assertRaisesRegex(AccountDataError, "未核准"):
            AccountImporter().import_file(self.input, CUTOFF, "acct-test-5", "official", {})

    def test_pending_settlement_rows_must_match_cash_totals(self):
        self.payload["unsettled_receivable"] = "100"
        self.payload["settlements"] = [{
            "settlement_id": "settlement-1", "direction": "receivable", "amount": "90",
            "settlement_at": "2026-09-24T00:00:00+00:00", "status": "pending",
        }]
        self.input.write_text(json.dumps(self.payload), encoding="utf-8")
        with self.assertRaisesRegex(AccountDataError, "交割合計"):
            AccountImporter().import_file(self.input, CUTOFF, "acct-test-6", "fixture")

    def test_missing_cutoff_price_blocks_reconciliation(self):
        snapshot = dict(self.snapshot)
        snapshot["latest_prices"] = []
        result = reconcile(self.bundle, snapshot, "0.01")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("all_positions_have_cutoff_prices", result["errors"])

    def test_nav_drift_blocks_and_fixture_cannot_export(self):
        bundle = dict(self.bundle)
        bundle["reported_nav"] = "450000"
        bundle.pop("content_sha256")
        bundle["content_sha256"] = canonical_sha256(bundle)
        result = reconcile(bundle, self.snapshot, "0.001")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("reported_nav_within_tolerance", result["errors"])
        result = reconcile(self.bundle, self.snapshot, "0")
        with self.assertRaisesRegex(AccountDataError, "fixture"):
            export_decision_account(self.bundle, result, self.snapshot)

    def test_manifest_rejects_modified_archived_artifact(self):
        repo = AccountRunRepository(self.root / "runs")
        run = repo.save("acct-test-3", {"account_bundle.json": self.bundle}, self.input)
        self.assertEqual(repo.verify("acct-test-3"), run)
        (run / "account_bundle.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(AccountDataError, "雜湊"):
            repo.verify("acct-test-3")


if __name__ == "__main__":
    unittest.main()
