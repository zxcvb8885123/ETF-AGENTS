"""Agent 主導曝險階段 2、3（position_sizing.agent_swap.exposure）的純單元測試：docs/plans/agent_led_exposure_plan.md 第 3.2～3.4、6 節。

覆蓋：

1. 閘門（Policy 驗證）：形狀、值域、v1／v2 相依、前提（agent_swap、exposure_view）、人專用（策略反思不得改）、沒有閘門時
   ``AGENT_SWAP_FIELDS`` 以外逐位元不變。
2. 事實與簡報：曝險事實編號（取自 exposure_facts、附該股行情 evidence_id）、持股只能以曝險理由提名時的資格、需要提名的判定、
   沒有閘門時事實與簡報逐鍵不變。
3. Validator：``purpose`` 必填與值域、曝險事實編號只能出現在 purpose=exposure 的換出依據（缺它、引用別檔、放進 thesis 或換入依據一律拒絕）、
   第一輪不能採納它、背景識別碼不得出現在理由、高信心持股讓位的順序約束（``lower_conviction_review``）、program 封存的形狀。
4. 綁定：``apply_swap_nomination`` 綁 purpose、``bind_exposure_view`` 只綁 v2、Finalizer 重算比對、``exposure_swap_binding`` 對 v1／v2 與未綁定的處理。

合成世界沿用 test_swap_nomination 的三檔夾具；配置與端到端見 test_exposure_swap_allocation.py、test_exposure_swap_pipeline.py。
"""

import copy
import unittest

from etf_agent.automation.agent_schemas import SWAP_NOMINATION_EXPOSURE_SCHEMA, SWAP_NOMINATION_SCHEMA
from etf_agent.decision import DecisionToolError, MomentumEngine, seal_trade_decision, trade_decision_envelope
from etf_agent.decision import swap_nomination as sn
from etf_agent.decision.exposure_swap import (
    EXPOSURE_SWAP_V1, EXPOSURE_SWAP_V2, bind_exposure_view, exposure_basis_by_symbol, exposure_basis_id, exposure_swap_binding,
    exposure_swap_gate, is_exposure_basis_id, validate_exposure_swap_gate, validate_lower_conviction_review,
)
from etf_agent.decision.sizing import GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS
from etf_agent.decision.swap_nomination import (
    SwapNominationValidator, apply_swap_nomination, needs_nomination, nomination_facts, program_nomination, seal_swap_nomination,
    swap_nomination_brief, validate_team_swap_nomination,
)
from etf_agent.decision.trader import TradeDecisionValidator
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_agent_swap_policy import agent_swap_errors, errors_for, sizing_with
from test_gate_prerequisites import decision_item
from test_swap_nomination import World, first_round_items, good_output

CAPS = {"start_rebuild": 1, "rebuild": 2}
REASON = "這檔持股在反彈日幾乎沒有跟上，放掉它的代價是少一檔低波動持股；新股的多頭論點已採納。"


def exposure_facts(symbols=("2317.TW", "2330.TW")):
    """最小的 team_inputs.exposure_facts（只含提名會讀的欄位；完整結構與重算見 test_exposure_view.py）。"""
    return {
        "facts_id": "exposure-facts:abc123", "up_day": {"status": "available", "date": "2026-09-19", "pool_return": "0.020000"},
        "holdings": [
            {"symbol": symbol, "weight": "0.100000", "beta": "0.100000", "up_capture": "0.200000", "down_capture": "0.300000",
             "sector": "x", "up_day_return": "-0.010000"} for symbol in symbols
        ],
    }


class ExposureWorld(World):
    """三檔世界加 ``agent_swap.exposure``（預設 v1 影子）與 exposure_facts。"""

    def __init__(self, items=None, mutate_policy=None, version="v1", facts=None, **rules):
        def mutate(policy):
            gate = {"version": version}
            if version == EXPOSURE_SWAP_V2:
                gate["max_pairs_by_view"] = dict(CAPS)
            policy["position_sizing"]["agent_swap"]["exposure"] = gate
            if mutate_policy:
                mutate_policy(policy)

        super().__init__(items, mutate, **rules)
        self.exposure_facts = exposure_facts() if facts is None else facts

    def facts(self):
        return nomination_facts(*self.inputs(), self.exposure_facts)

    def validator(self, prior_context=None):
        return SwapNominationValidator(*self.inputs(), prior_context=prior_context, exposure_facts=self.exposure_facts)

    def brief(self, verdicts=None):
        return swap_nomination_brief(*self.inputs(), verdicts=verdicts, exposure_facts=self.exposure_facts)

    def validate(self, raw, prior_context=None):
        return self.validator(prior_context).validate(seal_swap_nomination(self.envelope, raw, True))


def exposure_pair(sell="2317.TW", **changes):
    pair = {
        "buy_symbol": "1101.TW", "sell_symbol": sell, "buy_basis_ids": ["bull-1101-1"],
        "sell_basis_ids": [exposure_basis_id(sell)], "reason": REASON, "purpose": "exposure",
    }
    pair.update(changes)
    return pair


def thesis_pair(**changes):
    pair = dict(good_output()["nominations"][0], purpose="thesis")
    pair.update(changes)
    return pair


def output(pairs=None, review=(), **changes):
    value = {"nominations": [exposure_pair()] if pairs is None else list(pairs), "lower_conviction_review": list(review),
             "thesis_hint_responses": []}
    value.update(changes)
    return value


def high_items(symbol="2317.TW"):
    """2317 第一輪 hold／high（只採納多頭，沒有空頭依據）；2330 hold／medium。"""
    return first_round_items(**{symbol: decision_item(symbol, "hold", "high", adopt=["bull-%s-1" % symbol[:4]])})


# ---------------------------------------------------------------- 1. 閘門

class GateTests(unittest.TestCase):
    def sizing(self, version="v2", agent_version="v2", **changes):
        sizing = sizing_with(agent_version)
        sizing["exposure_view"] = {"version": "v1"}
        sizing["cash_stance_turn_facts"] = {"version": "v1"}
        sizing["market_index_facts"] = {"version": "v1"}
        gate = {"version": version}
        if version == EXPOSURE_SWAP_V2:
            gate["max_pairs_by_view"] = dict(CAPS)
        gate.update(changes)
        sizing["agent_swap"]["exposure"] = gate
        return sizing

    def problems(self, sizing):
        errors: list = []
        validate_exposure_swap_gate(sizing, errors)
        return errors

    def test_valid_shapes(self):
        self.assertEqual(self.problems(self.sizing("v1", "v1")), [])
        self.assertEqual(self.problems(self.sizing("v1", "v2")), [])  # 影子可搭 agent_swap v2（配置略過 exposure 配對）
        self.assertEqual(self.problems(self.sizing("v2", "v2")), [])
        self.assertEqual(self.problems(self.sizing("v2", "v2", view="start_rebuild")), [])

    def test_v1_takes_only_a_version(self):
        for field, value in (("max_pairs_by_view", dict(CAPS)), ("view", "rebuild")):
            self.assertTrue(any("只用於 version=v2" in e for e in self.problems(self.sizing("v1", "v1", **{field: value}))), field)

    def test_v2_needs_agent_swap_v2_and_valid_caps(self):
        self.assertTrue(any("agent_swap.version=v2" in e for e in self.problems(self.sizing("v2", "v1"))))
        for label, caps in {
            "缺格": {"start_rebuild": 1}, "多格": dict(CAPS, keep_defensive=0), "高於現行上限": {"start_rebuild": 1, "rebuild": 3},
            "負數": {"start_rebuild": -1, "rebuild": 1}, "布林": {"start_rebuild": True, "rebuild": 2}, "字串": {"start_rebuild": "1", "rebuild": 2},
            "start 高於 rebuild": {"start_rebuild": 2, "rebuild": 1},
        }.items():
            self.assertTrue(self.problems(self.sizing("v2", "v2", max_pairs_by_view=caps)), label)
        self.assertEqual(self.problems(self.sizing("v2", "v2", max_pairs_by_view={"start_rebuild": 0, "rebuild": 0})), [])  # 0 合法：等於不取用
        self.assertTrue(self.problems(self.sizing("v2", "v2", view="all_in")))
        missing = self.sizing("v2", "v2")
        del missing["agent_swap"]["exposure"]["max_pairs_by_view"]
        self.assertTrue(self.problems(missing))

    def test_unknown_version_unknown_field_and_wrong_shape(self):
        for bad in ({"version": "v3"}, {"version": None}, {"version": "v1", "extra": 1}):
            sizing = self.sizing("v1", "v1")
            sizing["agent_swap"]["exposure"] = bad
            self.assertTrue(self.problems(sizing), bad)
        sizing = self.sizing("v1", "v1")
        sizing["agent_swap"]["exposure"] = "v1"
        self.assertTrue(self.problems(sizing))

    def test_prerequisites_and_policy_level_validation(self):
        sizing = self.sizing("v1", "v1")
        self.assertEqual([e for e in errors_for(sizing) if "exposure" in e and "agent_swap" in e], [])
        without_view = copy.deepcopy(sizing)
        del without_view["exposure_view"]
        self.assertTrue(any("agent_swap.exposure 必須與 exposure_view 同時存在" in e for e in errors_for(without_view)), errors_for(without_view))
        v2 = self.sizing("v2", "v2")
        self.assertEqual([e for e in agent_swap_errors(v2) if "exposure" in e], [])
        v2["agent_swap"]["exposure"]["max_pairs_by_view"]["rebuild"] = 3
        self.assertTrue(any("max_pairs_by_view" in e for e in errors_for(v2)))

    def test_declared_human_only_and_forbidden_to_reflection(self):
        self.assertIn("agent_swap.exposure", GATE_FIELDS)
        self.assertNotIn("agent_swap.exposure", POSITION_SIZING_FIELDS)  # 巢狀欄位：頂層欄位集合不變
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "agent_swap.exposure")
        self.assertTrue(rule.human_only)
        self.assertEqual(set(rule.requires), {"agent_swap", "exposure_view"})
        self.assertIn("agent_swap", FORBIDDEN_PREFIXES)

    def test_binding_shape_requires_purpose_only_with_the_gate(self):
        sizing = self.sizing("v2", "v2")
        swap = sizing["agent_swap"]
        ref = {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"}
        swap.update(nomination_ref=ref, nominated_pairs=[{"buy_symbol": "1101.TW", "sell_symbol": "2317.TW", "purpose": "exposure"}])
        self.assertEqual([e for e in agent_swap_errors(sizing) if "nominated_pairs" in e], [])
        for bad in ({"buy_symbol": "1101.TW", "sell_symbol": "2317.TW"},
                    {"buy_symbol": "1101.TW", "sell_symbol": "2317.TW", "purpose": "other"}):
            swap["nominated_pairs"] = [bad]
            self.assertTrue(any("nominated_pairs" in e for e in agent_swap_errors(sizing)), bad)
        plain = sizing_with("v2")
        plain["agent_swap"].update(nomination_ref=ref, nominated_pairs=[{"buy_symbol": "1101.TW", "sell_symbol": "2317.TW", "purpose": "thesis"}])
        self.assertTrue(any("nominated_pairs" in e for e in agent_swap_errors(plain)))  # 沒有閘門時 purpose 不是合法欄位


class BindingHelperTests(unittest.TestCase):
    def sizing(self, version="v2", **gate):
        base = {"version": version}
        if version == EXPOSURE_SWAP_V2:
            base.update(max_pairs_by_view=dict(CAPS))
        base.update(gate)
        return {"agent_swap": {
            "version": "v2", "max_swaps_per_run": 2, "min_holding_days": 5, "exposure": base,
            "nominated_pairs": [
                {"buy_symbol": "1101.TW", "sell_symbol": "2317.TW", "purpose": "exposure"},
                {"buy_symbol": "2412.TW", "sell_symbol": "2330.TW", "purpose": "thesis"},
            ],
            "nomination_ref": {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"},
        }}

    def test_gate_lookup(self):
        self.assertIsNone(exposure_swap_gate(None))
        self.assertIsNone(exposure_swap_gate({}))
        self.assertIsNone(exposure_swap_gate({"agent_swap": {"version": "v1"}}))
        self.assertEqual(exposure_swap_gate(self.sizing("v1")), {"version": "v1"})

    def test_binding_reads_nothing_without_the_gate_or_for_the_shadow_agent_swap(self):
        self.assertIsNone(exposure_swap_binding({}))
        sizing = self.sizing("v1")
        sizing["agent_swap"]["version"] = "v1"
        self.assertIsNone(exposure_swap_binding(sizing))

    def test_v1_binding_has_no_view_or_caps_and_v2_needs_a_bound_view(self):
        shadow = exposure_swap_binding(self.sizing("v1"))
        self.assertEqual((shadow["version"], shadow["view"], shadow["caps"], shadow["purposes"]), ("v1", None, None, ["exposure", "thesis"]))
        with self.assertRaises(DecisionToolError):
            exposure_swap_binding(self.sizing("v2"))  # 沒綁 view：不靜默當作 keep_defensive
        bound = exposure_swap_binding(self.sizing("v2", view="start_rebuild"))
        self.assertEqual((bound["version"], bound["view"], bound["caps"]), ("v2", "start_rebuild", CAPS))

    def test_unbound_pairs_or_missing_purpose_are_rejected(self):
        sizing = self.sizing("v1")
        del sizing["agent_swap"]["nominated_pairs"]
        with self.assertRaises(DecisionToolError):
            exposure_swap_binding(sizing)
        sizing = self.sizing("v1")
        del sizing["agent_swap"]["nominated_pairs"][0]["purpose"]
        with self.assertRaises(DecisionToolError):
            exposure_swap_binding(sizing)

    def test_bind_exposure_view_only_for_v2(self):
        stance = {"exposure_view": "rebuild"}
        bound = bind_exposure_view(self.sizing("v2"), stance)
        self.assertEqual(bound["exposure"]["view"], "rebuild")
        self.assertNotIn("view", self.sizing("v2")["agent_swap"]["exposure"])  # 不改原物件
        self.assertNotIn("view", bind_exposure_view(self.sizing("v1"), stance)["exposure"])  # 影子不綁
        with self.assertRaises(DecisionToolError):
            bind_exposure_view(self.sizing("v2"), {"exposure_view": "all_in"})
        with self.assertRaises(DecisionToolError):
            bind_exposure_view(self.sizing("v2"), {})


# ---------------------------------------------------------------- 2. 事實與簡報

class FactsAndBriefTests(unittest.TestCase):
    def test_basis_is_derived_from_exposure_facts_with_the_stocks_price_evidence(self):
        world = ExposureWorld()
        basis = exposure_basis_by_symbol(world.bundle, world.exposure_facts)
        prices = {row["symbol"]: row["source_evidence_id"] for row in world.bundle["snapshot"]["latest_prices"]}
        self.assertEqual(sorted(basis), ["2317.TW", "2330.TW"])
        row = basis["2317.TW"]
        self.assertEqual((row["id"], row["kind"], row["evidence_ids"]), ("review:2317.TW:exposure", "exposure", [prices["2317.TW"]]))
        self.assertEqual(row["facts"]["pool_up_day_return"], "0.020000")
        self.assertEqual(row["facts"]["up_day_return"], "-0.010000")
        self.assertTrue(is_exposure_basis_id(row["id"]))
        self.assertFalse(is_exposure_basis_id("review:2317.TW:position"))
        self.assertFalse(is_exposure_basis_id(5))

    def test_unavailable_up_day_and_missing_price_evidence_do_not_fill_values(self):
        world = ExposureWorld()
        gap = copy.deepcopy(world.exposure_facts)
        gap["up_day"] = {"status": "unavailable"}
        facts = exposure_basis_by_symbol(world.bundle, gap)["2317.TW"]["facts"]
        self.assertEqual((facts["up_day_date"], facts["pool_up_day_return"], facts["up_day_return"]), (None, None, None))
        bundle = copy.deepcopy(world.bundle)
        bundle["snapshot"]["latest_prices"] = [r for r in bundle["snapshot"]["latest_prices"] if r["symbol"] != "2317.TW"]
        self.assertNotIn("2317.TW", exposure_basis_by_symbol(bundle, world.exposure_facts))

    def test_holdings_without_a_bear_basis_can_still_be_nominated_for_exposure(self):
        facts = ExposureWorld().facts()
        victims = {row["symbol"]: row for row in facts["victims"]}
        self.assertEqual((victims["2317.TW"]["nominable"], victims["2317.TW"]["reasons"]), (False, ["no_bear_basis"]))
        self.assertEqual((victims["2317.TW"]["exposure_nominable"], victims["2317.TW"]["exposure_reasons"]), (True, []))
        self.assertEqual(victims["2317.TW"]["exposure_basis_id"], "review:2317.TW:exposure")
        self.assertEqual(facts["exposure_nominable_sells"], ["2317.TW", "2330.TW"])
        self.assertEqual(facts["lower_conviction_symbols"], ["2317.TW", "2330.TW"])  # low／medium
        self.assertEqual(facts["high_conviction_exposure_symbols"], [])
        high = ExposureWorld(high_items()).facts()
        self.assertEqual(high["high_conviction_exposure_symbols"], ["2317.TW"])
        self.assertEqual(high["lower_conviction_symbols"], ["2330.TW"])

    def test_exposure_reasons(self):
        young = ExposureWorld(mutate_policy=lambda p: p["position_sizing"]["agent_swap"].update(min_holding_days=30))
        self.assertEqual({r["symbol"]: r["exposure_reasons"] for r in young.facts()["victims"]}["2330.TW"], ["holding_days_short"])
        no_facts = ExposureWorld(facts=exposure_facts(("2330.TW",)))
        self.assertEqual({r["symbol"]: r["exposure_reasons"] for r in no_facts.facts()["victims"]}["2317.TW"], ["no_exposure_facts"])

        def untradable(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "2330.TW", "state": "restricted"}]}

        blocked = ExposureWorld(mutate_bundle=untradable)
        self.assertEqual({r["symbol"]: r["exposure_reasons"] for r in blocked.facts()["victims"]}["2330.TW"], ["not_tradable"])

    def test_the_gate_needs_exposure_facts(self):
        world = ExposureWorld()
        with self.assertRaises(DecisionToolError):
            nomination_facts(*world.inputs())
        with self.assertRaises(DecisionToolError):
            nomination_facts(*world.inputs(), exposure_facts=None)

    def test_an_exposure_only_victim_still_makes_the_nomination_needed(self):
        items = first_round_items(sell_adopted=("bull-2330-1",), sell_rejected=("bear-2330-1",))  # 沒有任何持股有空頭依據
        self.assertFalse(needs_nomination(World(items).facts()))
        self.assertTrue(needs_nomination(ExposureWorld(items).facts()))

    def test_without_the_gate_facts_and_brief_are_unchanged_even_if_facts_are_passed(self):
        plain = World()
        facts = plain.facts()
        self.assertEqual(facts, nomination_facts(*plain.inputs(), exposure_facts()))
        for key in ("exposure_nominable_sells", "lower_conviction_symbols", "high_conviction_exposure_symbols"):
            self.assertNotIn(key, facts)
        for row in facts["victims"]:
            self.assertNotIn("exposure_nominable", row)
        brief = plain.brief()
        self.assertEqual(brief, swap_nomination_brief(*plain.inputs(), exposure_facts=exposure_facts()))
        self.assertNotIn("exposure_swap", brief)
        for row in brief["holdings"]:
            self.assertFalse({"exposure_nominable", "exposure_basis", "exposure_blocked_reasons"} & set(row))

    def test_the_gated_brief_offers_the_citable_basis_and_the_ordering_rules(self):
        world = ExposureWorld(high_items())
        brief = world.brief()
        holdings = {row["symbol"]: row for row in brief["holdings"]}
        self.assertEqual(holdings["2317.TW"]["exposure_basis"]["id"], "review:2317.TW:exposure")
        self.assertTrue(holdings["2317.TW"]["exposure_nominable"])
        block = brief["exposure_swap"]
        self.assertEqual((block["lower_conviction_symbols"], block["high_conviction_symbols"]), (["2330.TW"], ["2317.TW"]))
        self.assertIn("purpose=exposure", block["note"])
        # v1（影子）與 v2 的簡報完全相同：Agent 不會被告知目前是影子期。
        self.assertEqual(brief, ExposureWorld(high_items(), version="v2").brief())
        self.assertNotIn("影子", str(brief))


# ---------------------------------------------------------------- 3. Validator

class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.world = ExposureWorld()

    def rejects(self, raw, text, world=None, **kwargs):
        errors = (world or self.world).validate(raw, **kwargs)
        self.assertTrue(any(text in error for error in errors), (text, errors))

    def test_good_exposure_and_thesis_pairs_are_accepted(self):
        self.assertEqual(self.world.validate(output()), [])
        self.assertEqual(self.world.validate(output([thesis_pair(sell_symbol="2330.TW")])), [])
        # 沒有空頭依據的 2317 以曝險理由讓位（thesis 資格不足）也合格；2330 同時有空頭依據與曝險編號也合格。
        both = exposure_pair("2330.TW", sell_basis_ids=["bear-2330-1", "review:2330.TW:exposure"])
        self.assertEqual(self.world.validate(output([both])), [])

    def test_purpose_is_required_and_in_the_enum(self):
        pair = exposure_pair()
        del pair["purpose"]
        self.rejects(output([pair]), "purpose 必須是 thesis／exposure")
        self.rejects(output([exposure_pair(purpose="rebuild")]), "purpose 必須是 thesis／exposure")
        self.rejects(output([exposure_pair(purpose=None)]), "purpose 必須是 thesis／exposure")

    def test_purpose_and_review_are_rejected_without_the_gate(self):
        plain = World()
        sealed = seal_swap_nomination(plain.envelope, output())
        errors = plain.validator().validate(sealed)
        self.assertTrue(any("x_lower_conviction_review" in e for e in errors), errors)
        plain_pair = dict(good_output()["nominations"][0], purpose="thesis")
        errors = plain.validate(good_output(nominations=[plain_pair]))
        self.assertTrue(any("purpose" in e and "含未允許欄位" in e for e in errors), errors)

    def test_the_exposure_basis_is_required_and_must_be_the_sold_stocks_own(self):
        self.rejects(output([exposure_pair(sell_basis_ids=["bear-2330-1"])], ), "必須含")  # 2317 沒有曝險編號（也不是它的空頭 claim）
        self.rejects(output([exposure_pair(sell_basis_ids=["review:2330.TW:exposure"])]), "引用別檔的曝險事實編號")
        self.rejects(output([exposure_pair(sell_basis_ids=["review:2317.TW:exposure", "review:2330.TW:exposure"])]), "引用別檔的曝險事實編號")
        self.rejects(output([exposure_pair(sell_basis_ids=["review:2317.TW:exposure", "review:2317.TW:exposure"])]), "不得重複")
        self.rejects(output([exposure_pair(sell_basis_ids=[])]), "必須含 2317.TW 自己的曝險事實編號")

    def test_other_basis_ids_on_an_exposure_pair_still_follow_the_first_round_rules(self):
        self.rejects(output([exposure_pair("2330.TW", sell_basis_ids=["review:2330.TW:exposure", "bull-2330-1"])]), "第一輪已否決")
        self.rejects(output([exposure_pair("2330.TW", sell_basis_ids=["review:2330.TW:exposure", "bear-2330-9"])]), "不存在")
        self.rejects(output([exposure_pair("2330.TW", sell_basis_ids=["review:2330.TW:exposure", "bull-1101-1"])]), "屬於其他股票")

    def test_the_exposure_basis_is_only_for_exposure_purpose_sell_basis(self):
        self.rejects(output([thesis_pair(sell_basis_ids=["bear-2330-1", "review:2330.TW:exposure"])]), "只能用於 purpose=exposure")
        self.rejects(output([exposure_pair(buy_basis_ids=["bull-1101-1", "review:2317.TW:exposure"])]), "buy_basis_ids 不得放曝險事實編號")
        thesis_for_2317 = thesis_pair(sell_symbol="2317.TW", sell_basis_ids=["review:2317.TW:exposure"])
        errors = self.world.validate(output([thesis_for_2317]))
        self.assertTrue(any("只能用於 purpose=exposure" in e for e in errors))
        self.assertTrue(any("no_bear_basis" in e for e in errors), errors)  # thesis 的資格不變：2317 仍沒有空頭依據

    def test_the_first_round_cannot_adopt_the_exposure_basis(self):
        items = first_round_items(**{"2330.TW": decision_item(
            "2330.TW", "hold", "medium", adopt=["bear-2330-1", "review:2330.TW:position", "review:2330.TW:exposure"], reject=["bull-2330-1"])})
        world = ExposureWorld(items)
        victims = {row["symbol"]: row for row in world.facts()["victims"]}
        self.assertNotIn("review:2330.TW:exposure", victims["2330.TW"]["basis_ids"])
        validator = TradeDecisionValidator(
            world.bundle, MomentumEngine(world.bundle).run(), world.debate, position_review=world.review, hold_conviction=True)
        trade = seal_trade_decision(trade_decision_envelope(world.bundle, world.debate, "trade-x"), items)
        errors = validator.validate(trade)
        self.assertTrue(any("引用不屬於該股票的 claim" in e and "review:2330.TW:exposure" in e for e in errors), errors)

    def test_exposure_victim_rules(self):
        self.rejects(output([exposure_pair("1101.TW", buy_symbol="2317.TW", sell_basis_ids=["review:1101.TW:exposure"])]), "不能作為換出股")
        young = ExposureWorld(mutate_policy=lambda p: p["position_sizing"]["agent_swap"].update(min_holding_days=30))
        self.rejects(output(), "holding_days_short", world=young)
        self.rejects(output(), "no_exposure_facts", world=ExposureWorld(facts=exposure_facts(("2330.TW",))))

    def test_background_identifiers_are_rejected_in_reasons(self):
        for token in ("exposure_facts", "exposure-facts:abc123"):
            self.rejects(output([exposure_pair(reason="依 %s 判斷" % token)]), "不得引用前次決策背景")
        prior = {"context_id": "ctx-prev-9", "source": {"run_id": "run-prev-9", "decision_id": "decision:abc", "intent_id": "intent-prev-9"}}
        self.rejects(output([exposure_pair(reason="承接 run-prev-9")]), "不得引用前次決策背景", prior_context=prior)
        self.rejects(output([], no_nomination_reason="因為 exposure_facts"), "不得引用前次決策背景")

    def test_empty_nominations_still_need_a_reason_and_an_empty_review(self):
        self.assertEqual(self.world.validate(output([], no_nomination_reason="沒有合適的配對。")), [])
        self.rejects(output([]), "no_nomination_reason 必須是非空字串")

    def test_review_shape_and_missing_review(self):
        for raw in ({"nominations": [exposure_pair()], "thesis_hint_responses": []},):
            self.rejects(raw, "lower_conviction_review 必須是陣列")
        self.rejects(output(lower_conviction_review="x"), "lower_conviction_review 必須是陣列")

    def test_program_seals_carry_an_empty_review_only_with_the_gate(self):
        world = ExposureWorld()
        for status in ("exhausted", "unavailable"):
            sealed = program_nomination(world.envelope, status, ["x"], exposure=True)
            self.assertEqual(sealed["lower_conviction_review"], [])
            self.assertEqual(world.validator().validate(sealed), [], status)
            plain = program_nomination(world.envelope, status, ["x"])
            self.assertNotIn("lower_conviction_review", plain)
            self.assertTrue(any("lower_conviction_review 必須是空陣列" in e for e in world.validator().validate(plain)))
            tampered = copy.deepcopy(sealed)
            tampered["lower_conviction_review"] = [{"symbol": "2330.TW", "response": "kept", "reason": "x"}]
            tampered["content_sha256"] = sn.artifact_content_sha256(tampered)
            self.assertTrue(any("必須是空陣列" in e for e in world.validator().validate(tampered)))
        self.assertNotIn("lower_conviction_review", program_nomination(world.envelope, "unavailable", ["x"]))

    def test_the_agent_schema_requires_every_declared_property_and_only_adds_the_two_fields(self):
        schema = SWAP_NOMINATION_EXPOSURE_SCHEMA
        self.assertEqual(set(schema["properties"]), set(schema["required"]))
        self.assertEqual(set(schema["properties"]) - set(SWAP_NOMINATION_SCHEMA["properties"]), {"lower_conviction_review"})
        for node in (schema["properties"]["nominations"]["items"], schema["properties"]["lower_conviction_review"]["items"]):
            self.assertEqual(set(node["properties"]), set(node["required"]))
        self.assertEqual(
            set(schema["properties"]["nominations"]["items"]["properties"]) - set(SWAP_NOMINATION_SCHEMA["properties"]["nominations"]["items"]["properties"]),
            {"purpose"})
        self.assertNotIn("lower_conviction_review", SWAP_NOMINATION_SCHEMA["properties"])  # 沒有閘門的 Schema 逐位元不變


class HighConvictionOrderTests(unittest.TestCase):
    """提名高信心持股（2317 hold／high）前必須逐檔交代中、低信心持股（這個世界只有 2330 hold／medium）。"""

    def setUp(self):
        self.world = ExposureWorld(high_items())

    def rejects(self, raw, text, world=None):
        errors = (world or self.world).validate(raw)
        self.assertTrue(any(text in error for error in errors), (text, errors))

    def kept(self, **changes):
        return dict({"symbol": "2330.TW", "response": "kept", "reason": "它在反彈日有跟上，不適合讓位。"}, **changes)

    def test_a_complete_review_makes_the_high_conviction_pair_acceptable(self):
        self.assertEqual(self.world.validate(output([exposure_pair("2317.TW")], [self.kept()])), [])

    def test_missing_or_incomplete_review_is_rejected(self):
        self.rejects(output([exposure_pair("2317.TW")], []), "漏交代可提名的 medium／low 持股：2330.TW")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(), self.kept()]), "每檔只能交代一次")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(), self.kept(symbol="2317.TW")]), "含不需交代的股票：2317.TW")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(), self.kept(symbol="9999.TW")]), "含不需交代的股票：9999.TW")

    def test_entry_fields_and_responses(self):
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(reason="")]), "標 kept 必須附一句理由")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(reason="  ")]), "標 kept 必須附一句理由")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(response="swap")]), "response 必須是 nominated／kept")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(extra=1)]), "含未允許欄位")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(symbol="")]), "symbol 必須是非空字串")
        self.rejects(output([exposure_pair("2317.TW")], ["x"]), "必須是物件")
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(response="nominated", reason=5)]), "reason 必須是字串")

    def test_nominated_must_actually_be_an_exposure_sell_and_kept_must_not_be_sold(self):
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(response="nominated")]), "必須是某一對 purpose=exposure 的 sell_symbol")
        # 2330 以 thesis 配對讓位時不算「已以曝險理由讓位」，標 nominated 不成立；標 kept 又與它出現在 sell_symbol 矛盾。
        thesis = thesis_pair(sell_symbol="2330.TW", buy_symbol="2412.TW", buy_basis_ids=["bull-1101-1"])
        for response in ("nominated", "kept"):
            errors = self.world.validate(output([exposure_pair("2317.TW"), thesis], [self.kept(response=response)]))
            self.assertTrue(errors, response)

    def test_the_review_must_be_empty_without_a_high_conviction_exposure_pair(self):
        world = ExposureWorld()  # 沒有高信心持股
        self.rejects(output([exposure_pair("2317.TW")], [self.kept()]), "必須是空陣列", world=world)
        self.rejects(output([thesis_pair(sell_symbol="2330.TW")], [self.kept()]), "必須是空陣列", world=world)
        # 高信心持股只以 thesis 讓位時（有空頭依據）也不需要交代。
        items = first_round_items(**{"2330.TW": decision_item(
            "2330.TW", "hold", "high", adopt=["bear-2330-1", "review:2330.TW:position"], reject=["bull-2330-1"])})
        thesis_world = ExposureWorld(items)
        self.assertEqual(thesis_world.validate(output([thesis_pair()], [])), [])

    def test_kept_reasons_cannot_cite_background_identifiers(self):
        self.rejects(output([exposure_pair("2317.TW")], [self.kept(reason="依 exposure_facts 判斷")]), "不得引用前次決策背景")

    def test_pair_order_pure_function(self):
        def run(pairs, review, lower=("L1",), high=("H1",)):
            errors, texts = [], []
            sold = {sell for _, sell, _ in pairs}
            validate_lower_conviction_review(review, pairs, lower, high, sold, texts, errors)
            return errors

        nominated = [{"symbol": "L1", "response": "nominated", "reason": "已讓位"}]
        low_first = [("B1", "L1", "exposure"), ("B2", "H1", "exposure")]
        self.assertEqual(run(low_first, nominated), [])
        high_first = list(reversed(low_first))
        self.assertTrue(any("排在所有 medium／low" in e for e in run(high_first, nominated)))
        # thesis 配對不受順序約束，排在高信心 exposure 配對之前或之後都可以。
        mixed = [("B3", "T1", "thesis"), ("B2", "H1", "exposure"), ("B4", "T2", "thesis")]
        self.assertEqual(run(mixed, [{"symbol": "L1", "response": "kept", "reason": "不讓位"}]), [])
        # 只有高信心配對、沒有中低信心配對：照常需要逐檔交代。
        self.assertTrue(any("漏交代" in e for e in run([("B2", "H1", "exposure")], [])))
        self.assertEqual(run([("B2", "H1", "exposure")], [], lower=()), [])

    def test_two_high_pairs_after_two_lower_pairs(self):
        pairs = [("B1", "L1", "exposure"), ("B2", "L2", "exposure"), ("B3", "H1", "exposure"), ("B4", "H2", "exposure")]
        review = [{"symbol": "L1", "response": "nominated", "reason": "a"}, {"symbol": "L2", "response": "nominated", "reason": "b"}]
        errors: list = []
        validate_lower_conviction_review(review, pairs, ["L1", "L2"], ["H1", "H2"], {p[1] for p in pairs}, [], errors)
        self.assertEqual(errors, [])
        shuffled = [pairs[0], pairs[2], pairs[1], pairs[3]]
        errors = []
        validate_lower_conviction_review(review, shuffled, ["L1", "L2"], ["H1", "H2"], {p[1] for p in pairs}, [], errors)
        self.assertTrue(any("排在所有 medium／low" in e for e in errors))


# ---------------------------------------------------------------- 4. 綁定與 Finalizer

class BindingAndFinalizerTests(unittest.TestCase):
    def setUp(self):
        self.world = ExposureWorld(version="v2")
        self.nomination = seal_swap_nomination(self.world.envelope, output(), True)

    def v2_policy(self):
        policy = copy.deepcopy(self.world.policy)
        policy["position_sizing"]["agent_swap"]["version"] = "v2"
        return policy

    def team(self, **changes):
        world = self.world
        team = {"swap_nomination": self.nomination, "selection_ranking": world.ranking, "position_review": world.review,
                "exposure_review": world.exposure, "exposure_facts": world.exposure_facts}
        team.update(changes)
        return team

    def check(self, policy, team=None):
        errors: list = []
        validate_team_swap_nomination(self.world.bundle, policy, self.world.trade, self.world.debate, team or self.team(), errors)
        return errors

    def test_apply_binds_purpose_and_the_finalizer_accepts_it(self):
        bound = apply_swap_nomination(self.v2_policy(), self.nomination)
        self.assertEqual(
            bound["position_sizing"]["agent_swap"]["nominated_pairs"],
            [{"buy_symbol": "1101.TW", "sell_symbol": "2317.TW", "purpose": "exposure"}])
        self.assertEqual(self.check(bound), [])

    def test_a_wrong_or_missing_purpose_in_the_policy_is_rejected(self):
        bound = apply_swap_nomination(self.v2_policy(), self.nomination)
        wrong = copy.deepcopy(bound)
        wrong["position_sizing"]["agent_swap"]["nominated_pairs"][0]["purpose"] = "thesis"
        self.assertTrue(any("nominated_pairs" in e for e in self.check(wrong)))
        missing = copy.deepcopy(bound)
        del missing["position_sizing"]["agent_swap"]["nominated_pairs"][0]["purpose"]
        self.assertTrue(any("nominated_pairs" in e for e in self.check(missing)))

    def test_the_finalizer_needs_the_exposure_facts_for_a_gated_nomination(self):
        bound = apply_swap_nomination(self.v2_policy(), self.nomination)
        team = self.team()
        del team["exposure_facts"]
        self.assertTrue(any("exposure_facts" in e for e in self.check(bound, team)), self.check(bound, team))
        forged = self.team(exposure_facts=exposure_facts(("2330.TW",)))  # 缺 2317 的曝險事實：2317 不能以曝險理由讓位
        self.assertTrue(any("no_exposure_facts" in e for e in self.check(bound, forged)))

    def test_without_the_gate_nothing_changes(self):
        plain = World()
        nomination = seal_swap_nomination(plain.envelope, good_output())
        policy = copy.deepcopy(plain.policy)
        policy["position_sizing"]["agent_swap"]["version"] = "v2"
        bound = apply_swap_nomination(policy, nomination)
        self.assertEqual(bound["position_sizing"]["agent_swap"]["nominated_pairs"], [{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW"}])
        errors: list = []
        validate_team_swap_nomination(
            plain.bundle, bound, plain.trade, plain.debate,
            {"swap_nomination": nomination, "selection_ranking": plain.ranking, "position_review": plain.review,
             "exposure_review": plain.exposure}, errors)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
