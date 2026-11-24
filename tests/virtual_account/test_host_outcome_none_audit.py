"""主辦方 outcome=none（前一交易日未繳交）也結算為無成交；資料包晚到時只偵測、不改寫已封存紀錄；持股歷史帶交易日。"""

import json
import unittest

from etf_agent.ledger import HOST_NOT_SUBMITTED

from test_host_outcome_settlement import (  # noqa: F401  (沿用同一組夾具)
    HostOutcomeSettlementTests, TRADE_DATE, datapack, prepare_and_decide,
)


class HostOutcomeNoneTests(HostOutcomeSettlementTests):
    # 繼承只為重用 setUp／prices／runner；清掉父類別的測試方法，避免重跑。
    for _name in [n for n in dir(HostOutcomeSettlementTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def write_pack(self, **kwargs):
        (self.datapacks / "pack.json").write_text(json.dumps(datapack(**kwargs)), encoding="utf-8")

    def transition(self, run_id):
        return json.loads((self.repository.root / "runs" / run_id / "transition.json").read_text(encoding="utf-8"))

    def test_none_outcome_settles_as_no_fill_like_voided(self):
        decision, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        self.write_pack(outcome="none", reasons=())
        result = self.runner(decisions).settle()
        self.assertEqual((result["status"], result["fills"], result["unfilled"], result["host_outcome"]), ("settled", 0, 1, "none"))
        state = self.repository.latest()["state"]
        self.assertEqual(state["positions"], [])
        self.assertEqual(state["provenance"]["host_outcome"]["outcome"], "none")
        order = self.transition(result["run_id"])["execution"]["unfilled_orders"][0]
        self.assertEqual((order["reason"], order["unfilled_shares"]), (HOST_NOT_SUBMITTED, decision["orders"][0]["shares"]))

    def test_unknown_outcome_still_simulates_fills(self):
        _, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        self.write_pack(outcome="unknown", reasons=())
        self.assertEqual(self.runner(decisions).settle()["fills"], 1)

    def test_late_datapack_is_detected_without_rewriting_sealed_records(self):
        _, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        settled = self.runner(decisions, with_datapacks=False).settle()  # 結算時資料包還沒到
        self.assertEqual(settled["fills"], 1)
        before = self.repository.latest()
        self.assertEqual(self.runner(decisions).audit_host_outcome()["status"], "no_datapack")
        self.write_pack(outcome="none", reasons=())
        audit = self.runner(decisions).audit_host_outcome()
        self.assertEqual((audit["status"], audit["datapack_outcome"], audit["simulated_fills"]), ("conflict", "none", 1))
        self.assertIn("--rebase-to-datapack", audit["message"])
        self.write_pack(outcome="voided")
        self.assertEqual(self.runner(decisions).audit_host_outcome()["status"], "conflict")
        self.write_pack(outcome="executed", reasons=())
        self.assertEqual(self.runner(decisions).audit_host_outcome()["status"], "ok")
        self.assertEqual(self.repository.latest(), before)  # 只讀

    def test_recorded_outcome_matching_datapack_is_ok(self):
        _, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        self.write_pack(outcome="none", reasons=())
        self.runner(decisions).settle()
        self.assertEqual(self.runner(decisions).audit_host_outcome()["status"], "ok")

    def test_holdings_history_carries_trade_date_not_fetch_time(self):
        _, decisions = prepare_and_decide(self.service, self.root)
        self.prices()
        self.runner(decisions, with_datapacks=False).settle()
        history = self.repository.holdings_history()
        self.assertEqual(history[-1]["trade_date"], TRADE_DATE)
        self.assertIsNone(history[0]["trade_date"])  # 開帳狀態沒有交易日，不猜


if __name__ == "__main__":
    unittest.main()
