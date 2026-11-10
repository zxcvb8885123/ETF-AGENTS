import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.data.valuation import ValuationCollector, ValuationError, parse_roc_date, validate_valuation_run, valuation_store
from etf_agent.data.valuation.contracts import normalize_metric


def official(rows, day="1151109"):
    return json.dumps([{"Date": day, "Code": c, "Name": "x", "PEratio": per, "PBratio": pbr, "DividendYield": dy}
                       for c, per, pbr, dy in rows])


def finmind(code, rows):
    return json.dumps({"status": 200, "data": [
        {"date": d, "stock_id": code, "PER": per, "PBR": pbr, "dividend_yield": dy} for d, per, pbr, dy in rows]})


class FakeOfficial:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self):
        return self.raw


class FakeFinMind:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def fetch_per(self, code, start, end):
        self.calls.append((code, start, end))
        value = self.responses[code]
        if isinstance(value, Exception):
            raise value
        return value


CLOCK = lambda: datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)


class ValuationCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def collect(self, raw, responses, codes, **kwargs):
        fm = FakeFinMind(responses)
        summary = ValuationCollector(self.root, FakeOfficial(raw), fm, CLOCK).collect(codes, **kwargs)
        return summary, fm

    def records(self, summary):
        run_dir = valuation_store(self.root).verify(summary["run_id"])
        return {r["code"]: r for r in json.loads((run_dir / "records.json").read_text())["records"]}

    def test_roc_date_conversion_and_validation(self):
        self.assertEqual(parse_roc_date("1151109"), date(2026, 11, 9))
        for bad in ("115119", "1151331", "20261109", "", None, "11511a9"):
            with self.assertRaises(ValuationError):
                parse_roc_date(bad)

    def test_matching_values_are_accepted_and_history_is_marked(self):
        raw = official([("2308", "34.83", "9.47", "1.00")])
        fm = finmind("2308", [("2026-11-06", "36.1", "9.9", "0.9"), ("2026-11-09", "34.83", "9.47", "1.0")])
        summary, _ = self.collect(raw, {"2308": fm}, ["2308"])
        record = self.records(summary)["2308"]
        self.assertEqual(record["status"], "matched")
        self.assertEqual(record["values"], {"per": "34.83", "pbr": "9.47", "dividend_yield": "1"})
        self.assertEqual(record["available_at"], "2026-11-10T00:30:00+00:00")
        self.assertIsNone(record["published_at"])
        self.assertEqual(record["license_status"], "approved")
        run_dir = valuation_store(self.root).verify(summary["run_id"])
        history = json.loads((run_dir / "history.json").read_text())["history"]
        self.assertEqual([h["verification"] for h in history], ["unverified_history", "verified_against_official"])
        self.assertEqual(validate_valuation_run(self.root, summary["run_id"])["matched"], 1)

    def test_mismatch_missing_and_unavailable_are_rejected_without_filling(self):
        raw = official([("1101", "10.00", "1.00", "3.00"), ("1102", "10.00", "1.00", "3.00"),
                        ("1103", "10.00", "1.00", "3.00")])
        responses = {
            "1101": finmind("1101", [("2026-11-09", "10.01", "1.00", "3.00")]),
            "1102": finmind("1102", [("2026-11-06", "10.00", "1.00", "3.00")]),
            "1103": finmind("1103", [("2026-11-09", "10.00", "1.00", "3.00")]),
        }
        summary, _ = self.collect(raw, responses, ["1101", "1102", "1103"])
        records = self.records(summary)
        self.assertEqual(records["1101"]["reject_reason"], "VALUE_MISMATCH")
        self.assertEqual(records["1101"]["mismatched_metrics"], ["per"])
        self.assertEqual(records["1102"]["reject_reason"], "FINMIND_MISSING_DATE")
        self.assertEqual(records["1103"]["status"], "matched")
        self.assertEqual(records["1101"]["values"], {"per": None, "pbr": None, "dividend_yield": None})

    def test_symbol_missing_in_official_and_quota_error_stops_calls(self):
        raw = official([("1101", "10", "1", "3"), ("1102", "10", "1", "3")])
        summary, fm = self.collect(raw, {"1101": RuntimeError("FinMind HTTP 402；請檢查權限或額度"), "1102": "{}", "9999": "{}"},
                                   ["1101", "1102", "9999"])
        records = self.records(summary)
        self.assertEqual(len(fm.calls), 1)
        self.assertEqual(records["1101"]["reject_reason"], "FINMIND_UNAVAILABLE")
        self.assertEqual(records["1102"]["reject_reason"], "FINMIND_UNAVAILABLE")
        self.assertEqual(records["9999"]["reject_reason"], "OFFICIAL_MISSING_SYMBOL")

    def test_empty_zero_and_negative_per_become_null_with_reason(self):
        self.assertEqual(normalize_metric("per", ""), (None, "NOT_PROVIDED"))
        self.assertEqual(normalize_metric("per", "0"), (None, "NONPOSITIVE"))
        self.assertEqual(normalize_metric("per", "-3.2"), (None, "NONPOSITIVE"))
        self.assertEqual(normalize_metric("pbr", 0), (None, "NONPOSITIVE"))
        value, reason = normalize_metric("dividend_yield", "0")
        self.assertEqual((str(value), reason), ("0.00", None))
        raw = official([("2002", "", "1.20", "0")])
        fm = finmind("2002", [("2026-11-09", 0, "1.2", "0")])
        summary, _ = self.collect(raw, {"2002": fm}, ["2002"])
        record = self.records(summary)["2002"]
        self.assertEqual(record["status"], "matched")
        self.assertIsNone(record["values"]["per"])
        self.assertEqual(record["null_reasons"], {"per": "NOT_PROVIDED"})

    def test_official_structure_problems_reject_whole_run_and_store_nothing(self):
        cases = [
            json.dumps([{"Date": "1151109", "Code": "1101", "PEratio": "1"}]),  # 欄位名變更
            official([("1101", "1", "1", "1")], day="1171109"),  # 資料日晚於抓取日
            json.dumps([{"Date": "1151109", "Code": "1101", "PEratio": "1", "PBratio": "1", "DividendYield": "1"},
                        {"Date": "1151106", "Code": "1102", "PEratio": "1", "PBratio": "1", "DividendYield": "1"}]),
            "not json",
        ]
        for raw in cases:
            with self.assertRaises(ValuationError):
                self.collect(raw, {}, ["1101"])
        self.assertEqual(list(self.root.rglob("manifest.json")), [])

    def test_rerun_keeps_both_versions_and_same_run_id_cannot_be_overwritten(self):
        raw = official([("1101", "10", "1", "3")])
        responses = {"1101": finmind("1101", [("2026-11-09", "10", "1", "3")])}
        summary, _ = self.collect(raw, responses, ["1101"])
        later = ValuationCollector(self.root, FakeOfficial(raw.replace('"3"', '"4"')), FakeFinMind(
            {"1101": finmind("1101", [("2026-11-09", "10", "1", "4")])}),
            lambda: datetime(2026, 11, 10, 0, 31, tzinfo=timezone.utc)).collect(["1101"])
        self.assertNotEqual(summary["run_id"], later["run_id"])
        self.assertEqual(len(list(self.root.rglob("manifest.json"))), 2)
        # 同一秒、不同內容：拒絕覆蓋（更正須另存新 run）
        with self.assertRaises(ValuationError):
            self.collect(raw.replace('"3"', '"5"'), {"1101": finmind("1101", [("2026-11-09", "10", "1", "5")])}, ["1101"])

    def test_validator_detects_tampering_with_records_and_raw(self):
        raw = official([("1101", "10", "1", "3")])
        summary, _ = self.collect(raw, {"1101": finmind("1101", [("2026-11-09", "10", "1", "3")])}, ["1101"])
        run_dir = valuation_store(self.root).verify(summary["run_id"])
        path = run_dir / "records.json"
        original = path.read_text()
        data = json.loads(original)
        data["records"][0]["values"]["per"] = "1"
        # 改檔後 manifest 雜湊不符，先被封存檢查擋下
        path.write_text(json.dumps(data))
        with self.assertRaises(ValuationError):
            validate_valuation_run(self.root, summary["run_id"])
        # 連 manifest 一起重封也要被重建比對擋下
        manifest = json.loads((run_dir / "manifest.json").read_text())
        manifest["artifacts"]["records"]["sha256"] = canonical_sha256(data)
        manifest.pop("manifest_sha256")
        manifest["manifest_sha256"] = canonical_sha256(manifest)
        (run_dir / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ValuationError):
            validate_valuation_run(self.root, summary["run_id"])

    def test_history_start_default_and_explicit(self):
        raw = official([("1101", "10", "1", "3")])
        responses = {"1101": finmind("1101", [("2026-11-09", "10", "1", "3")])}
        _, fm = self.collect(raw, responses, ["1101"])
        self.assertEqual(fm.calls[0][1:], (date(2026, 10, 26), date(2026, 11, 9)))
        self.root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        _, fm = self.collect(raw, responses, ["1101"], history_start=date(2019, 1, 2))
        self.assertEqual(fm.calls[0][1], date(2019, 1, 2))
        with self.assertRaises(ValuationError):
            self.collect(raw, responses, ["1101"], history_start=date(2026, 12, 1))

    def test_listed_only(self):
        from types import SimpleNamespace
        from etf_agent.data.valuation import listed_codes
        universe = [SimpleNamespace(symbol="2330.TW", code="2330"), SimpleNamespace(symbol="3718.TWO", code="3718")]
        self.assertEqual(listed_codes(universe), ["2330"])


if __name__ == "__main__":
    unittest.main()
