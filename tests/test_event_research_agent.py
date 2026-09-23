import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from cli.event_research import EventResearchApplication

from etf_agent.data import MarketDataDatabase
from etf_agent.research import (
    HistoricalPriceFeatureRepository,
    ResearchDebateValidator,
    ResearchResultValidator,
    ResearchToolError,
    SnapshotResearchTools,
)


ROOT = Path(__file__).resolve().parents[1]


def snapshot_fixture():
    return {
        "snapshot_id": "snapshot-1",
        "decision_cutoff": "2026-09-20T00:55:00+00:00",
        "usable": True,
        "quality_flags": [],
        "universe_size": 1,
        "latest_trade_date": "2026-09-19",
        "source_evidence": [
            {
                "evidence_id": "document-evidence-1",
                "source": "TWSE_MOPS_MONTHLY_REVENUE",
                "authority": "mops",
                "data_type": "monthly_revenue",
            },
            {
                "evidence_id": "price-evidence-1",
                "source": "YAHOO_FINANCE",
                "authority": "vendor",
                "data_type": "daily_price",
            },
        ],
        "documents": [
            {
                "document_id": 1,
                "source": "TWSE_MOPS_MONTHLY_REVENUE",
                "external_id": "monthly-revenue:2330:2026-08",
                "version": 1,
                "document_type": "monthly_revenue",
                "symbol": "2330.TW",
                "title": "台積電 2026-08 月營收",
                "body": "年增 12.34%",
                "source_url": "fixture://monthly",
                "published_at": "2026-09-14T16:00:00+00:00",
                "available_at": "2026-09-15T00:00:00+00:00",
                "content_sha256": "abc",
                "source_evidence_id": "document-evidence-1",
                "monthly_revenue": {
                    "revenue_period": "2026-08",
                    "currency": "TWD",
                    "unit_multiplier": 1000,
                    "current_revenue": "100",
                    "previous_month_revenue": "95",
                    "previous_year_revenue": "89",
                    "mom_pct": "5.26",
                    "yoy_pct": "12.34",
                    "cumulative_revenue": "800",
                    "previous_year_cumulative_revenue": "700",
                    "cumulative_yoy_pct": "14.29",
                    "note": "",
                },
            }
        ],
        "latest_prices": [
            {
                "symbol": "2330.TW",
                "trade_date": "2026-09-19",
                "analysis_close_price": "1000",
                "source_evidence_id": "price-evidence-1",
            }
        ],
    }


def valid_result():
    return {
        "schema_version": "2.1",
        "run_id": "research-run-1",
        "snapshot_id": "snapshot-1",
        "decision_cutoff": "2026-09-20T00:55:00+00:00",
        "skill_version": "2.1.0",
        "status": "completed",
        "items": [
            {
                "event_id": "monthly-revenue:2330:2026-08",
                "symbol": "2330.TW",
                "event_type": "monthly_revenue",
                "published_at": "2026-09-14T16:00:00+00:00",
                "catalyst_date": None,
                "event_summary": "八月月營收年增，但沒有市場共識資料。",
                "research_process": {
                    "fact_packet_id": "run-1:fact",
                    "bull_packet_id": "run-1:bull",
                    "bear_packet_id": "run-1:bear",
                    "adjudication_packet_id": "run-1:adjudicator",
                    "independence": "bull_bear_independent",
                },
                "direction": "positive",
                "impact_mechanism": "營收年增可能改善短期營運預期。",
                "evidence_ids": ["document-evidence-1"],
                "counter_evidence_ids": [],
                "fact_values": [
                    {
                        "name": "monthly_revenue_yoy_pct",
                        "value": "12.34",
                        "unit": "percent",
                        "period": "2026-08",
                        "evidence_id": "document-evidence-1",
                        "comparison_basis": "previous_year_same_month",
                    }
                ],
                "assessment": {
                    "evidence_quality": "verified",
                    "novelty": {
                        "classification": "new",
                        "rationale": "本期首次發布。",
                        "prior_event_ids": [],
                    },
                    "reference_frames": [
                        {
                            "kind": "prior_year_same_month",
                            "actual": "100",
                            "reference": "89",
                            "difference_pct": "12.34",
                            "unit": "TWD x 1000",
                            "is_market_expectation": False,
                            "evidence_ids": ["document-evidence-1"],
                        }
                    ],
                    "materiality": {
                        "level": "medium",
                        "affected_metrics": ["revenue"],
                        "causal_chain": [
                            {
                                "from": "八月營收",
                                "to": "第三季營收",
                                "relationship": "八月是第三季組成月份",
                                "support": "fact",
                                "evidence_ids": ["document-evidence-1"],
                            }
                        ],
                        "horizon": "within_competition",
                        "rationale": "後續確認落在比賽期間。",
                    },
                },
                "debate": {
                    "bull_case": {
                        "thesis": "營收成長可能延續。",
                        "evidence_ids": ["document-evidence-1"],
                        "assumptions": ["不是一次性認列"],
                        "failure_conditions": ["下期反轉"],
                    },
                    "bear_case": {
                        "thesis": "營收不等於獲利。",
                        "evidence_ids": ["document-evidence-1"],
                        "assumptions": ["產品組合可能惡化"],
                        "failure_conditions": ["毛利展望上修"],
                    },
                    "adjudication": {
                        "prevailing_case": "bull",
                        "rationale": "只保留營收改善，不推論獲利。",
                        "surviving_claims": ["營收動能改善"],
                        "rejected_claims": ["獲利必然改善"],
                        "unresolved_questions": ["產品組合"],
                    },
                },
                "price_confirmation": {
                    "status": "unconfirmed",
                    "interpretation": "價格尚未確認。",
                    "as_of": "2026-09-19",
                    "return_1d": 0.01,
                    "source_evidence_id": "price-evidence-1",
                },
                "risk_flags": ["NO_MARKET_EXPECTATION"],
                "uncertainties": [],
                "research_status": "candidate",
                "status_reason": "官方數字有明確比較基準。",
                "invalidation_signals": ["後續年增轉負"],
            }
        ],
        "errors": [],
    }


def valid_debate_bundle():
    common = {
        "event_id": "monthly-revenue:2330:2026-08",
        "symbol": "2330.TW",
    }
    fact_id = "debate-1:fact"
    return {
        "schema_version": "1.0",
        "run_id": "debate-1",
        "snapshot_id": "snapshot-1",
        "decision_cutoff": "2026-09-20T00:55:00+00:00",
        **common,
        "packets": [
            {
                "packet_id": fact_id,
                "role": "fact",
                **common,
                "input_packet_ids": [],
                "evidence_ids": ["document-evidence-1", "price-evidence-1"],
                "output": {
                    "event_summary": "八月營收年增 12.34%。",
                    "verified_facts": [
                        {
                            "name": "monthly_revenue_yoy_pct",
                            "value": "12.34",
                            "unit": "percent",
                            "period": "2026-08",
                            "evidence_id": "document-evidence-1",
                        }
                    ],
                    "reference_frames": [
                        {
                            "kind": "prior_year_same_month",
                            "is_market_expectation": False,
                            "evidence_ids": ["document-evidence-1"],
                        }
                    ],
                    "open_questions": ["市場共識"],
                    "price_features": {},
                },
            },
            {
                "packet_id": "debate-1:bull",
                "role": "bull",
                **common,
                "input_packet_ids": [fact_id],
                "evidence_ids": ["document-evidence-1"],
                "output": {
                    "thesis": "營收可能延續成長。",
                    "causal_chain": [],
                    "assumptions": [],
                    "catalysts": [],
                    "failure_conditions": [],
                    "unresolved_questions": [],
                },
            },
            {
                "packet_id": "debate-1:bear",
                "role": "bear",
                **common,
                "input_packet_ids": [fact_id],
                "evidence_ids": ["document-evidence-1"],
                "output": {
                    "thesis": "營收未必轉化為獲利。",
                    "causal_chain": [],
                    "assumptions": [],
                    "risks": [],
                    "failure_conditions": [],
                    "unresolved_questions": [],
                },
            },
            {
                "packet_id": "debate-1:adjudicator",
                "role": "adjudicator",
                **common,
                "input_packet_ids": [
                    fact_id,
                    "debate-1:bull",
                    "debate-1:bear",
                ],
                "evidence_ids": ["document-evidence-1"],
                "output": {
                    "prevailing_case": "indeterminate",
                    "direction": "uncertain",
                    "research_status": "pending",
                    "rationale": "獲利傳導尚未確認。",
                    "status_reason": "缺少市場共識與毛利資料。",
                    "surviving_claims": [],
                    "rejected_claims": [],
                    "unresolved_questions": [],
                },
            },
        ],
    }


class FixturePriceRepository:
    def get_features(
        self, symbol, decision_cutoff, latest_trade_date, event_published_at=None
    ):
        return {
            "symbol": symbol,
            "as_of": "2026-09-19",
            "return_1d": 0.01,
            "decision_cutoff": decision_cutoff,
        }


class SnapshotResearchToolTests(unittest.TestCase):
    def test_list_events_cli_can_archive_candidate_list(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot_path = root / "snapshot.json"
            output_path = root / "candidates.json"
            snapshot_path.write_text(
                json.dumps(snapshot_fixture(), ensure_ascii=False), encoding="utf-8"
            )

            exit_code = EventResearchApplication(ROOT).run(
                [
                    "--snapshot", str(snapshot_path), "--database", str(root / "prices.db"),
                    "list-events", "--lookback-days", "30", "--output", str(output_path),
                ]
            )

            archived = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(exit_code, 0)
        self.assertEqual(archived["snapshot_id"], snapshot_fixture()["snapshot_id"])
        self.assertEqual(len(archived["events"]), 1)

    def test_lists_and_reads_only_snapshot_events(self):
        tools = SnapshotResearchTools(snapshot_fixture())
        events = tools.list_events(lookback_days=30)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], "monthly-revenue:2330:2026-08")
        source = tools.read_source("document-evidence-1")
        self.assertEqual(source["kind"], "document")
        self.assertEqual(source["document"]["symbol"], "2330.TW")
        facts = tools.get_company_facts("2330.TW")
        self.assertEqual(facts["documents"][0]["monthly_revenue"]["yoy_pct"], "12.34")
        context = tools.analyze_event_context("document-evidence-1")
        self.assertFalse(context["market_expectation_available"])
        self.assertEqual(
            context["reference_frames"][1]["kind"], "prior_year_same_month"
        )
        self.assertIn("不是市場預期差", context["research_warning"])

    def test_event_list_limit_accepts_a_complete_small_snapshot(self):
        tools = SnapshotResearchTools(snapshot_fixture())
        self.assertEqual(len(tools.list_events(lookback_days=30, limit=1000)), 1)
        with self.assertRaisesRegex(ResearchToolError, "1～1000"):
            tools.list_events(lookback_days=30, limit=1001)

    def test_rejects_snapshot_without_evidence_bridge(self):
        snapshot = snapshot_fixture()
        snapshot["source_evidence"] = []
        with self.assertRaisesRegex(ResearchToolError, "缺少 source_evidence"):
            SnapshotResearchTools(snapshot)

    def test_validates_structured_result_and_evidence(self):
        validator = ResearchResultValidator(snapshot_fixture())
        self.assertEqual(validator.validate(valid_result()), [])

        invalid = valid_result()
        invalid["items"][0]["evidence_ids"] = ["invented"]
        errors = validator.validate(invalid)
        self.assertTrue(any("引用不存在" in error for error in errors))
        self.assertTrue(any("正式文件引用" in error for error in errors))

    def test_rejects_unverifiable_fact_and_candidate_without_invalidation(self):
        result = valid_result()
        result["items"][0]["fact_values"][0]["value"] = "999.99"
        result["items"][0]["invalidation_signals"] = []
        errors = ResearchResultValidator(snapshot_fixture()).validate(result)
        self.assertTrue(any("無法在引用文件中核對" in error for error in errors))
        self.assertTrue(any("必須提供 invalidation_signals" in error for error in errors))

    def test_candidate_requires_debate_materiality_and_expectation_disclosure(self):
        result = valid_result()
        result["items"][0]["debate"]["adjudication"]["prevailing_case"] = "bear"
        result["items"][0]["assessment"]["materiality"]["level"] = "low"
        result["items"][0]["risk_flags"] = []
        errors = ResearchResultValidator(snapshot_fixture()).validate(result)
        self.assertTrue(any("多空裁決一致" in error for error in errors))
        self.assertTrue(any("materiality" in error for error in errors))
        self.assertTrue(any("NO_MARKET_EXPECTATION" in error for error in errors))

    def test_recalculates_price_confirmation_during_cli_validation(self):
        tools = SnapshotResearchTools(
            snapshot_fixture(), price_repository=FixturePriceRepository()
        )
        self.assertEqual(tools.validate_result(valid_result()), [])
        result = valid_result()
        result["items"][0]["price_confirmation"]["return_1d"] = 0.99
        errors = tools.validate_result(result)
        self.assertTrue(any("確定性工具結果不一致" in error for error in errors))

    def test_validates_independent_subagent_debate_bundle(self):
        validator = ResearchDebateValidator(snapshot_fixture())
        self.assertEqual(validator.validate(valid_debate_bundle()), [])

        invalid = valid_debate_bundle()
        invalid["packets"][1]["input_packet_ids"].append("debate-1:bear")
        errors = validator.validate(invalid)
        self.assertTrue(any("維持多空獨立" in error for error in errors))


class HistoricalPriceFeatureTests(unittest.TestCase):
    def test_features_prefer_same_day_official_price_over_yahoo(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "prices.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO instruments(symbol, code, name, market, source_date, in_competition_universe)
                    VALUES ('2330.TW', '2330', '台積電', 'TWSE', '2026-09-01', 1)
                    """
                )
                for source, close in (("YAHOO_FINANCE", "100"), ("TWSE_STOCK_DAY_ALL", "101")):
                    run_id = "run-" + source
                    connection.execute(
                        "INSERT INTO collection_runs(run_id, source, started_at, status) VALUES (?, ?, ?, 'success')",
                        (run_id, source, "2026-09-19T10:00:00+00:00"),
                    )
                    payload_id = database.insert_raw_payload(
                        connection, run_id, source, "fixture://" + source,
                        "2026-09-19T10:00:00+00:00", "{}",
                    )
                    connection.execute(
                        """
                        INSERT INTO daily_prices(
                            symbol, trade_date, open_price, high_price, low_price,
                            close_price, adjusted_close_price, price_change,
                            volume_shares, trade_value, transactions, source,
                            fetched_at, run_id, raw_payload_id
                        ) VALUES ('2330.TW', '2026-09-19', '100', '102', '99', ?, NULL,
                                  '1', 1000, 100000, 100, ?, '2026-09-19T10:00:00+00:00', ?, ?)
                        """,
                        (close, source, run_id, payload_id),
                    )

            features = HistoricalPriceFeatureRepository(database).get_features(
                "2330.TW", "2026-09-20T00:55:00+00:00", "2026-09-19"
            )

        self.assertEqual(features["source"], "TWSE_STOCK_DAY_ALL")
        self.assertEqual(features["analysis_close_price"], "101")

    def test_features_use_only_rows_available_before_cutoff(self):
        with tempfile.TemporaryDirectory() as directory:
            database = MarketDataDatabase(Path(directory) / "prices.db")
            database.initialize()
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO instruments(symbol, code, name, market, source_date, in_competition_universe)
                    VALUES ('2330.TW', '2330', '台積電', 'TWSE', '2026-09-01', 1)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO collection_runs(run_id, source, started_at, finished_at, status)
                    VALUES ('run-1', 'YAHOO_FINANCE', '2026-09-19T10:00:00+00:00', '2026-09-19T10:01:00+00:00', 'success')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO raw_payloads(run_id, source, endpoint, fetched_at, sha256, payload_json)
                    VALUES ('run-1', 'YAHOO_FINANCE', 'fixture://prices', '2026-09-19T10:00:00+00:00', 'abc', '{}')
                    """
                )
                payload_id = connection.execute("SELECT id FROM raw_payloads").fetchone()[0]
                start = date(2026, 8, 29)
                for index in range(22):
                    trade_date = (start + timedelta(days=index)).isoformat()
                    close = 100 + index
                    connection.execute(
                        """
                        INSERT INTO daily_prices(
                            symbol, trade_date, open_price, high_price, low_price,
                            close_price, adjusted_close_price, price_change,
                            volume_shares, trade_value, transactions, source,
                            fetched_at, run_id, raw_payload_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "2330.TW", trade_date, str(close - 1), str(close + 1),
                            str(close - 2), str(close), str(close), "1",
                            1000 + index * 10, 100000, 100, "YAHOO_FINANCE",
                            "2026-09-19T10:00:00+00:00", "run-1", payload_id,
                        ),
                    )
            features = HistoricalPriceFeatureRepository(database).get_features(
                "2330.TW",
                "2026-09-20T00:55:00+00:00",
                "2026-09-19",
                "2026-09-14T16:00:00+00:00",
            )
            self.assertEqual(features["as_of"], "2026-09-19")
            self.assertEqual(features["observations"], 22)
            self.assertGreater(features["return_10d"], 0)
            self.assertGreater(features["volume_ratio_20d_median"], 1)
            self.assertEqual(features["event_trade_date"], "2026-09-15")
            self.assertIsNotNone(features["event_day_return"])


if __name__ == "__main__":
    unittest.main()
