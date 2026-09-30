import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

from etf_agent.data.database import MarketDataDatabase
from etf_agent.data.supplemental_sources import (
    FinMindFundamentalCollector,
    FinMindFundamentalProvider,
    FinMindNewsCollector,
    FinMindNewsProvider,
    SupplementalRepository,
)


class FakeResponse:
    def __init__(self, content):
        self.content = content.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.content


class SupplementalSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = MarketDataDatabase(Path(self.temp.name) / "data.db")
        self.repository = SupplementalRepository(self.database)
        self.fetched = "2026-09-29T09:00:00+00:00"

    def test_finmind_versions_and_historical_cutoff(self):
        item = {"date": "2026-06-30", "stock_id": "2330",
                "type": "Revenue", "value": 100, "origin_name": "營業收入"}
        payload = {"status": 200, "data": [item]}
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse(json.dumps(payload))

        collector = FinMindFundamentalCollector(
            self.repository, FinMindFundamentalProvider("secret", opener)
        )
        first = collector.collect("TaiwanStockFinancialStatements", "2330",
                                  date(2026, 1, 1), date(2026, 9, 29), self.fetched)
        second = collector.collect("TaiwanStockFinancialStatements", "2330",
                                   date(2026, 1, 1), date(2026, 9, 29), self.fetched)
        self.assertEqual((first.stored_rows, second.stored_rows), (1, 0))
        self.assertNotIn("secret", calls[0].full_url)
        self.assertEqual(calls[0].get_header("Authorization"), "Bearer secret")
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM supplemental_facts").fetchone()
            self.assertIsNone(row["published_at"])
            self.assertEqual(row["available_at"], self.fetched)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM supplemental_facts WHERE available_at <= ?",
                ("2026-06-30T23:59:59+00:00",),
            ).fetchone()[0], 0)
            payloads = [item[0] for item in connection.execute(
                "SELECT payload_json FROM raw_payloads"
            )]
        self.assertTrue(all("secret" not in raw for raw in payloads))
        payload["data"][0]["value"] = 101
        changed = collector.collect("TaiwanStockFinancialStatements", "2330",
                                    date(2026, 1, 1), date(2026, 9, 29), self.fetched)
        self.assertEqual(changed.stored_rows, 1)

    def test_finmind_rejects_mismatch_and_bad_numbers_without_writes(self):
        payload = {"status": 200, "data": [
            {"date": "2026-06-30", "stock_id": "2317", "type": "EPS",
             "value": "NaN", "origin_name": "EPS"}
        ]}
        provider = FinMindFundamentalProvider(
            "secret", lambda request, timeout: FakeResponse(json.dumps(payload))
        )
        collector = FinMindFundamentalCollector(self.repository, provider)
        with self.assertRaisesRegex(ValueError, "代號"):
            collector.collect("TaiwanStockFinancialStatements", "2330",
                              date(2026, 1, 1), date(2026, 9, 29), self.fetched)
        payload["data"][0]["stock_id"] = "2330"
        with self.assertRaisesRegex(ValueError, "有限"):
            collector.collect("TaiwanStockFinancialStatements", "2330",
                              date(2026, 1, 1), date(2026, 9, 29), self.fetched)
        self.assertFalse(self.database.path.exists())

    def test_finmind_missing_token_and_http_error_hide_secret(self):
        with self.assertRaisesRegex(ValueError, "FINMIND_TOKEN"):
            FinMindFundamentalProvider("", lambda *_: None).fetch(
                "TaiwanStockBalanceSheet", "2330", date(2026, 1, 1), date(2026, 9, 29)
            )

        def denied(request, timeout):
            raise HTTPError(request.full_url, 402, "token secret", None, None)

        with self.assertRaisesRegex(RuntimeError, "HTTP 402") as context:
            FinMindFundamentalProvider("secret", denied).fetch(
                "TaiwanStockBalanceSheet", "2330", date(2026, 1, 1), date(2026, 9, 29)
            )
        self.assertNotIn("secret", str(context.exception))

    def test_finmind_news_is_stock_linked_but_not_licensed_or_historically_available(self):
        item = {"date": "2026-09-29 01:04:00", "stock_id": "2330",
                "title": "台積電新聞", "link": "https://example.com/article/1#section",
                "source": "範例媒體"}
        payload = {"status": 200, "data": [item]}
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse(json.dumps(payload))

        collector = FinMindNewsCollector(
            self.repository, FinMindNewsProvider("secret", opener)
        )
        first = collector.collect("2330", date(2026, 9, 29), self.fetched)
        second = collector.collect("2330", date(2026, 9, 29), self.fetched)
        self.assertEqual((first.stored_rows, second.stored_rows), (1, 0))
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM finmind_news_candidates").fetchone()
        self.assertEqual(row["license_status"], "unverified")
        self.assertEqual(row["stock_id"], "2330")
        self.assertEqual(row["available_at"], self.fetched)
        self.assertIsNone(row["published_at"])
        self.assertEqual(row["source_time_raw"], "2026-09-29 01:04:00")
        self.assertNotIn("secret", calls[0].full_url)

        for bad_item in (
            dict(item, stock_id="2317"),
            dict(item, date="2026-09-30 01:04:00"),
            dict(item, link="javascript:alert(1)"),
        ):
            payload["data"] = [bad_item]
            with self.assertRaises(ValueError):
                collector.collect("2330", date(2026, 9, 29), self.fetched)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM finmind_news_candidates"
            ).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
