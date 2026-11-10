import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from etf_agent.data.universe import Instrument
from etf_agent.data.corporate import CorporateRecord, FinancialStatement, FinancialStatementFact, SourceDocument
from etf_agent.data.database import MarketDataDatabase
from etf_agent.data.fundamental_repair import (
    FIELDS,
    KNOWN_UNREPAIRABLE,
    redact_secret,
    run_scheduled_repair,
    stored_prior_external_ids,
    verified_prior_record,
)

TOKEN = "SECRET-TOKEN-VALUE-12345"


def make_anchor(symbol, industry="ci", fields=FIELDS, status="provided", cutoff="2026-10-03T15:00:00+08:00"):
    document = SourceDocument("TWSE_TEST", "anchor-" + symbol, "financial_statement", "財報", "{}",
                              "https://openapi.twse.com.tw", cutoff, cutoff, cutoff, symbol, "官方")
    statement = FinancialStatement(symbol, "income_statement", industry, 2026, 2, "2026-01-01", "2026-06-30",
                                   "cumulative_to_quarter", "unknown", "TWD", 1000, "2026-10-03", None, "official",
                                   tuple(FinancialStatementFact(k, k, Decimal(3), status, "TWD", 1000) for k in fields))
    return CorporateRecord(document, financial_statement=statement)


def make_raw(code, fields=FIELDS):
    raw = {"status": 200, "data": []}
    for year in (2025, 2026):
        for end, value in (("03-31", 1000), ("06-30", 2000)):
            for field, origin in fields.values():
                raw["data"].append({"stock_id": code, "date": "%s-%s" % (year, end), "type": field,
                                    "origin_name": origin, "value": value})
    return raw


class FakeFinMind:
    def __init__(self, overrides=None, error_at=None, error_message="FinMind HTTP 402；請檢查權限或額度"):
        self.overrides = overrides or {}
        self.error_at = error_at
        self.error_message = error_message
        self.calls = []

    def fetch(self, dataset, stock_id, start, end):
        self.calls.append(stock_id)
        if self.error_at == len(self.calls):
            raise RuntimeError(self.error_message)
        return json.dumps(self.overrides.get(stock_id, make_raw(stock_id)))


class ScheduledPriorRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "test.db"
        self.database = MarketDataDatabase(self.path)
        self.symbols = {"1101.TW": "TWSE", "2330.TW": "TWSE", "7828.TWO": "TPEX", "3718.TWO": "TPEX"}
        self.universe = [Instrument(symbol, symbol.split(".")[0], symbol, market, "2026-07-31")
                         for symbol, market in self.symbols.items()]

    def run_repair(self, anchors, provider, **kwargs):
        return run_scheduled_repair(self.database, self.universe, anchors, provider, database_path=self.path, **kwargs)

    def documents(self):
        with sqlite3.connect(self.path) as connection:
            return connection.execute("SELECT external_id FROM source_documents WHERE source = "
                                      "'FINMIND_VERIFIED_PRIOR' ORDER BY external_id").fetchall()

    def test_repairs_one_symbol_without_snapshot_and_is_idempotent(self):
        anchors = [make_anchor("2330.TW")]
        provider = FakeFinMind()
        summary = self.run_repair(anchors, provider)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["stored_documents"], 1)
        self.assertFalse(summary["snapshot_built"])
        self.assertEqual(self.documents(), [("2330.TW:2025:Q2",)])
        self.assertEqual(stored_prior_external_ids(self.path), {"2330.TW:2025:Q2"})
        # 重跑：已有同期文件，零次呼叫、不重複寫入。
        again = FakeFinMind()
        summary = self.run_repair(anchors, again)
        self.assertEqual(again.calls, [])
        self.assertEqual(summary["skipped_existing"], 1)
        self.assertEqual(summary["stored_documents"], 0)
        self.assertEqual(self.documents(), [("2330.TW:2025:Q2",)])

    def test_missing_database_means_nothing_stored(self):
        self.assertEqual(stored_prior_external_ids(Path(self.tmp.name) / "none.db"), frozenset())

    def test_rejects_unverifiable_data_without_writing(self):
        base = make_raw("2330")
        bad = {}
        bad["差一元"] = copy.deepcopy(base); bad["差一元"]["data"][-1]["value"] += 1
        bad["缺季"] = copy.deepcopy(base); bad["缺季"]["data"].pop()
        bad["欄位重複"] = copy.deepcopy(base); bad["欄位重複"]["data"].append(base["data"][0])
        bad["股票誤配"] = copy.deepcopy(base); bad["股票誤配"]["data"][0]["stock_id"] = "1101"
        bad["單位衝突"] = copy.deepcopy(base)
        for row in bad["單位衝突"]["data"]:
            row["value"] *= 1000
        bad["回應狀態"] = {"status": 402, "data": []}
        for name, raw in bad.items():
            with self.subTest(name):
                summary = self.run_repair([make_anchor("2330.TW")], FakeFinMind({"2330": raw}))
                self.assertEqual(summary["status"], "partial")
                self.assertEqual(len(summary["unverified"]), 1)
                self.assertEqual(self.documents(), [])

    def test_official_anchor_problems_are_rejected(self):
        template = make_anchor("2330.TW")
        statement = template.financial_statement
        anchors = {
            "官方任一項未申報": make_anchor("2330.TW", status="not_reported"),
            "期間類型衝突": replace(template, financial_statement=replace(statement, period_kind="quarter")),
            "幣別衝突": replace(template, financial_statement=replace(statement, currency="USD")),
            "非官方基準": replace(template, document=replace(template.document, source="YAHOO")),
        }
        for name, anchor in anchors.items():
            with self.subTest(name):
                summary = self.run_repair([anchor], FakeFinMind())
                self.assertEqual(summary["status"], "partial")
                self.assertEqual(self.documents(), [])

    def test_two_net_income_fields_and_future_or_naive_time_rejected(self):
        from etf_agent.data.fundamental_repair import FINANCIAL_FIELDS
        anchor = make_anchor("2330.TW", industry="fh", fields=FINANCIAL_FIELDS)
        raw = make_raw("2330", FINANCIAL_FIELDS)
        extra = make_raw("2330", {"net_income": ("IncomeAfterTaxes", "本期淨利（淨損）")})
        raw["data"].extend(extra["data"])
        summary = self.run_repair([anchor], FakeFinMind({"2330": raw}))
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(self.documents(), [])
        cutoff = "2026-10-03T15:00:00+08:00"
        for decision_cutoff in ("2026-09-29T15:00:00+08:00", "2026-10-03T15:00:00"):
            with self.subTest(decision_cutoff), self.assertRaises(ValueError):
                verified_prior_record(make_anchor("2330.TW"), make_raw("2330"), cutoff, decision_cutoff)

    def test_quota_error_stops_round_and_keeps_others_untouched(self):
        anchors = [make_anchor("1101.TW"), make_anchor("2330.TW"), make_anchor("7828.TWO")]
        provider = FakeFinMind(error_at=2)
        summary = self.run_repair(anchors, provider)
        self.assertEqual(summary["status"], "aborted")
        self.assertEqual(provider.calls, ["1101", "2330"])
        self.assertEqual(self.documents(), [("1101.TW:2025:Q2",)])
        self.assertEqual(len(summary["aborted"]), 1)

    def test_single_symbol_failure_degrades_without_stopping(self):
        bad = make_raw("1101"); bad["data"][-1]["value"] += 1
        provider = FakeFinMind({"1101": bad})
        summary = self.run_repair([make_anchor("1101.TW"), make_anchor("2330.TW")], provider)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(provider.calls, ["1101", "2330"])
        self.assertEqual(self.documents(), [("2330.TW:2025:Q2",)])

    def test_per_round_limit_defers_remaining(self):
        anchors = [make_anchor("1101.TW"), make_anchor("2330.TW"), make_anchor("7828.TWO")]
        provider = FakeFinMind()
        summary = self.run_repair(anchors, provider, max_symbols=2)
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(summary["deferred_by_limit"], 1)
        self.assertEqual(summary["status"], "partial")
        with self.assertRaises(ValueError):
            self.run_repair(anchors, provider, max_symbols=0)

    def test_known_unrepairable_tried_last_and_not_counted_as_failure(self):
        bad = make_raw("7828"); bad["data"].pop()
        anchors = [make_anchor("7828.TWO"), make_anchor("2330.TW")]
        provider = FakeFinMind({"7828": bad})
        summary = self.run_repair(anchors, provider)
        self.assertEqual(provider.calls, ["2330", "7828"])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["unverified"], [])
        self.assertEqual(summary["known_unrepairable"], {"7828.TWO": KNOWN_UNREPAIRABLE["7828.TWO"]})
        self.assertEqual(self.documents(), [("2330.TW:2025:Q2",)])

    def test_secret_never_reaches_summary_or_stored_payload(self):
        provider = FakeFinMind(error_at=1, error_message="FinMind 連線失敗 token=" + TOKEN)
        summary = self.run_repair([make_anchor("2330.TW")], provider, secret=TOKEN)
        self.assertEqual(summary["status"], "aborted")
        self.assertNotIn(TOKEN, json.dumps(summary, ensure_ascii=False))
        self.assertEqual(redact_secret("x " + TOKEN, TOKEN), "x [已遮蔽]")
        self.assertEqual(redact_secret("abc", "abc"), "abc")
        self.run_repair([make_anchor("1101.TW")], FakeFinMind(), secret=TOKEN)
        with sqlite3.connect(self.path) as connection:
            dump = "\n".join(row[0] for row in connection.execute("SELECT payload_json FROM raw_payloads"))
        self.assertNotIn(TOKEN, dump)

    def test_scheduled_does_not_create_output_directory(self):
        output = Path(self.tmp.name) / "fundamental_repairs"
        self.run_repair([make_anchor("2330.TW")], FakeFinMind())
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
