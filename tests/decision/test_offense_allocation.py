"""position_sizing.offense v1 的配置層（AllocationOrderEngine._offense_limits）直接測試。

覆蓋：beta_stop 到線後其後的曝險配對不取用、max_size 只縮不放（OFFENSE_SIZE_CAPPED）、max_exposure_pairs 上限（probe 的 exposure_cap）、
OFFENSE:<等級>／OFFENSE_SUPPRESSED:<原因> 旗標、被壓回 keep_defensive 時曝險配對全部取消且換出者不賣、組合 beta 缺值時取消（fail-closed；以真實缺行情的持股／換入者走 BetaModel，不用 mock）、
沒有 offense 時配置逐位元不變。合成世界是機制測試，數值只放在測試內。
"""

import copy
import unittest

from etf_agent.core import canonical_sha256
from etf_agent.decision import AllocationOrderEngine, decision_bundle_sha256, decision_policy_sha256

from test_agent_swap_allocation import four_symbol_bundle, four_symbol_items, v2_world
from test_entry_reserve import RESERVE
from test_entry_reserve import VACANT
from test_exposure_swap_allocation import LIMITS, TWO_PAIRS
from test_proceeds_to_add import ADD, BUYER, VICTIM, items
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_offense import OFFENSE


class OffenseAllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="736000", positions={"2330.TW": 1000, "1101.TW": 1000}, **LIMITS)
        cls.intent, base, _, _ = v2_world(cls.bundle, four_symbol_items(), pairs=TWO_PAIRS, policy_fields=LIMITS)
        cls.base = copy.deepcopy(base)
        sizing = cls.base["position_sizing"]
        for pair in sizing["agent_swap"]["nominated_pairs"]:
            pair["purpose"] = "exposure"
        sizing["agent_swap"]["exposure"] = {"version": "v2", "max_pairs_by_view": {"probe": 1, "start_rebuild": 2, "rebuild": 2}, "view": "keep_defensive"}
        sizing["exposure_view"] = {"version": "v2"}
        sizing["market_index_facts"] = dict(MARKET_INDEX_GATE)

    def run_offense(self, view="rebuild", effective=None, stop=None, max_size=None, size="large", reason=None, bundle=None):
        policy = copy.deepcopy(self.base)
        sizing = policy["position_sizing"]
        sizing["agent_swap"]["entry_reserve"] = dict(RESERVE, max_entry_weight="0.2")
        for pair in sizing["agent_swap"]["nominated_pairs"]:
            pair["size"] = size
        offense = copy.deepcopy(OFFENSE)
        offense["offense_off_drawdown"] = "0.08"  # 世界的 defensive_at 是 0.10
        offense["by_view"]["probe"]["max_exposure_pairs"] = 1
        offense["by_view"]["start_rebuild"]["max_exposure_pairs"] = 2
        offense["by_view"]["rebuild"]["max_exposure_pairs"] = 2
        if stop:
            for row in offense["by_view"].values():
                row["beta_stop"] = stop
        if max_size:
            for row in offense["by_view"].values():
                row["max_size"] = max_size
        effective = effective or view
        sizing["offense"] = dict(offense, view=view, effective_view=effective, suppressed_reason=reason, cash_loosening=None)
        sizing["agent_swap"]["exposure"]["view"] = effective
        policy["content_sha256"] = decision_policy_sha256(policy)
        return AllocationOrderEngine(bundle or self.bundle, policy).run(self.intent)

    @staticmethod
    def flags(body):
        return {(f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith(("OFFENSE", "AGENT_SWAP"))}

    @staticmethod
    def sides(body):
        return {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]}

    def test_generous_beta_stop_takes_both_pairs_and_flags_the_level(self):
        body = self.run_offense("start_rebuild", stop="0.6")
        self.assertEqual(self.sides(body), {("2330.TW", "sell"), ("2317.TW", "buy"), ("1101.TW", "sell"), ("2412.TW", "buy")})
        self.assertIn(("PORTFOLIO", "OFFENSE:start_rebuild"), self.flags(body))

    def test_once_the_estimate_reaches_beta_stop_later_exposure_pairs_are_not_taken(self):
        body = self.run_offense("start_rebuild", stop="0.2")
        self.assertIn(("2412.TW", "AGENT_SWAP_CANCELLED:offense_beta_stop"), self.flags(body))
        self.assertEqual(self.sides(body), {("2330.TW", "sell"), ("2317.TW", "buy")})  # 取消的組：換出者不賣、換入者不買
        self.assertNotIn(("1101.TW", "sell"), self.sides(body))

    def test_a_portfolio_already_at_the_stop_takes_no_exposure_pair(self):
        body = self.run_offense("start_rebuild", stop="0.05")
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertEqual({code for _, code in self.flags(body) if code.startswith("AGENT_SWAP_CANCELLED")}, {"AGENT_SWAP_CANCELLED:offense_beta_stop"})

    def test_max_size_only_shrinks_and_a_too_small_lot_cancels_the_pair(self):
        capped = self.run_offense("start_rebuild", max_size="medium", stop="0.6")
        self.assertIn(("2317.TW", "OFFENSE_SIZE_CAPPED"), self.flags(capped))
        small = self.run_offense("rebuild", max_size="small")
        self.assertIn(("2317.TW", "OFFENSE_SIZE_CAPPED"), self.flags(small))
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:entry_reserve_lot"), self.flags(small))
        self.assertNotIn(("2330.TW", "sell"), self.sides(small))  # 不會只賣不買
        same = self.run_offense("rebuild", max_size="large", size="small")  # 交易 Agent 比上限小：不放大
        self.assertNotIn(("2317.TW", "OFFENSE_SIZE_CAPPED"), self.flags(same))

    def test_probe_takes_at_most_its_pair_cap(self):
        body = self.run_offense("probe")
        self.assertIn(("2412.TW", "AGENT_SWAP_CANCELLED:exposure_cap"), self.flags(body))
        self.assertIn(("PORTFOLIO", "OFFENSE:probe"), self.flags(body))
        self.assertEqual(self.sides(body), {("2330.TW", "sell"), ("2317.TW", "buy")})

    def test_a_suppressed_view_is_keep_defensive_and_says_why(self):
        body = self.run_offense("rebuild", effective="keep_defensive", reason="facts_unavailable")
        self.assertIn(("PORTFOLIO", "OFFENSE_SUPPRESSED:facts_unavailable"), self.flags(body))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertTrue({"AGENT_SWAP_CANCELLED:exposure_view"} <= {code for _, code in self.flags(body)})

    def bundle_with_a_gap(self, symbol):
        """真實缺行情：該檔在 beta 視窗內少一天收盤（覆蓋不足，beta 為 null；不補值），其餘行情與雜湊照常重算。"""
        bundle = copy.deepcopy(self.bundle)
        series = next(item for item in bundle["price_series"] if item["symbol"] == symbol)
        del series["bars"][-3]
        series["series_sha256"] = canonical_sha256(series["bars"])
        bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
        return bundle

    def test_a_held_symbol_with_missing_quotes_cancels_the_exposure_pairs_fail_closed(self):
        """持股 1101.TW 缺一天收盤：走真實 beta 計算（BetaModel）得到 null，組合 beta 起點不可用，所有曝險配對取消。"""
        self.assertEqual(self.sides(self.run_offense("rebuild", stop="0.6")),
                         {("2330.TW", "sell"), ("2317.TW", "buy"), ("1101.TW", "sell"), ("2412.TW", "buy")})  # 對照：行情完整時照常成交
        body = self.run_offense("rebuild", stop="0.6", bundle=self.bundle_with_a_gap("1101.TW"))
        self.assertEqual(
            {(symbol, code) for symbol, code in self.flags(body) if code.startswith("AGENT_SWAP_CANCELLED")},
            {("2317.TW", "AGENT_SWAP_CANCELLED:offense_beta_unavailable"), ("2412.TW", "AGENT_SWAP_CANCELLED:offense_beta_unavailable")})
        self.assertEqual(body["order_proposal"]["orders"], [])  # 缺資料永不放行；換出者不賣，不會只賣不買

    def test_a_candidate_with_missing_quotes_cancels_only_its_own_pair(self):
        """換入者 2317.TW 缺一天收盤：該組取消（換出者 2330.TW 不賣），另一組照常；仍不會只賣不買。"""
        body = self.run_offense("rebuild", stop="0.6", bundle=self.bundle_with_a_gap("2317.TW"))
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:offense_beta_unavailable"), self.flags(body))
        self.assertNotIn(("2412.TW", "AGENT_SWAP_CANCELLED:offense_beta_unavailable"), self.flags(body))
        self.assertEqual(self.sides(body), {("1101.TW", "sell"), ("2412.TW", "buy")})


class OffenseVacantSlotTests(unittest.TestCase):
    """審查 A1：有空名額（持 2 檔、上限 4 檔）且換入者目標不足一張時，offense 的份量格上限與曝險對數上限仍然生效
    （修正前換入者被標 AGENT_SWAP_UNNEEDED，offense／entry_reserve 整個被繞過）。"""

    @classmethod
    def setUpClass(cls):
        cls.bundle = four_symbol_bundle(cash="60000", positions={ADD: 10000, VICTIM: 2000}, **VACANT)
        cls.intent, base, _, _ = v2_world(cls.bundle, items(), pairs=[(BUYER, VICTIM)], policy_fields=VACANT, level="neutral", grade="lower")
        cls.base = copy.deepcopy(base)
        sizing = cls.base["position_sizing"]
        sizing["positions_by_breadth"] = {key: 4 for key in sizing["positions_by_breadth"]}
        sizing["slot_recycle"] = {"version": "v1"}
        for pair in sizing["agent_swap"]["nominated_pairs"]:
            pair["purpose"] = "exposure"
        sizing["agent_swap"]["exposure"] = {"version": "v2", "max_pairs_by_view": {"probe": 1, "start_rebuild": 2, "rebuild": 2}, "view": "keep_defensive"}
        sizing["exposure_view"] = {"version": "v2"}
        sizing["market_index_facts"] = dict(MARKET_INDEX_GATE)

    def run_offense(self, view="start_rebuild", max_size=None, stop="0.9", size="large"):
        policy = copy.deepcopy(self.base)
        sizing = policy["position_sizing"]
        sizing["agent_swap"]["entry_reserve"] = dict(RESERVE, max_entry_weight="0.2")
        for pair in sizing["agent_swap"]["nominated_pairs"]:
            pair["size"] = size
        offense = copy.deepcopy(OFFENSE)
        offense["offense_off_drawdown"] = "0.08"
        offense["by_view"]["probe"]["max_exposure_pairs"] = 1
        offense["by_view"]["start_rebuild"]["max_exposure_pairs"] = 2
        offense["by_view"]["rebuild"]["max_exposure_pairs"] = 2
        for row in offense["by_view"].values():
            row["beta_stop"] = stop
            if max_size:
                row["max_size"] = max_size
        sizing["offense"] = dict(offense, view=view, effective_view=view, suppressed_reason=None, cash_loosening=None)
        sizing["agent_swap"]["exposure"]["view"] = view
        policy["content_sha256"] = decision_policy_sha256(policy)
        return AllocationOrderEngine(self.bundle, policy).run(self.intent)

    @staticmethod
    def flags(body):
        return {(f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith(("OFFENSE", "AGENT_SWAP"))}

    def test_the_size_cap_and_the_reserved_lot_apply_to_a_starved_buyer_with_a_vacant_slot(self):
        body = self.run_offense(max_size="medium")
        flags = self.flags(body)
        self.assertNotIn((BUYER, "AGENT_SWAP_UNNEEDED"), flags)
        self.assertIn((BUYER, "OFFENSE_SIZE_CAPPED"), flags)  # large 被程式縮為 medium，沒有被繞過

    def test_a_starved_buyer_is_filled_without_a_cap_when_the_level_allows_large(self):
        body = self.run_offense(max_size="large")
        flags = self.flags(body)
        self.assertNotIn((BUYER, "AGENT_SWAP_UNNEEDED"), flags)
        self.assertIn((BUYER, "AGENT_SWAP_FILL"), flags)
        sides = {(o["symbol"], o["side"]) for o in body["order_proposal"]["orders"]}
        self.assertIn((BUYER, "buy"), sides)
        self.assertIn((VICTIM, "sell"), sides)
        self.assertNotIn((ADD, "buy"), sides)

    def test_the_beta_stop_still_stops_the_exposure_pair_when_the_portfolio_is_already_at_the_stop(self):
        body = self.run_offense(stop="0.05")
        self.assertIn((BUYER, "AGENT_SWAP_CANCELLED:offense_beta_stop"), self.flags(body))
        self.assertEqual(body["order_proposal"]["orders"], [])  # 取消的組：換出者不賣


if __name__ == "__main__":
    unittest.main()
