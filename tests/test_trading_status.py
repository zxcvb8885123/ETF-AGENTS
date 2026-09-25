import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    DEFAULT_REQUIRED_CATEGORIES,
    MarketDataDatabase,
    TradingStatusBundleBuilder,
    TradingStatusBundleValidator,
    TradingStatusCollector,
    TradingStatusError,
    TradingStatusRepository,
    TradingStatusRequest,
    TradingStatusSourceDefinition,
    SavedJsonTradingStatusProvider,
    parse_trading_status_payload,
)
from etf_agent.decision import DecisionInputValidator, canonical_sha256, decision_bundle_sha256
from test_portfolio_decision_agent import decision_bundle as decision_fixture


CUTOFF = "2026-09-20T00:55:00+00:00"
SESSION_START = "2026-09-20T01:00:00+00:00"
SESSION_END = "2026-09-20T05:00:00+00:00"
CATEGORIES = ["trading_halt", "special_trading"]


def request():
    return TradingStatusRequest(
        request_id="status-request-1",
        snapshot_id="snapshot-1",
        universe_version="universe-1",
        universe_symbols=("2330.TW", "2317.TW"),
        decision_cutoff=CUTOFF,
        target_session_start=SESSION_START,
        target_session_end=SESSION_END,
        required_categories=tuple(CATEGORIES),
        source_config_version="source-config-1",
        policy_version="policy-1",
    )


def coverage(complete=True):
    rows = []
    for category in CATEGORIES:
        rows.append(
            {
                "source_id": "TWSE_%s" % category.upper(),
                "market": "TWSE",
                "category": category,
                "approval_status": "approved",
                "coverage_status": "complete" if complete else "partial",
                "semantics": "complete_current_state",
                "as_of": CUTOFF,
                "query_start": "2026-09-19T00:00:00+00:00",
                "query_end": CUTOFF,
                "row_count": 0,
                "page_count": 1,
                "expected_page_count": 1,
                "raw_payload_id": 1,
                "raw_payload_sha256": "a" * 64,
            }
        )
    return rows


def record(symbol="2330.TW", category="trading_halt", status_code="halted"):
    result = {
        "record_id": "record-1",
        "symbol": symbol,
        "market": "TWSE",
        "category": category,
        "status_code": status_code,
        "effective_from": SESSION_START,
        "effective_to": None,
        "published_at": CUTOFF,
        "available_at": CUTOFF,
        "fetched_at": CUTOFF,
        "source_id": "TWSE_%s" % category.upper(),
        "source_url": "https://example.invalid/status",
        "raw_payload_id": 1,
        "raw_payload_sha256": "a" * 64,
        "evidence_id": "status-evidence-1",
        "content_sha256": "",
        "version": "1",
        "parser_version": "1.0",
    }
    result["content_sha256"] = canonical_sha256(
        {key: value for key, value in result.items() if key != "content_sha256"}
    )
    return result


def refresh_record_hash(value):
    value["content_sha256"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "content_sha256"}
    )


class TradingStatusTests(unittest.TestCase):
    def test_default_policy_treats_attention_as_optional_information(self):
        self.assertNotIn("attention", DEFAULT_REQUIRED_CATEGORIES)
        base = {
            "request_id": "r",
            "snapshot_id": "s",
            "universe_version": "u",
            "universe_symbols": ["2330.TW"],
            "decision_cutoff": CUTOFF,
            "target_session": {"start": SESSION_START, "end": SESSION_END},
        }
        current = TradingStatusRequest.from_dict(base)
        self.assertEqual(current.policy_version, "trading-status-policy-2")
        self.assertNotIn("attention", current.required_categories)
        legacy = TradingStatusRequest.from_dict({**base, "policy_version": "trading-status-policy-1"})
        self.assertIn("attention", legacy.required_categories)

    def test_optional_attention_is_reported_only_from_approved_coverage(self):
        attention = record(category="attention", status_code="attention")
        candidate_coverage = coverage() + [{
            **coverage()[0],
            "source_id": "TWSE_ATTENTION",
            "category": "attention",
            "approval_status": "candidate",
        }]
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [attention], candidate_coverage
        ).build()
        self.assertEqual(bundle["status"], "completed")
        item = next(item for item in assessment["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(item["state"], "allowed")
        self.assertNotIn("ATTENTION:attention", item["reason_codes"])
        approved_coverage = copy.deepcopy(candidate_coverage)
        approved_coverage[-1]["approval_status"] = "approved"
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [attention], approved_coverage
        ).build()
        item = next(item for item in assessment["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(item["state"], "allowed")
        self.assertIn("ATTENTION:attention", item["reason_codes"])
        self.assertIn(attention["evidence_id"], item["evidence_ids"])
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_rebuilds_blocked_and_allowed_separately(self):
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [record()], coverage()
        ).build()
        by_symbol = {item["symbol"]: item for item in assessment["symbols"]}
        self.assertEqual(by_symbol["2330.TW"]["state"], "blocked")
        self.assertEqual(by_symbol["2317.TW"]["state"], "allowed")
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_missing_coverage_is_degraded_and_unknown(self):
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [record()], coverage(complete=False)
        ).build()
        self.assertEqual(bundle["status"], "degraded")
        self.assertTrue(all(item["state"] == "unknown" for item in assessment["symbols"]))
        self.assertEqual(TradingStatusBundleValidator().validate(bundle, assessment), [])

    def test_candidate_source_never_upgrades_to_allowed(self):
        candidate_coverage = coverage()
        candidate_coverage[0]["approval_status"] = "candidate"
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [record()], candidate_coverage
        ).build()
        self.assertEqual(bundle["status"], "degraded")
        item = next(item for item in assessment["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(item["state"], "unknown")

    def test_future_record_fails_closed(self):
        future = record()
        future["available_at"] = "2026-09-20T01:01:00+00:00"
        refresh_record_hash(future)
        with self.assertRaisesRegex(TradingStatusError, "available_at"):
            TradingStatusBundleBuilder(request(), [future], coverage()).build()

    def test_conflicting_active_status_becomes_unknown(self):
        second = record(status_code="active")
        second["record_id"] = "record-2"
        second["evidence_id"] = "status-evidence-2"
        refresh_record_hash(second)
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [record(), second], coverage()
        ).build()
        item = next(item for item in assessment["symbols"] if item["symbol"] == "2330.TW")
        self.assertEqual(item["state"], "unknown")
        self.assertIn("CONFLICTING_STATUS:trading_halt", item["reason_codes"])

    def test_tampering_and_sqlite_round_trip_are_detected(self):
        bundle, assessment = TradingStatusBundleBuilder(
            request(), [record()], coverage()
        ).build()
        tampered = copy.deepcopy(bundle)
        tampered["records"][0]["status_code"] = "active"
        self.assertTrue(TradingStatusBundleValidator().validate(tampered, assessment))
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "status.db")
            TradingStatusRepository().save(database, bundle)
            loaded = TradingStatusRepository().load(database, bundle["bundle_id"])
            self.assertEqual(loaded, bundle)

    def test_parser_preserves_raw_payload_identity_and_empty_semantics(self):
        definition = TradingStatusSourceDefinition(
            source_id="TWSE_TRADING_HALT",
            market="TWSE",
            category="trading_halt",
            url="https://example.invalid/status",
            approval_status="approved",
            semantics="complete_current_state",
            code_field="Code",
            status_code_field="Status",
            effective_from_field="EffectiveFrom",
        )
        payload = json.dumps(
            [
                {
                    "Code": "2330",
                    "Status": "halted",
                    "EffectiveFrom": SESSION_START,
                }
            ],
            ensure_ascii=False,
        )
        records, source_coverage = parse_trading_status_payload(
            payload,
            definition,
            fetched_at=CUTOFF,
            raw_payload_id=7,
            query_start="2026-09-19T00:00:00+00:00",
            query_end=CUTOFF,
            as_of=CUTOFF,
        )
        self.assertEqual(records[0]["symbol"], "2330.TW")
        self.assertEqual(records[0]["raw_payload_id"], 7)
        self.assertEqual(source_coverage["coverage_status"], "complete")
        empty_payload = "[]"
        _, empty_coverage = parse_trading_status_payload(
            empty_payload,
            definition,
            fetched_at=CUTOFF,
            raw_payload_id=8,
            query_start="2026-09-19T00:00:00+00:00",
            query_end=CUTOFF,
            as_of=CUTOFF,
        )
        self.assertEqual(empty_coverage["coverage_status"], "failed")

    def test_cli_build_and_validate_returns_machine_readable_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1]
            directory_path = Path(directory)
            request_path = directory_path / "request.json"
            records_path = directory_path / "records.json"
            coverage_path = directory_path / "coverage.json"
            bundle_path = directory_path / "bundle.json"
            request_path.write_text(json.dumps(request().as_dict()), encoding="utf-8")
            records_path.write_text(json.dumps([record()]), encoding="utf-8")
            coverage_path.write_text(json.dumps(coverage()), encoding="utf-8")
            command = [
                sys.executable,
                str(root / "cli" / "trading_status.py"),
                "build-bundle",
                "--request",
                str(request_path),
                "--records",
                str(records_path),
                "--coverage",
                str(coverage_path),
                "--output",
                str(bundle_path),
            ]
            built = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(built.returncode, 0, built.stderr)
            validation = subprocess.run(
                [sys.executable, str(root / "cli" / "trading_status.py"), "validate", "--input", str(bundle_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(validation.returncode, 0, validation.stderr)
            self.assertTrue(json.loads(validation.stdout)["valid"])

    def test_collector_saves_raw_payload_before_degraded_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            raw_path = directory_path / "raw.json"
            raw_path.write_text(
                json.dumps(
                    [{"Code": "2330", "Status": "halted", "EffectiveFrom": SESSION_START}]
                ),
                encoding="utf-8",
            )
            definition = TradingStatusSourceDefinition(
                source_id="TWSE_TRADING_HALT",
                market="TWSE",
                category="trading_halt",
                url="https://example.invalid/status",
                approval_status="approved",
                semantics="complete_current_state",
                code_field="Code",
                status_code_field="Status",
                effective_from_field="EffectiveFrom",
            )
            database = MarketDataDatabase(directory_path / "collector.db")
            result = TradingStatusCollector(database).collect(
                request(),
                [
                    (
                        definition,
                        SavedJsonTradingStatusProvider(
                            "TWSE_TRADING_HALT", raw_path, CUTOFF
                        ),
                    )
                ],
            )
            self.assertEqual(result.bundle["status"], "degraded")
            with database.connect() as connection:
                payload = connection.execute("SELECT COUNT(*) AS count FROM raw_payloads").fetchone()
                run = connection.execute("SELECT status FROM collection_runs").fetchone()
            self.assertEqual(payload["count"], 1)
            self.assertEqual(run["status"], "success")

    def test_decision_adapter_requires_paired_bundle_and_assessment(self):
        decision = decision_fixture()
        status_request = TradingStatusRequest(
            request_id="status-request-decision",
            snapshot_id=decision["snapshot_id"],
            universe_version="universe-1",
            universe_symbols=("2330.TW", "2317.TW"),
            decision_cutoff=decision["decision_cutoff"],
            target_session_start="2026-09-20T01:00:00+00:00",
            target_session_end="2026-09-20T05:00:00+00:00",
            required_categories=tuple(CATEGORIES),
            source_config_version="source-config-1",
            policy_version="policy-1",
        )
        status_bundle, assessment = TradingStatusBundleBuilder(
            status_request, [record()], coverage()
        ).build()
        decision["trading_status_bundle"] = status_bundle
        decision["tradability_assessment"] = assessment
        decision["snapshot_sha256"] = canonical_sha256(decision["snapshot"])
        decision["bundle_sha256"] = decision_bundle_sha256(decision)
        self.assertEqual(DecisionInputValidator(decision).validate(), [])
        del decision["tradability_assessment"]
        decision["bundle_sha256"] = decision_bundle_sha256(decision)
        self.assertTrue(
            any("成對" in error for error in DecisionInputValidator(decision).validate())
        )


if __name__ == "__main__":
    unittest.main()
