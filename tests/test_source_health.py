import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.contracts import SourceFeasibilityReport
from etf_agent.data import (
    DataAgentService,
    Instrument,
    MarketDataDatabase,
    SourceHealthProbe,
    SourceProbeDefinition,
    UniverseValidator,
    load_universe,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
FETCHED_AT = "2026-09-17T06:00:00+00:00"


def definition(market):
    if market == "TWSE":
        return SourceProbeDefinition(
            source_id="TWSE_STOCK_DAY_ALL",
            market="TWSE",
            data_type="latest_prices",
            url="fixture://twse",
            format="json",
            code_field="Code",
            date_field="Date",
            tradable_field="ClosingPrice",
            required_fields=("Code", "Name", "Date", "ClosingPrice"),
        )
    return SourceProbeDefinition(
        source_id="TPEX_MAINBOARD_QUOTES",
        market="TPEX",
        data_type="latest_prices",
        url="fixture://tpex",
        format="json",
        code_field="SecuritiesCompanyCode",
        date_field="Date",
        tradable_field="Close",
        required_fields=("SecuritiesCompanyCode", "CompanyName", "Date", "Close"),
    )


def probe(market, universe):
    filename = (
        "universe_validation_twse.json"
        if market == "TWSE"
        else "universe_validation_tpex.json"
    )
    return SourceHealthProbe.parse_payload(
        definition(market),
        (FIXTURES / filename).read_text(encoding="utf-8"),
        universe,
        fetched_at=FETCHED_AT,
        duration_ms=12,
    )


def report_for(universe):
    probes = [probe("TWSE", universe), probe("TPEX", universe)]
    degraded = any(item.universe_covered < item.universe_total for item in probes)
    return SourceFeasibilityReport(
        schema_version="1.0",
        report_id="source-report-fixture",
        generated_at=FETCHED_AT,
        status="degraded" if degraded else "passed",
        probes=probes,
    )


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.valid_universe = [
            Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-07-31"),
            Instrument("5904.TWO", "5904", "寶雅*", "TPEX", "2026-07-31"),
        ]

    def test_probe_records_schema_hash_date_and_coverage(self):
        result = probe("TWSE", self.valid_universe)
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.data_date, "2026-09-17")
        self.assertEqual(result.universe_covered, 1)
        self.assertEqual(result.universe_total, 1)
        self.assertEqual(len(result.content_sha256), 64)
        self.assertIn("ClosingPrice", result.schema_fields)

    def test_probe_rejects_schema_drift(self):
        payload = json.dumps([{"Code": "2330", "Date": "1150917"}])
        with self.assertRaisesRegex(ValueError, "schema 缺少欄位"):
            SourceHealthProbe.parse_payload(
                definition("TWSE"), payload, self.valid_universe, FETCHED_AT
            )

    def test_source_failure_marks_market_symbols_as_mismatch(self):
        def failing_opener(*args, **kwargs):
            raise TimeoutError("fixture timeout")

        source_report = SourceHealthProbe(opener=failing_opener).run(
            [definition("TWSE")], self.valid_universe[:1]
        )
        validation = UniverseValidator().validate(
            self.valid_universe[:1], source_report
        )
        self.assertEqual(source_report.status, "failed")
        self.assertEqual(validation.status, "failed")
        self.assertFalse(validation.usable)
        self.assertEqual(validation.instruments[0].reason_code, "SOURCE_UNAVAILABLE")

    def test_empty_universe_fails_validation(self):
        source_report = report_for([])
        validation = UniverseValidator().validate([], source_report)
        self.assertEqual(validation.status, "failed")
        self.assertFalse(validation.usable)
        self.assertIn("EMPTY_COMPETITION_UNIVERSE", validation.errors)

    def test_same_code_in_wrong_market_does_not_count_as_coverage(self):
        universe = [
            Instrument("2330.TWO", "2330", "跨市場錯配", "TPEX", "2026-07-31")
        ]
        result = UniverseValidator().validate(universe, report_for(universe))
        self.assertEqual(result.mismatch_count, 1)
        self.assertEqual(result.instruments[0].status, "universe_mismatch")

    def test_duplicate_universe_symbol_is_rejected(self):
        content = (
            "symbol,name,market,source_date\n"
            "2330,台積電,TWSE,2026-07-31\n"
            "2330,台積電,TWSE,2026-07-31\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "重複股票"):
                load_universe(path)

    def test_current_official_universe_uses_3718(self):
        universe = load_universe(ROOT / "data" / "official_universe.csv")
        symbols = {item.symbol for item in universe}
        replacement = next(item for item in universe if item.symbol == "3718.TWO")
        self.assertEqual(len(universe), 150)
        self.assertEqual(sum(item.market == "TWSE" for item in universe), 100)
        self.assertEqual(sum(item.market == "TPEX" for item in universe), 50)
        self.assertNotIn("5371.TWO", symbols)
        self.assertEqual(replacement.name, "中光電投控")
        self.assertEqual(replacement.source_date, "2026-09-14")

    def test_5371_is_mismatch_and_3718_is_only_a_candidate(self):
        universe = self.valid_universe + [
            Instrument("5371.TWO", "5371", "中光電", "TPEX", "2026-07-31")
        ]
        result = UniverseValidator().validate(
            universe,
            report_for(universe),
            successor_candidates={"5371.TWO": "3718.TWO"},
        )
        mismatch = next(item for item in result.instruments if item.code == "5371")
        self.assertEqual(result.status, "degraded")
        self.assertFalse(result.usable)
        self.assertEqual(result.tradable_count, 2)
        self.assertEqual(result.mismatch_count, 1)
        self.assertEqual(mismatch.status, "universe_mismatch")
        self.assertEqual(mismatch.successor_candidate, "3718.TWO")
        self.assertIn("不自動替換", mismatch.evidence)

    def test_complete_validation_can_open_snapshot_gate(self):
        source_report = report_for(self.valid_universe)
        validation = UniverseValidator().validate(self.valid_universe, source_report)
        self.assertTrue(validation.usable)
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                database.upsert_instruments(connection, self.valid_universe, True)
            database.save_source_feasibility_report(source_report)
            database.save_universe_validation(validation)
            snapshot = DataAgentService(database).build_snapshot(
                "2030-01-01T00:00:00+00:00", require_prices=False
            )
        self.assertTrue(snapshot.usable)
        self.assertEqual(snapshot.universe_validation_id, validation.validation_id)
        self.assertEqual(snapshot.tradable_symbols, 2)

    def test_missing_validation_closes_snapshot_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                database.upsert_instruments(connection, self.valid_universe, True)
            snapshot = DataAgentService(database).build_snapshot(
                "2030-01-01T00:00:00+00:00", require_prices=False
            )
        self.assertFalse(snapshot.usable)
        self.assertIn("MISSING_UNIVERSE_VALIDATION", snapshot.quality_flags)

    def test_degraded_validation_closes_snapshot_gate(self):
        universe = self.valid_universe + [
            Instrument("5371.TWO", "5371", "中光電", "TPEX", "2026-07-31")
        ]
        source_report = report_for(universe)
        validation = UniverseValidator().validate(
            universe,
            source_report,
            successor_candidates={"5371.TWO": "3718.TWO"},
        )
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                database.upsert_instruments(connection, universe, True)
            database.save_source_feasibility_report(source_report)
            database.save_universe_validation(validation)
            snapshot = DataAgentService(database).build_snapshot(
                "2030-01-01T00:00:00+00:00", require_prices=False
            )
        self.assertFalse(snapshot.usable)
        self.assertEqual(snapshot.universe_mismatches, 1)
        self.assertTrue(
            any(flag.startswith("UNUSABLE_UNIVERSE_VALIDATION_") for flag in snapshot.quality_flags)
        )

    def test_replacing_universe_clears_removed_symbols(self):
        old_universe = self.valid_universe + [
            Instrument("9999.TW", "9999", "舊交易池", "TWSE", "2026-01-01")
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "test.db")
            database.initialize()
            with database.connect() as connection:
                database.upsert_instruments(connection, old_universe, True)
            with database.connect() as connection:
                database.replace_competition_universe(connection, self.valid_universe)
            with database.connect() as connection:
                active = connection.execute(
                    """
                    SELECT symbol FROM instruments
                    WHERE in_competition_universe = 1 ORDER BY symbol
                    """
                ).fetchall()
        self.assertEqual(
            [row["symbol"] for row in active],
            sorted(item.symbol for item in self.valid_universe),
        )


if __name__ == "__main__":
    unittest.main()
