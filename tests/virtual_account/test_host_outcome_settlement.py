"""主辦方 DataPack 結果當地面真相：prev_day_orders 為 voided 的決策結算為無成交；policy 帶 void_batch 時現金不足整批作廢。"""

import copy
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.decision import DecisionRepository, decision_bundle_sha256, decision_policy_sha256
from etf_agent.decision.sizing import INSUFFICIENT_FUNDS_VOID_BATCH
from etf_agent.ledger import HOST_VOIDED, INSUFFICIENT_FUNDS_VOIDED
from etf_agent.virtual_account import VirtualAccountError, VirtualAccountRepository, VirtualAccountService
from etf_agent.virtual_account.daily import DailyAccountRunner, find_host_outcome

from test_virtual_account_daily import create_prices


TEAM = "TEAM_T"
TRADE_DATE = "2026-09-21"


def datapack(outcome="voided", reasons=("insufficient_funds",), team=TEAM, as_of=TRADE_DATE, generated="2026-09-21T18:00:00+08:00"):
    return {
        "schema_version": "3.0", "doc_type": "DataPack", "team_id": team,
        "trade_date": "2026-09-22", "as_of_date": as_of, "generated_at": generated,
        "missing": [], "prev_day_orders": {"outcome": outcome, "void_reasons": list(reasons), "excluded_codes": []},
        "account": {"cash": 1000000000.0, "cash_ratio_tradable": 1.0, "nav_tradable": 1000000000.0,
                    "aum": 1000000000.0, "receivable_cash": 0.0, "holdings_count": 0},
        "holdings": [], "compliance": {"violations": 0, "notices": 0, "incidents": []},
    }


def prepare_and_decide(service, root, sizing_extra=None):
    """同 test_virtual_account.prepare_and_decide，但可在綁定後的 policy 加版本閘門（例如 insufficient_funds_policy）。"""
    from test_portfolio_risk_decision import policy
    from test_team_chain import finalize, team_world, tradable_bundle
    from test_trader import items as trade_items

    bundle = tradable_bundle()
    snapshot = bundle["snapshot"]
    snapshot["decision_cutoff"] = "2026-09-20T00:55:00+00:00"
    bundle["decision_cutoff"] = snapshot["decision_cutoff"]
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    snapshot_path = root / "decision-snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    prepared = service.prepare_day(snapshot_path, "prepare-with-orders")
    bundle["account_snapshot"] = prepared["account_snapshot"]
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    decisions = trade_items()
    decisions[1] = dict(decisions[1], intent="no_trade")
    settings = policy()
    settings["liquidity_fill_rate"] = "1"
    bundle, momentum, debate, intent, bound, team, _, _ = team_world(bundle, decisions, settings)
    if sizing_extra:
        bound = copy.deepcopy(bound)
        bound["position_sizing"].update(sizing_extra)
        bound["content_sha256"] = decision_policy_sha256(bound)
    artifacts, decision = finalize(bundle, momentum, debate, intent, bound, team)
    assert decision["status"] == "approved", decision["errors"]
    decision_root = root / "decisions"
    DecisionRepository(decision_root).save("decision-1", {
        "decision_input": bundle, "policy": bound, "momentum": momentum,
        "debate": debate, "intent": intent, "decision": decision, "team_inputs": team, **artifacts,
    })
    return decision, decision_root


class FindHostOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, pack):
        (self.dir / name).write_text(json.dumps(pack), encoding="utf-8")

    def test_matches_team_and_as_of_date_and_keeps_newest(self):
        self.write("a.json", datapack(outcome="executed", reasons=(), generated="2026-09-21T17:00:00+08:00"))
        self.write("b.json", datapack(generated="2026-09-21T18:30:00+08:00"))
        self.write("other-team.json", datapack(team="TEAM_OTHER"))
        self.write("other-day.json", datapack(as_of="2026-09-18", generated="2026-09-21T19:00:00+08:00"))
        self.write("broken.json", {"doc_type": "DataPack"})
        found = find_host_outcome(self.dir, TEAM, TRADE_DATE)
        self.assertEqual(found["outcome"], "voided")
        self.assertEqual(found["void_reasons"], ["insufficient_funds"])
        self.assertEqual((found["as_of_date"], found["trade_date"]), (TRADE_DATE, "2026-09-22"))
        self.assertTrue(found["datapack_path"].endswith("b.json"))
        self.assertEqual(len(found["datapack_sha256"]), 64)

    def test_missing_inputs_mean_no_host_outcome(self):
        self.assertIsNone(find_host_outcome(None, TEAM, TRADE_DATE))
        self.assertIsNone(find_host_outcome(self.dir, None, TRADE_DATE))
        self.assertIsNone(find_host_outcome(self.dir / "nope", TEAM, TRADE_DATE))
        self.write("a.json", datapack(team="TEAM_OTHER"))
        self.assertIsNone(find_host_outcome(self.dir, TEAM, TRADE_DATE))


class HostOutcomeSettlementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rules = self.root / "rules.json"
        rules.write_text(json.dumps({"initial_capital_twd": 1_000_000_000, "version": "fixture-v1"}), encoding="utf-8")
        self.repository = VirtualAccountRepository(self.root / "accounts", "cup-test")
        self.service = VirtualAccountService(self.repository)
        self.service.initialize(rules, "cup-test", "2026-09-18T17:00:00+08:00")
        self.database = self.root / "prices.db"
        self.datapacks = self.root / "datapack"
        self.datapacks.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def prices(self, price_2317=120):
        create_prices(self.database, [
            ("2317.TW", TRADE_DATE, "120", 1_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00", int(price_2317) * 1_000),
            ("2330.TW", TRADE_DATE, "500", 50_000_000, "TWSE_STOCK_DAY_ALL", "2026-09-21T12:00:00+00:00"),
        ])

    def runner(self, decisions, with_datapacks=True):
        return DailyAccountRunner(self.repository, self.database, decisions,
                                  datapack_dir=self.datapacks if with_datapacks else None, team_id=TEAM if with_datapacks else None)

    def test_voided_datapack_settles_the_decision_as_no_fill(self):
        decision, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        (self.datapacks / "pack.json").write_text(json.dumps(datapack()), encoding="utf-8")
        result = self.runner(decisions).settle()
        self.assertEqual((result["status"], result["fills"], result["unfilled"], result["host_outcome"]), ("settled", 0, 1, "voided"))
        state = self.repository.latest()["state"]
        self.assertEqual(state["positions"], [])
        self.assertEqual(Decimal(state["settled_cash"]), Decimal(1_000_000_000))
        self.assertEqual(state["provenance"]["type"], "close")
        self.assertEqual(state["provenance"]["host_outcome"]["outcome"], "voided")
        self.assertEqual(len(state["provenance"]["host_outcome"]["datapack_sha256"]), 64)
        transition = json.loads((self.repository.root / "runs" / result["run_id"] / "transition.json").read_text(encoding="utf-8"))
        self.assertEqual(transition["execution"]["fills"], [])
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["reason"], HOST_VOIDED)
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["unfilled_shares"], decision["orders"][0]["shares"])
        self.assertEqual(transition["execution"]["voided"]["void_reasons"], ["insufficient_funds"])

    def test_executed_datapack_fills_normally_and_records_the_outcome(self):
        decision, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        (self.datapacks / "pack.json").write_text(json.dumps(datapack(outcome="executed", reasons=())), encoding="utf-8")
        result = self.runner(decisions).settle()
        self.assertEqual((result["fills"], result["unfilled"], result["host_outcome"]), (1, 0, "executed"))
        state = self.repository.latest()["state"]
        self.assertEqual(state["positions"][0]["shares"], decision["orders"][0]["shares"])
        self.assertEqual(state["provenance"]["host_outcome"]["outcome"], "executed")

    def test_without_datapack_dir_old_behaviour_and_state_shape_are_unchanged(self):
        decision, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        (self.datapacks / "pack.json").write_text(json.dumps(datapack()), encoding="utf-8")
        result = self.runner(decisions, with_datapacks=False).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (1, 0))
        self.assertNotIn("host_outcome", result)
        state = self.repository.latest()["state"]
        self.assertNotIn("host_outcome", state["provenance"])
        self.assertEqual(state["positions"][0]["shares"], decision["orders"][0]["shares"])

    def test_host_outcome_for_another_day_or_malformed_fails_closed(self):
        _, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        runner = self.runner(decisions, with_datapacks=False)
        base = dict(outcome="voided", void_reasons=["insufficient_funds"], excluded_codes=[], datapack_sha256="a" * 64,
                    datapack_path="x.json", trade_date="2026-09-22", as_of_date="2026-09-18", generated_at="2026-09-21T18:00:00+08:00")
        markets = runner.market.build(TRADE_DATE, ["2317.TW", "2330.TW"])
        inputs = self.root / "inputs"
        inputs.mkdir()
        execution_path, close_path = inputs / "execution.json", inputs / "close.json"
        execution_path.write_text(json.dumps(markets[0]), encoding="utf-8")
        close_path.write_text(json.dumps(markets[1]), encoding="utf-8")
        cases = [
            (base, "不一致"),
            ({**base, "as_of_date": TRADE_DATE, "outcome": "cancelled"}, "outcome"),
            ({**base, "as_of_date": TRADE_DATE, "void_reasons": []}, "void_reasons"),
            ({**base, "as_of_date": TRADE_DATE, "extra": 1}, "未知欄位"),
            ({**base, "as_of_date": TRADE_DATE, "datapack_sha256": ""}, "datapack_sha256"),
            ("voided", "必須是物件"),
        ]
        for host, message in cases:
            with self.subTest(host=host):
                with self.assertRaisesRegex(VirtualAccountError, message):
                    self.service.apply_decision(decisions, "decision-1", execution_path, close_path, "2026-09-23", "close-bad", host_outcome=host)
        self.assertEqual(self.repository.latest()["run_id"], "prepare-with-orders")

    def test_void_batch_policy_voids_the_whole_batch_when_cash_is_short(self):
        decision, decisions = prepare_and_decide(self.service, self.root, {"insufficient_funds_policy": INSUFFICIENT_FUNDS_VOID_BATCH})
        shares = decision["orders"][0]["shares"]
        # 成交均價漲到讓整筆買單超過 10 億買力：主辦方會整批作廢，帳本照辦。
        self.prices(price_2317=int(Decimal(1_000_000_000) / shares * 2))
        result = self.runner(decisions, with_datapacks=False).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (0, 1))
        transition = json.loads((self.repository.root / "runs" / result["run_id"] / "transition.json").read_text(encoding="utf-8"))
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["reason"], INSUFFICIENT_FUNDS_VOIDED)
        self.assertEqual(self.repository.latest()["state"]["positions"], [])

    def test_without_the_policy_cash_shortfall_still_reduces_lots(self):
        decision, decisions = prepare_and_decide(self.service, self.root)
        shares = decision["orders"][0]["shares"]
        self.prices(price_2317=int(Decimal(1_000_000_000) / shares * 2))
        result = self.runner(decisions, with_datapacks=False).settle()
        self.assertEqual((result["fills"], result["unfilled"]), (1, 1))
        transition = json.loads((self.repository.root / "runs" / result["run_id"] / "transition.json").read_text(encoding="utf-8"))
        self.assertEqual(transition["execution"]["unfilled_orders"][0]["reason"], "LIQUIDITY_OR_CASH")
        self.assertGreater(self.repository.latest()["state"]["positions"][0]["shares"], 0)


if __name__ == "__main__":
    unittest.main()
