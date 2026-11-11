"""優質股第三階段（position_sizing.agent_swap.quality_dip）：docs/quality_dip_plan.md 第 4.2、10.3、10.4 節。

第三階段改走換股提名：``purpose`` 與曝險階段 2 合用同一個列舉，多一個值 ``quality_dip``，不另開程式配對路徑。覆蓋：

1. 閘門（Policy 驗證）：形狀、值域、全部數字必填、v1／v2 相依、前提（agent_swap、agent_swap.exposure、quality_dip_facts、
   quality_dip_assessment、cash_stance_turn_facts）、只由人設定（策略反思禁區）、沒有閘門時欄位集合與 purpose 允許值不變。
2. 5c 事實、簡報與 Validator：候選標示與 dip_assessment 相符、無新壞事、下跌、非價格多頭 claim、換出者 hold／low 與持有天數、
   超跌候選預設排除（另開 oversold 區塊才可）、缺資料既不通過也不否決（同一檔仍可用 thesis 提名）、背景識別碼不得寫進理由；
   沒有閘門時事實與簡報逐鍵不變、``needs_nomination`` 判定不變。
3. 配置（v2）：成交時買進權重不超過 ``max_entry_weight``、買進金額不超過換出賣款；turn_assessment 不足、回撤下限 defensive 或帳本歷史
   不可用、賣款不可再用時整批取消；對數上限從尾端取消；減碼日略過；換手與現金下限不放寬；任何取消都不會只賣不買；
   不改寫交易 Agent 意圖；v1（影子）配置逐位元等於沒有這些配對；v2 未綁 turn_assessment 一律拒絕。
4. 正式樣板回歸：``config/decision_policy.json`` 原樣讀入時沒有本閘門，提名 Schema 與 purpose 允許值不變。
"""

import copy
import json
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.automation.agent_schemas import (
    SWAP_NOMINATION_EXPOSURE_SCHEMA, SWAP_NOMINATION_QUALITY_DIP_SCHEMA, SWAP_NOMINATION_SCHEMA,
)
from etf_agent.decision import AllocationOrderEngine, DecisionToolError, ProposalValidator, decision_policy_sha256
from etf_agent.decision.quality_dip_swap import (
    PURPOSE_QUALITY_DIP, bind_quality_dip_turn, nomination_purposes, quality_dip_swap_binding, quality_dip_swap_gate, turn_allows,
    validate_quality_dip_swap_gate,
)
from etf_agent.decision.sizing import GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS
from etf_agent.decision.swap_nomination import (
    SwapNominationValidator, needs_nomination, nomination_facts, seal_swap_nomination, swap_nomination_brief,
    validate_team_swap_nomination,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_agent_swap_allocation import PAIR, exposure_bundle, summary, v2_world
from test_agent_swap_policy import agent_swap_errors, errors_for, sizing_with
from test_exposure_rotation_e2e import codes, sold_and_bought
from test_exposure_swap import exposure_facts
from test_gate_prerequisites import decision_item
from test_market_index_facts import GATE as MARKET_INDEX_GATE
from test_risk_limit_gates import guard
from test_swap_nomination import World, first_round_items, good_output

ROOT = Path(__file__).resolve().parents[2]
GATE = {
    "version": "v1", "max_swaps_per_run": 1, "max_entry_weight": "0.03", "min_new_conviction": "medium",
    "min_holding_days": 5, "min_turn_assessment": "turn_unconfirmed",
}
OVERSOLD = {"max_swaps_per_run": 1, "max_entry_weight": "0.02"}
REASON = "跌幅幾乎都能由大盤解釋、查無新壞事，新股的營運論點已採納；換出股的空頭論點已採納。"


def dip_facts(candidate="market_dragged", pattern="market_explained", adverse=(), status="available", symbol="1101.TW"):
    """最小的 team_inputs.dip_facts（只含提名會讀的欄位；完整結構與重算見 test_quality_dip_facts.py）。"""
    return {
        "facts_id": "dip-facts:abc123", "data_status": status, "version": "v1",
        "items": [{
            "symbol": symbol, "price_pattern": pattern, "candidate": candidate,
            "cause": "unknown" if candidate == "oversold" else None,
            "fundamental_flags": {"new_adverse_facts": list(adverse), "concerns": [], "unknown": []},
        }],
    }


def quality_items(assessment="market_driven", victim_conviction="low", buy_conviction="medium", **overrides):
    buy = decision_item("1101.TW", "buy", buy_conviction, adopt=["bull-1101-1"])
    buy["dip_assessment"] = assessment
    victim = decision_item(
        "2330.TW", "hold", victim_conviction, adopt=["bear-2330-1", "review:2330.TW:position"], reject=["bull-2330-1"])
    return first_round_items(**dict({"1101.TW": buy, "2330.TW": victim}, **overrides))


class QualityWorld(World):
    """三檔世界加 ``agent_swap.exposure`` v1、``agent_swap.quality_dip`` 與 dip_facts；多頭 claim 的 claim_kind 可指定。"""

    def __init__(self, items=None, gate=None, facts=None, kinds=None, gate_on=True, **rules):
        def mutate(policy):
            policy["position_sizing"]["agent_swap"]["exposure"] = {"version": "v1"}
            if gate_on:
                policy["position_sizing"]["agent_swap"]["quality_dip"] = dict(GATE if gate is None else gate)

        super().__init__(items or quality_items(), mutate, **rules)
        self.exposure_facts = exposure_facts()
        self.dip_facts = dip_facts() if facts is None else facts
        for packet in self.debate["packets"]:
            for item in packet["items"]:
                for claim in item["claims"]:
                    kind = (kinds or {}).get(claim["claim_id"], "revenue" if claim["claim_id"] == "bull-1101-1" else "price")
                    if kind is not None:
                        claim["claim_kind"] = kind

    def facts(self):
        return nomination_facts(*self.inputs(), self.exposure_facts, self.dip_facts)

    def validator(self, prior_context=None):
        return SwapNominationValidator(
            *self.inputs(), prior_context=prior_context, exposure_facts=self.exposure_facts, dip_facts=self.dip_facts)

    def brief(self, verdicts=None):
        return swap_nomination_brief(*self.inputs(), verdicts=verdicts, exposure_facts=self.exposure_facts, dip_facts=self.dip_facts)

    def validate(self, raw, prior_context=None):
        return self.validator(prior_context).validate(seal_swap_nomination(self.envelope, raw, True))


def quality_pair(**changes):
    pair = dict(good_output()["nominations"][0], purpose=PURPOSE_QUALITY_DIP, reason=REASON)
    pair.update(changes)
    return pair


def output(pairs=None, **changes):
    value = {"nominations": [quality_pair()] if pairs is None else list(pairs), "lower_conviction_review": [], "thesis_hint_responses": []}
    value.update(changes)
    return value


def quality_sizing(version="v1", agent_version="v2", **changes):
    sizing = sizing_with(agent_version)
    sizing.update(
        exposure_view={"version": "v1"}, cash_stance_turn_facts={"version": "v1"}, market_index_facts={"version": "v1"},
        quality_dip_facts={"version": "v1"}, quality_dip_assessment={"version": "v1"},
    )
    sizing["agent_swap"]["exposure"] = {"version": "v1"}
    sizing["agent_swap"]["quality_dip"] = dict(GATE, version=version, **changes)
    return sizing


# ---------------------------------------------------------------- 1. 閘門

class GateTests(unittest.TestCase):
    def problems(self, sizing):
        errors: list = []
        validate_quality_dip_swap_gate(sizing, errors)
        return errors

    def test_valid_shapes(self):
        self.assertEqual(self.problems(quality_sizing("v1", "v1")), [])
        self.assertEqual(self.problems(quality_sizing("v1", "v2")), [])  # 影子可搭 agent_swap v2（配置略過 quality_dip 配對）
        self.assertEqual(self.problems(quality_sizing("v2", "v2")), [])
        self.assertEqual(self.problems(quality_sizing("v2", "v2", turn_assessment="turn_confirmed")), [])
        self.assertEqual(self.problems(quality_sizing("v2", "v2", oversold=dict(OVERSOLD))), [])
        self.assertEqual(self.problems(quality_sizing("v1", "v2", max_swaps_per_run=0)), [])  # 0 合法：等於不取用

    def test_every_number_is_required_and_has_no_default(self):
        for field in ("max_swaps_per_run", "max_entry_weight", "min_new_conviction", "min_holding_days", "min_turn_assessment", "version"):
            sizing = quality_sizing("v1")
            del sizing["agent_swap"]["quality_dip"][field]
            self.assertTrue(self.problems(sizing), field)

    def test_bad_values(self):
        for label, changes in {
            "高於 agent_swap 上限": {"max_swaps_per_run": 3}, "負數": {"max_swaps_per_run": -1}, "布林": {"max_swaps_per_run": True},
            "權重 0": {"max_entry_weight": "0"}, "權重 1": {"max_entry_weight": "1"}, "權重非字串": {"max_entry_weight": 0.03},
            "信心": {"min_new_conviction": "very_high"}, "持有天數": {"min_holding_days": 61},
            "沒有轉向跡象": {"min_turn_assessment": "no_turn_evidence"}, "未知欄位": {"extra": 1},
            "v1 不得綁 turn": {"turn_assessment": "turn_confirmed"},
        }.items():
            self.assertTrue(self.problems(quality_sizing("v1", "v2", **changes)), label)
        self.assertTrue(self.problems(quality_sizing("v2", "v2", turn_assessment="maybe")))
        self.assertTrue(self.problems(quality_sizing("v4", "v2")))  # v3 已是合法版本（見 test_quality_dip_sizing.py）
        self.assertTrue(any("agent_swap.version=v2" in e for e in self.problems(quality_sizing("v2", "v1"))))

    def test_oversold_block_cannot_be_looser_than_the_market_dragged_limits(self):
        for label, block in {
            "對數高於本閘門": dict(OVERSOLD, max_swaps_per_run=2), "權重高於本閘門": dict(OVERSOLD, max_entry_weight="0.05"),
            "缺欄位": {"max_swaps_per_run": 1}, "未知欄位": dict(OVERSOLD, extra=1),
        }.items():
            self.assertTrue(self.problems(quality_sizing("v1", "v2", oversold=block)), label)
        sizing = quality_sizing("v1", "v2")
        sizing["agent_swap"]["quality_dip"]["oversold"] = "yes"
        self.assertTrue(self.problems(sizing))

    def test_prerequisites_and_policy_level_validation(self):
        sizing = quality_sizing("v1", "v1")
        self.assertEqual([e for e in errors_for(sizing) if "quality_dip" in e and "agent_swap" in e], [])
        for missing in ("quality_dip_facts", "quality_dip_assessment", "cash_stance_turn_facts"):
            broken = copy.deepcopy(sizing)
            del broken[missing]
            self.assertTrue(any("agent_swap.quality_dip 必須與 %s 同時存在" % missing in e for e in errors_for(broken)), missing)
        no_exposure = copy.deepcopy(sizing)
        del no_exposure["agent_swap"]["exposure"]
        self.assertTrue(any("agent_swap.quality_dip 必須與 agent_swap.exposure 同時存在" in e for e in errors_for(no_exposure)))
        v2 = quality_sizing("v2", "v2")
        self.assertEqual([e for e in agent_swap_errors(v2) if "quality_dip" in e], [])

    def test_declared_human_only_and_forbidden_to_reflection(self):
        self.assertIn("agent_swap.quality_dip", GATE_FIELDS)
        self.assertNotIn("agent_swap.quality_dip", POSITION_SIZING_FIELDS)  # 巢狀欄位：頂層欄位集合不變
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "agent_swap.quality_dip")
        self.assertTrue(rule.human_only)
        self.assertEqual(set(rule.requires), {
            "agent_swap", "agent_swap.exposure", "quality_dip_facts", "quality_dip_assessment", "cash_stance_turn_facts"})
        self.assertTrue(any("agent_swap.quality_dip".startswith(prefix) for prefix in FORBIDDEN_PREFIXES))

    def test_binding_accepts_quality_dip_purpose_only_with_the_gate(self):
        ref = {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"}
        pairs = [{"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "quality_dip"}]
        sizing = quality_sizing("v2", "v2")
        sizing["agent_swap"].update(nomination_ref=ref, nominated_pairs=pairs)
        self.assertEqual([e for e in agent_swap_errors(sizing) if "nominated_pairs" in e], [])
        del sizing["agent_swap"]["quality_dip"]
        self.assertTrue(any("nominated_pairs" in e for e in agent_swap_errors(sizing)))


class BindingHelperTests(unittest.TestCase):
    def sizing(self, version="v2", agent_version="v2", **gate):
        return {"agent_swap": {
            "version": agent_version, "max_swaps_per_run": 2, "min_holding_days": 5, "exposure": {"version": "v1"},
            "quality_dip": dict(GATE, version=version, **gate),
            "nominated_pairs": [
                {"buy_symbol": "1101.TW", "sell_symbol": "2330.TW", "purpose": "quality_dip"},
                {"buy_symbol": "2412.TW", "sell_symbol": "2317.TW", "purpose": "thesis"},
            ],
            "nomination_ref": {"nomination_id": "n", "sha256": "a" * 64, "status": "completed"},
        }}

    def test_gate_lookup_and_purposes(self):
        self.assertIsNone(quality_dip_swap_gate(None))
        self.assertIsNone(quality_dip_swap_gate({"agent_swap": {"version": "v2"}}))
        self.assertEqual(nomination_purposes({"agent_swap": {"exposure": {"version": "v1"}}}), ("thesis", "exposure"))
        self.assertEqual(nomination_purposes(self.sizing()), ("thesis", "exposure", "quality_dip"))

    def test_binding_reads_nothing_without_the_gate_or_for_the_shadow_agent_swap(self):
        self.assertIsNone(quality_dip_swap_binding({}))
        self.assertIsNone(quality_dip_swap_binding(self.sizing("v1", "v1")))

    def test_v1_binding_has_only_purposes_and_v2_needs_a_bound_turn(self):
        self.assertEqual(quality_dip_swap_binding(self.sizing("v1")), {"version": "v1", "purposes": ["quality_dip", "thesis"]})
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(self.sizing("v2"))  # 沒綁 turn_assessment：不靜默當作沒有轉向跡象
        bound = quality_dip_swap_binding(self.sizing("v2", turn_assessment="turn_unconfirmed", oversold=dict(OVERSOLD)))
        self.assertEqual((bound["cap"], bound["entry_weight"], bound["oversold"]), (1, Decimal("0.03"), {"cap": 1, "entry_weight": Decimal("0.02")}))
        sizing = self.sizing("v1")
        sizing["agent_swap"]["nominated_pairs"][0]["purpose"] = "dip"
        with self.assertRaises(DecisionToolError):
            quality_dip_swap_binding(sizing)

    def test_bind_turn_only_for_v2(self):
        stance = {"turn_assessment": "turn_confirmed"}
        sizing = self.sizing("v2")
        bound = bind_quality_dip_turn(sizing["agent_swap"], sizing, stance)
        self.assertEqual(bound["quality_dip"]["turn_assessment"], "turn_confirmed")
        self.assertNotIn("turn_assessment", sizing["agent_swap"]["quality_dip"])  # 不改原物件
        shadow = self.sizing("v1")
        self.assertNotIn("turn_assessment", bind_quality_dip_turn(shadow["agent_swap"], shadow, stance)["quality_dip"])
        for bad in ({}, {"turn_assessment": "maybe"}):
            with self.assertRaises(DecisionToolError):
                bind_quality_dip_turn(sizing["agent_swap"], sizing, bad)

    def test_turn_allows_requires_a_turn_sign(self):
        for turn, minimum, expected in (
            ("no_turn_evidence", "turn_unconfirmed", False), ("turn_unconfirmed", "turn_unconfirmed", True),
            ("turn_confirmed", "turn_unconfirmed", True), ("turn_unconfirmed", "turn_confirmed", False),
            ("turn_confirmed", "turn_confirmed", True),
        ):
            self.assertEqual(turn_allows({"turn_assessment": turn, "min_turn": minimum}), expected, (turn, minimum))


# ---------------------------------------------------------------- 2. 5c 事實、簡報與 Validator

class FactsAndBriefTests(unittest.TestCase):
    def test_without_the_gate_facts_and_brief_are_unchanged(self):
        plain = QualityWorld(gate_on=False)
        facts = nomination_facts(*plain.inputs(), plain.exposure_facts)
        self.assertEqual(facts, nomination_facts(*plain.inputs(), plain.exposure_facts, plain.dip_facts))  # 沒有閘門時不讀 dip_facts
        self.assertFalse(any("quality_dip_reasons" in row for row in facts["candidates"] + facts["victims"]))
        brief = swap_nomination_brief(*plain.inputs(), exposure_facts=plain.exposure_facts, dip_facts=plain.dip_facts)
        self.assertNotIn("quality_dip_swap", brief)
        self.assertFalse(any("dip" in row or "quality_dip_nominable" in row for row in brief["candidates"] + brief["holdings"]))

    def test_the_gate_adds_keys_without_changing_thesis_eligibility_or_the_need(self):
        plain, world = QualityWorld(gate_on=False), QualityWorld()
        before = nomination_facts(*plain.inputs(), plain.exposure_facts)
        after = world.facts()
        strip = lambda rows: [{k: v for k, v in row.items() if not k.startswith("quality_dip")} for row in rows]
        self.assertEqual(strip(after["candidates"]), before["candidates"])
        self.assertEqual(strip(after["victims"]), before["victims"])
        self.assertEqual(needs_nomination(after), needs_nomination(before))
        candidate = after["candidates"][0]
        self.assertEqual((candidate["quality_dip_nominable"], candidate["quality_dip_reasons"]), (True, []))
        victims = {row["symbol"]: row for row in after["victims"]}
        self.assertTrue(victims["2330.TW"]["quality_dip_nominable"])
        self.assertEqual(victims["2317.TW"]["quality_dip_nominable"], False)  # 沒有空頭依據：thesis 規則照舊

    def test_brief_has_the_block_and_background_only(self):
        brief = QualityWorld().brief()
        self.assertEqual(brief["quality_dip_swap"]["oversold_enabled"], False)
        candidate = brief["candidates"][0]
        self.assertEqual(candidate["dip"], {"citable": False, "candidate": "market_dragged", "price_pattern": "market_explained",
                                            "dip_assessment": "market_driven"})
        self.assertNotIn("dip-facts:abc123", json.dumps(brief, ensure_ascii=False))  # 背景不帶識別碼
        self.assertNotIn("max_entry_weight", json.dumps(brief))  # 數字上限由程式在配置時用，不給 Agent

    def test_missing_dip_facts_with_the_gate_is_refused(self):
        world = QualityWorld()
        world.dip_facts = None
        with self.assertRaises(DecisionToolError):
            world.facts()


class ValidatorTests(unittest.TestCase):
    def assert_rejected(self, world, raw, text):
        errors = world.validate(raw)
        self.assertTrue(any(text in error for error in errors), errors)

    def test_a_well_formed_quality_dip_pair_passes(self):
        self.assertEqual(QualityWorld().validate(output()), [])

    def test_new_adverse_facts_no_decline_or_unavailable_facts_are_refused(self):
        for label, facts, code in (
            ("新壞事", dip_facts(candidate="none", adverse=[{"item": "revenue_yoy_turned_negative"}]), "new_adverse_facts"),
            ("沒跌", dip_facts(candidate="none", pattern="no_decline"), "no_decline"),
            ("整份不可用", dip_facts(status="unavailable"), "dip_facts_unavailable"),
            ("事實沒有該股", dip_facts(symbol="2412.TW"), "no_dip_row"),
        ):
            with self.subTest(label):
                self.assert_rejected(QualityWorld(facts=facts), output(), code)

    def test_assessment_must_match_the_candidate_label(self):
        for assessment in ("not_a_dip", "stock_specific", "undetermined", "oversold_cause_unclear", None):
            with self.subTest(assessment):
                self.assert_rejected(QualityWorld(items=quality_items(assessment)), output(), "assessment_mismatch")

    def test_oversold_candidates_are_excluded_unless_the_block_is_written(self):
        oversold = dip_facts(candidate="oversold", pattern="beyond_market")
        self.assert_rejected(QualityWorld(items=quality_items("oversold_cause_unclear"), facts=oversold), output(), "oversold_not_enabled")
        gate = dict(GATE, oversold=dict(OVERSOLD))
        self.assertEqual(QualityWorld(items=quality_items("oversold_cause_unclear"), facts=oversold, gate=gate).validate(output()), [])
        self.assert_rejected(QualityWorld(items=quality_items("market_driven"), facts=oversold, gate=gate), output(), "assessment_mismatch")

    def test_a_non_price_bull_claim_is_required(self):
        for label, kinds in (("只有價格論點", {"bull-1101-1": "price"}), ("沒填 claim_kind", {"bull-1101-1": None})):
            with self.subTest(label):
                world = QualityWorld(kinds=kinds)
                errors = world.validate(output())
                self.assertTrue(any("no_non_price_bull_basis" in e for e in errors), errors)
                self.assertTrue(any("claim_kind 不是 price" in e for e in errors), errors)

    def test_conviction_below_the_gate_is_refused(self):
        self.assert_rejected(QualityWorld(gate=dict(GATE, min_new_conviction="high")), output(), "conviction_below_gate")

    def test_the_victim_must_be_hold_low_held_long_enough_and_keep_the_thesis_sell_basis(self):
        self.assert_rejected(QualityWorld(items=quality_items(victim_conviction="medium")), output(), "hold_conviction_not_low")
        self.assert_rejected(QualityWorld(gate=dict(GATE, min_holding_days=30)), output(), "holding_days_short_for_gate")
        world = QualityWorld()
        self.assert_rejected(world, output([quality_pair(sell_symbol="2317.TW", sell_basis_ids=["bull-2317-1"])]), "2317.TW")
        self.assert_rejected(world, output([quality_pair(sell_basis_ids=["review:2330.TW:exposure"])]), "曝險事實編號")

    def test_missing_data_neither_passes_nor_vetoes_the_same_stock_as_a_thesis_pair(self):
        world = QualityWorld(facts=dip_facts(status="unavailable"), kinds={"bull-1101-1": None})
        self.assertTrue(world.validate(output()))  # 不算通過：不能標 quality_dip
        self.assertEqual(world.validate(output([quality_pair(purpose="thesis")])), [])  # 也不否決：thesis 照原本規則

    def test_quality_dip_purpose_without_the_gate_is_refused(self):
        world = QualityWorld(gate_on=False)
        self.assert_rejected(world, output(), "purpose 必須是 thesis／exposure")

    def test_background_identifiers_in_the_reason_are_refused(self):
        world = QualityWorld()
        self.assert_rejected(world, output([quality_pair(reason="依 dip-facts:abc123 的標示買進")]), "不得引用前次決策背景")

    def test_team_validation_reads_dip_facts_from_team_inputs(self):
        world = QualityWorld()
        nomination = seal_swap_nomination(world.envelope, output(), True)
        team = {"swap_nomination": nomination, "selection_ranking": world.ranking, "position_review": world.review,
                "exposure_review": world.exposure, "exposure_facts": world.exposure_facts, "dip_facts": world.dip_facts}
        errors: list = []
        validate_team_swap_nomination(world.bundle, world.policy, world.trade, world.debate, team, errors)
        self.assertEqual(errors, [])
        del team["dip_facts"]
        validate_team_swap_nomination(world.bundle, world.policy, world.trade, world.debate, team, errors)
        self.assertTrue(errors)


# ---------------------------------------------------------------- 3. 配置（v2）

def bind_quality(bundle, intent, policy, purposes, version="v2", turn="turn_unconfirmed", gate=None, assessment="market_driven",
                 policy_changes=None):
    """把 purpose、``agent_swap.exposure`` v1、``agent_swap.quality_dip`` 與前提閘門綁進已有 nominated_pairs 的 Policy，重跑配置並重建驗證。"""
    bound = copy.deepcopy(policy)
    bound.update(policy_changes or {})
    sizing = bound["position_sizing"]
    swap = sizing["agent_swap"]
    swap["nominated_pairs"] = [dict(pair, purpose=purpose) for pair, purpose in zip(swap["nominated_pairs"], purposes)]
    swap["exposure"] = {"version": "v1"}
    quality = dict(GATE if gate is None else gate, version=version)
    if version == "v2":
        quality["turn_assessment"] = turn
    swap["quality_dip"] = quality
    sizing.update(exposure_view={"version": "v1"}, market_index_facts=dict(MARKET_INDEX_GATE),
                  quality_dip_facts={"version": "v1"}, quality_dip_assessment={"version": "v1"})
    bound["content_sha256"] = decision_policy_sha256(bound)
    intent = copy.deepcopy(intent)
    for item in intent["items"]:
        if item["intent"] in ("buy", "add"):
            item["dip_assessment"] = assessment
    body = AllocationOrderEngine(bundle, bound).run(intent)
    problems = ProposalValidator(bundle, bound, intent).validate(body)
    if problems:
        raise AssertionError(problems)
    return intent, bound, body


def quality_world(purposes=(PURPOSE_QUALITY_DIP,), bundle=None, policy_fields=None, **kwargs):
    bundle = bundle or exposure_bundle()
    intent, policy, plain, _ = v2_world(bundle, pairs=[PAIR], policy_fields=policy_fields)
    intent, bound, body = bind_quality(bundle, intent, policy, list(purposes), **kwargs)
    return bundle, intent, bound, body, plain


def agent_flags(body):
    return sorted((f["symbol"], f["code"]) for f in body["constraint_flags"] if f["code"].startswith(("AGENT_SWAP", "RISK_QUALITY_DIP")))


def order_of(body, symbol, side):
    return next((o for o in body["order_proposal"]["orders"] if o["symbol"] == symbol and o["side"] == side), None)


class FormalAllocationTests(unittest.TestCase):
    def assert_buy_within_proceeds(self, body):
        buy, sell = order_of(body, "2317.TW", "buy"), order_of(body, "2330.TW", "sell")
        self.assertIsNotNone(buy)
        self.assertIsNotNone(sell)
        self.assertLessEqual(-Decimal(buy["net_cash_impact"]), Decimal(sell["net_cash_impact"]))

    def test_a_quality_dip_pair_is_filled_within_the_entry_weight_and_the_proceeds(self):
        bundle, intent, policy, body, plain = quality_world(gate=dict(GATE, max_entry_weight="0.5"))
        self.assertEqual(sold_and_bought(body), ({"2330.TW"}, {"2317.TW"}))
        self.assertIn(("2330.TW", "AGENT_SWAP"), agent_flags(body))
        self.assertIn(("2317.TW", "AGENT_SWAP_FILL"), agent_flags(body))
        self.assert_buy_within_proceeds(body)
        nav = Decimal(body["allocation_proposal"]["nav"])
        self.assertLessEqual(Decimal(order_of(body, "2317.TW", "buy")["gross_amount"]) / nav, Decimal("0.5"))
        victim = next(f for f in body["constraint_flags"] if f["code"] == "AGENT_SWAP")
        self.assertIn("目的：優質股被大盤拖累", victim["detail"])
        self.assertEqual(next(i for i in intent["items"] if i["symbol"] == "2330.TW")["intent"], "hold")  # 意圖不被改寫
        self.assertTrue(guard(bundle, policy, body)["passed"])
        # 對照：同一對以 thesis 成交時買進金額超過賣款（賣 2330 一張、買 2317 三張），quality_dip 把它壓回賣款以內。
        self.assertGreater(order_of(plain, "2317.TW", "buy")["shares"], order_of(body, "2317.TW", "buy")["shares"])

    def test_the_entry_weight_caps_the_buy_and_a_tiny_cap_cancels_the_pair_without_selling(self):
        bundle, _, policy, body, _ = quality_world(gate=dict(GATE, max_entry_weight="0.12"))
        buy = order_of(body, "2317.TW", "buy")
        self.assertIsNotNone(buy)
        self.assertLessEqual(Decimal(buy["gross_amount"]) / Decimal(body["allocation_proposal"]["nav"]), Decimal("0.12"))
        self.assertIn(("2317.TW", "RISK_QUALITY_DIP_ENTRY"), agent_flags(body))
        self.assert_buy_within_proceeds(body)
        _, _, policy, tiny, _ = quality_world(gate=dict(GATE, max_entry_weight="0.001"))
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_entry"), agent_flags(tiny))
        self.assertEqual(tiny["order_proposal"]["orders"], [])  # 換出者不賣，不會只賣不買
        self.assertTrue(guard(bundle, policy, tiny)["passed"])

    def test_turn_assessment_below_the_gate_cancels_every_quality_dip_pair(self):
        for turn, minimum in (("no_turn_evidence", "turn_unconfirmed"), ("turn_unconfirmed", "turn_confirmed")):
            with self.subTest(turn):
                _, _, _, body, _ = quality_world(turn=turn, gate=dict(GATE, min_turn_assessment=minimum, max_entry_weight="0.5"))
                self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_turn")])
                self.assertEqual(body["order_proposal"]["orders"], [])

    def test_a_defensive_stance_floor_or_unknown_drawdown_skips_the_pairs(self):
        for label, level, control, code in (
            ("stance_floor", "defensive", {"stance_floor": "defensive"}, "quality_dip_stance_floor"),
            ("歷史不可用", "neutral", {"status": "unavailable", "stance_floor": None, "drawdown": None, "peak_nav": None, "history_length": 0},
             "quality_dip_drawdown_unavailable"),
        ):
            with self.subTest(label):
                bundle = exposure_bundle()
                intent, policy, _, _ = v2_world(bundle, pairs=[PAIR], level=level, grade="middle")
                bound = copy.deepcopy(policy)
                bound["drawdown_control"] = dict(bound["drawdown_control"], **control)
                _, _, body = bind_quality(bundle, intent, bound, [PURPOSE_QUALITY_DIP], gate=dict(GATE, max_entry_weight="0.5"))
                self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:%s" % code), agent_flags(body))
                self.assertFalse(sold_and_bought(body)[0] & {"2330.TW"})  # 換出者不賣

    def test_reuse_of_sell_proceeds_is_required(self):
        _, _, _, body, _ = quality_world(
            bundle=exposure_bundle(reuse_sell_proceeds=False), gate=dict(GATE, max_entry_weight="0.5"),
            policy_fields={"reuse_sell_proceeds": False})
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_proceeds")])
        self.assertFalse(sold_and_bought(body)[0])

    def test_a_zero_cap_and_a_mismatched_assessment_cancel_from_the_tail(self):
        _, _, _, body, _ = quality_world(gate=dict(GATE, max_swaps_per_run=0))
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_cap")])
        _, _, _, body, _ = quality_world(gate=dict(GATE, max_entry_weight="0.5"), assessment="oversold_cause_unclear")
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_assessment")])  # 沒有 oversold 區塊
        _, _, _, body, _ = quality_world(gate=dict(GATE, max_entry_weight="0.5", oversold={"max_swaps_per_run": 0, "max_entry_weight": "0.02"}),
                                         assessment="oversold_cause_unclear")
        self.assertEqual(agent_flags(body), [("2317.TW", "AGENT_SWAP_CANCELLED:quality_dip_cap")])
        for found in (body,):
            self.assertEqual(found["order_proposal"]["orders"], [])

    def test_the_turnover_cap_and_the_derisk_day_are_not_relaxed(self):
        bundle, _, policy, body, _ = quality_world(gate=dict(GATE, max_entry_weight="0.5"), policy_fields={"max_turnover_rate": "0.05"})
        self.assertIn(("2317.TW", "AGENT_SWAP_CANCELLED:turnover"), agent_flags(body))
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.05"))
        self.assertTrue(guard(bundle, policy, body)["passed"])
        bundle = exposure_bundle(cash="136000", positions={"2330.TW": 1000, "1101.TW": 7000})
        intent, policy, _, control = v2_world(bundle, pairs=[PAIR], level="defensive", grade="lower")
        _, _, body = bind_quality(bundle, intent, policy, [PURPOSE_QUALITY_DIP], gate=dict(GATE, max_entry_weight="0.5"))
        found = codes(body)
        self.assertIn("RISK_DRAWDOWN", found)
        self.assertIn("AGENT_SWAP_SKIPPED:derisk_day", found)
        self.assertFalse(found & {"AGENT_SWAP", "AGENT_SWAP_FILL"})
        self.assertEqual(body["order_proposal"]["orders"], control["order_proposal"]["orders"])

    def test_the_cash_floor_is_not_relaxed(self):
        bundle, _, policy, body, _ = quality_world(gate=dict(GATE, max_entry_weight="0.5"))
        cash = Decimal(body["allocation_proposal"]["cash"]) / Decimal(body["allocation_proposal"]["nav"])
        floor = Decimal(body["effective_policy"]["cash_buffer_rate"])
        self.assertGreaterEqual(cash, floor)

    def test_the_shadow_version_ignores_quality_dip_pairs_bit_for_bit(self):
        bundle = exposure_bundle()
        intent, _, plain, _ = v2_world(bundle, pairs=[])
        _, policy, _, _ = v2_world(bundle, pairs=[PAIR])
        _, _, shadow = bind_quality(bundle, intent, policy, [PURPOSE_QUALITY_DIP], version="v1")
        for key in ("allocation_proposal", "order_proposal", "constraint_flags"):
            self.assertEqual(shadow[key], plain[key], key)  # 不會被當成 thesis 配對成交
        self.assertFalse([code for code in codes(shadow) if code.startswith("AGENT_SWAP")])

    def test_an_unbound_turn_is_refused_instead_of_running_without_a_turn_sign(self):
        bundle = exposure_bundle()
        intent, policy, _, _ = v2_world(bundle, pairs=[PAIR])
        bound, _ = bind_quality(bundle, intent, policy, [PURPOSE_QUALITY_DIP])[1:]
        del bound["position_sizing"]["agent_swap"]["quality_dip"]["turn_assessment"]
        bound["content_sha256"] = decision_policy_sha256(bound)
        with self.assertRaises(DecisionToolError):
            AllocationOrderEngine(bundle, bound).run(intent)

    def test_without_the_gate_the_allocation_is_unchanged(self):
        bundle = exposure_bundle()
        _, _, plain, _ = v2_world(bundle, pairs=[PAIR])
        self.assertEqual(summary(plain), [("2317.TW", "buy", 3000, "buy"), ("2330.TW", "sell", 1000, "exit")])
        self.assertFalse([f for f in plain["constraint_flags"] if "QUALITY_DIP" in f["code"] or "quality_dip" in f["code"]])


# ---------------------------------------------------------------- 4. 正式樣板回歸

class ProductionTemplateTests(unittest.TestCase):
    def test_the_live_template_has_no_quality_dip_gate_and_the_nomination_contract_is_unchanged(self):
        live = json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        sizing = live["position_sizing"]
        # 2026-11-10 23:05 正式樣板開啟 agent_swap.quality_dip v1、23:25 切到 v2、2026-11-11 階段 0b 切到 v3、同日再切到 v4；契約本身不變，只確認閘門形狀。
        self.assertEqual(quality_dip_swap_gate(sizing)["version"], "v4")
        self.assertEqual(nomination_purposes(sizing), ("thesis", "exposure", "quality_dip"))
        pair = SWAP_NOMINATION_EXPOSURE_SCHEMA["properties"]["nominations"]["items"]
        self.assertEqual(pair["properties"]["purpose"], {"enum": ["thesis", "exposure"]})
        self.assertNotIn("purpose", SWAP_NOMINATION_SCHEMA["properties"]["nominations"]["items"]["properties"])
        quality = SWAP_NOMINATION_QUALITY_DIP_SCHEMA["properties"]["nominations"]["items"]
        self.assertEqual(quality["properties"]["purpose"], {"enum": ["thesis", "exposure", "quality_dip"]})
        self.assertEqual(set(quality["properties"]) - {"purpose"}, set(pair["properties"]) - {"purpose"})


if __name__ == "__main__":
    unittest.main()
