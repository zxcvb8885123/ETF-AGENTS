import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import MarketDataDatabase, SnapshotRepository
from etf_agent.decision import (
    DecisionInputBuilder,
    DecisionToolError,
    MomentumEngine,
    PortfolioDecisionApplicationService,
)

from test_portfolio_decision_agent import decision_bundle


FETCHED_AT = "2026-09-19T20:00:00+00:00"


def builder_inputs():
    """把決策 fixture 拆回 Snapshot、規則與資料庫歷史列，模擬正式輸入來源。"""
    fixture = decision_bundle()
    snapshot = copy.deepcopy(fixture["snapshot"])
    for evidence in snapshot["source_evidence"]:
        evidence["fetched_at"] = FETCHED_AT
    bars_by_symbol = {item["symbol"]: item["bars"] for item in fixture["price_series"]}
    history = []
    for row in snapshot["latest_prices"]:
        bars = bars_by_symbol[row["symbol"]]
        last = bars[-1]
        row.update(
            {
                "open_price": last["open"],
                "high_price": last["high"],
                "low_price": last["low"],
                "close_price": last["close"],
                "adjusted_close_price": None,
                "volume_shares": last["volume"],
                "trade_date": last["trade_date"],
            }
        )
        history.extend(
            {
                "symbol": row["symbol"],
                "trade_date": bar["trade_date"],
                "open_price": bar["open"],
                "high_price": bar["high"],
                "low_price": bar["low"],
                "close_price": bar["close"],
                "adjusted_close_price": None,
                "volume_shares": bar["volume"],
                "fetched_at": bar["available_at"],
            }
            for bar in bars[:-1]
        )
    snapshot["latest_trade_date"] = snapshot["latest_prices"][0]["trade_date"]
    rules = dict(fixture["rules"])
    rules.pop("config_sha256")
    # 正式決策規則未啟用 ETF 基準比較；Builder 不產生基準資料。
    rules["required_benchmark_ids"] = []
    rules["minimum_active_share"] = "0"
    return snapshot, rules, history, fixture["account_snapshot"]


def seed_history(database, rows):
    database.initialize()
    with database.connect() as connection:
        for symbol in sorted({row["symbol"] for row in rows}):
            connection.execute(
                """
                INSERT INTO instruments(symbol, code, name, market, source_date, in_competition_universe)
                VALUES (?, ?, ?, 'TWSE', '2026-09-14', 1)
                """,
                (symbol, symbol.split(".")[0], symbol),
            )
        for row in rows:
            run_id = "seed-%s-%s-%s" % (row["source"], row["symbol"], row["trade_date"])
            connection.execute(
                "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
                (run_id, row["source"], row["fetched_at"]),
            )
            payload_id = connection.execute(
                """
                INSERT INTO raw_payloads(run_id, source, endpoint, fetched_at, sha256, payload_json)
                VALUES (?, ?, 'fixture://history', ?, 'sha', '{}')
                """,
                (run_id, row["source"], row["fetched_at"]),
            ).lastrowid
            connection.execute(
                """
                INSERT INTO daily_prices(
                    symbol, trade_date, open_price, high_price, low_price, close_price,
                    adjusted_close_price, price_change, volume_shares, trade_value, transactions,
                    source, fetched_at, run_id, raw_payload_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, 0, 0, ?, ?, ?, ?)
                """,
                (
                    row["symbol"], row["trade_date"], row["open_price"], row["high_price"],
                    row["low_price"], row["close_price"], row.get("adjusted_close_price"),
                    row["volume_shares"], row["source"], row["fetched_at"], run_id, payload_id,
                ),
            )


class DecisionInputBuilderTests(unittest.TestCase):
    def test_builds_valid_bundle_that_momentum_engine_accepts(self):
        snapshot, rules, history, account = builder_inputs()
        builder = DecisionInputBuilder(snapshot, rules, history, account_snapshot=account)
        bundle = builder.build()

        self.assertEqual(builder.validate(bundle), [])
        series = {item["symbol"]: item for item in bundle["price_series"]}
        self.assertEqual(len(series["2330.TW"]["bars"]), 65)
        self.assertEqual(series["2330.TW"]["bars"][-1]["available_at"], FETCHED_AT)
        self.assertEqual(series["2330.TW"]["evidence_id"], "price-2330")
        self.assertEqual(MomentumEngine(bundle).run()["status"], "completed")

    def test_shipped_decision_rules_pass_rule_validation(self):
        snapshot, _, history, account = builder_inputs()
        rules_path = Path(__file__).resolve().parents[1] / "config" / "decision_rules.json"
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
        builder = DecisionInputBuilder(snapshot, rules, history, account_snapshot=account)
        self.assertEqual(
            [error for error in builder.validate(builder.build()) if "rules" in error],
            [],
        )

    def test_template_without_account_only_ignores_account_errors(self):
        snapshot, rules, history, _ = builder_inputs()
        builder = DecisionInputBuilder(snapshot, rules, history)
        bundle = builder.build()
        self.assertNotIn("account_snapshot", bundle)
        self.assertEqual(builder.validate(bundle), [])

        # Snapshot 只有交易狀態計數時，樣板仍須回報缺少正式交易狀態包。
        snapshot["tradable_symbols"] = 2
        builder = DecisionInputBuilder(snapshot, rules, history)
        errors = builder.validate(builder.build())
        self.assertTrue(any("trading_status_bundle" in error for error in errors))

    def test_history_on_or_after_snapshot_date_is_not_used(self):
        snapshot, rules, history, account = builder_inputs()
        latest = snapshot["latest_prices"][0]
        history.append(dict(history[-1], symbol=latest["symbol"], trade_date="2099-01-01"))
        bundle = DecisionInputBuilder(snapshot, rules, history, account_snapshot=account).build()
        dates = [
            bar["trade_date"]
            for item in bundle["price_series"]
            for bar in item["bars"]
        ]
        self.assertNotIn("2099-01-01", dates)

    def test_adjusted_close_scales_open_high_low(self):
        bar = DecisionInputBuilder._bar(
            {
                "symbol": "2330.TW",
                "trade_date": "2026-01-02",
                "open_price": "100",
                "high_price": "110",
                "low_price": "90",
                "close_price": "100",
                "adjusted_close_price": "50.0000",
                "volume_shares": 10,
            },
            FETCHED_AT,
        )
        self.assertEqual(
            (bar["open"], bar["high"], bar["low"], bar["close"]),
            ("50.0000", "55.0000", "45.0000", "50.0000"),
        )

    def test_missing_ohlc_fails_closed_instead_of_filling(self):
        snapshot, rules, history, account = builder_inputs()
        history[0]["open_price"] = None
        with self.assertRaisesRegex(DecisionToolError, "缺少 open_price"):
            DecisionInputBuilder(snapshot, rules, history, account_snapshot=account).build()


class PriceHistoryRepositoryTests(unittest.TestCase):
    def test_history_prefers_official_source_and_respects_cutoff_and_limit(self):
        base = {
            "symbol": "2330.TW", "open_price": "10", "high_price": "11",
            "low_price": "9", "close_price": "10", "volume_shares": 1,
        }
        rows = [
            dict(base, trade_date="2026-09-15", source="YAHOO_FINANCE",
                 adjusted_close_price="9.5", fetched_at="2026-09-18T00:00:00+00:00"),
            dict(base, trade_date="2026-09-16", source="YAHOO_FINANCE",
                 adjusted_close_price="9.6", fetched_at="2026-09-18T00:00:00+00:00"),
            dict(base, trade_date="2026-09-16", source="TWSE_STOCK_DAY",
                 close_price="10.5", fetched_at="2026-09-18T00:00:00+00:00"),
            dict(base, trade_date="2026-09-17", source="YAHOO_FINANCE",
                 adjusted_close_price="9.7", fetched_at="2026-09-18T00:00:00+00:00"),
            # cutoff 後才抓到的資料不得進入歷史行情。
            dict(base, trade_date="2026-09-17", source="TWSE_STOCK_DAY",
                 close_price="99", fetched_at="2026-09-30T00:00:00+00:00"),
            dict(base, trade_date="2026-09-18", source="TWSE_STOCK_DAY",
                 fetched_at="2026-09-18T00:00:00+00:00"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "market.db")
            seed_history(database, rows)
            repository = SnapshotRepository(database)
            with repository.connect() as connection:
                loaded = [
                    dict(row)
                    for row in repository.load_price_history(
                        connection, "2026-09-18", "2026-09-19T00:00:00+00:00", 2
                    )
                ]
        self.assertEqual(
            [(row["trade_date"], row["source"], row["close_price"]) for row in loaded],
            [("2026-09-16", "TWSE_STOCK_DAY", "10.5"), ("2026-09-17", "YAHOO_FINANCE", "10")],
        )

    def test_service_writes_bundle_from_snapshot_database_and_rules(self):
        snapshot, rules, history, account = builder_inputs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = MarketDataDatabase(root / "market.db")
            seed_history(database, [dict(row, source="TWSE_STOCK_DAY") for row in history])
            paths = {}
            for name, payload in (("snapshot", snapshot), ("rules", rules), ("account", account)):
                paths[name] = root / ("%s.json" % name)
                paths[name].write_text(json.dumps(payload), encoding="utf-8")
            output = root / "decision_input.json"
            result = PortfolioDecisionApplicationService.build_input_file(
                paths["snapshot"], root / "market.db", paths["rules"], output,
                account_path=paths["account"], lookback_bars=30,
            )
            self.assertTrue(result["valid"], result["errors"])
            self.assertFalse(result["template"])
            bundle = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual({len(item["bars"]) for item in bundle["price_series"]}, {30})
            self.assertTrue(
                PortfolioDecisionApplicationService.from_path(output).validate_input()["valid"]
            )


if __name__ == "__main__":
    unittest.main()
