import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from etf_agent.data.tpex_history import SOURCE, collect_quotes, parse_quotes
from etf_agent.data.database import MarketDataDatabase
from etf_agent.virtual_account.daily import OfficialCloseMarket


def payload(*, day="20260930", code="3105", amount="15,230,123"):
    return json.dumps({"date": day, "tables": [{"fields": ["代號", "收盤", "成交股數", "成交金額(元)", "成交筆數"],
                   "data": [[code, "50.00", "300,000", amount, "1,200"]]}]})


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.body.encode()


class TpexHistoryTests(unittest.TestCase):
    def test_rejects_wrong_date_missing_symbol_and_nonfinite(self):
        for body, message in ((payload(day="20261001"), "日期"), (payload(code="3374"), "缺少"),
                              (payload(amount="NaN"), "有限")):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                parse_quotes(body, date(2026, 9, 30), {"3105.TWO"})

    def test_stores_original_response_and_settlement_can_read_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.db"
            store = MarketDataDatabase(path)
            store.initialize()
            with store.connect() as connection:
                connection.execute("INSERT INTO instruments(symbol, code, name, market, source_date, in_competition_universe) VALUES ('3105.TWO', '3105', 'test', 'TPEX', '2026-09-30', 1)")
            result = collect_quotes(path, date(2026, 9, 30), {"3105.TWO"}, opener=lambda *_args, **_kwargs: Response(payload()))
            self.assertEqual(result["stored_rows"], 1)
            with store.connect() as connection:
                row = connection.execute("SELECT d.source, d.trade_value, r.payload_json, r.sha256 FROM daily_prices d JOIN raw_payloads r ON d.raw_payload_id = r.id").fetchone()
                self.assertEqual(row["source"], SOURCE)
                self.assertEqual(row["trade_value"], 15230123)
                self.assertEqual(row["payload_json"], payload())
            market = OfficialCloseMarket(path).build("2026-09-30", ["3105.TWO"])
            self.assertIsNotNone(market)
            self.assertEqual(market[0]["quotes"]["3105.TWO"]["execution_price"], "50.77")
