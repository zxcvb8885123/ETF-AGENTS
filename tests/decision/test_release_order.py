"""回撤減碼的釋出順序（drawdown_derisk v5，docs/plans/agent_autonomy_plan.md 階段 2）：契約、事實、Validator、Policy 綁定與配置。

覆蓋：

1. 事實與 Validator（純單元）：可減碼持股集合、可引用依據、中性回饋、``not_needed``／降級的形狀、信封與雜湊、前次決策識別碼。
2. Policy：v5 須有 selection_ranking、綁定欄位只用於 v5 且形狀固定、未綁定的 v5 在配置拋錯、v5 與 rebound／high_conviction_cash_floor 並用。
3. 配置：依 Agent 的順序賣、順序第一檔不夠時往下、ATR 相同順序時與 v4 訂單逐位相同、非減碼日與 v4 逐位相同、
   exhausted／unavailable 退回 v4 順序並留 ``DERISK_ORDER_SKIPPED:*``（只在真的減碼時）、not_needed 不留旗標。
4. 舊版不變：v1～v4 的 Policy 驗證與配置不受影響（既有測試涵蓋，這裡只確認 v4 與 v5 共用的數字規則）。

夾具：事實與 Validator 沿用換股提名的三檔世界（持 2317、2330 各一張；兩檔都是第一輪 hold）；配置沿用 test_budget_and_derisk_gates 的
兩檔持股世界（防守姿態、現金 3 萬）。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    DecisionToolError,
    ProposalValidator,
    apply_selection_ranking,
    bind_drawdown,
    decision_policy_sha256,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.rebound import validate_rebound_gate
from etf_agent.decision.release_order import (
    RELEASE_ORDER_KEY,
    ReleaseOrderValidator,
    apply_release_order,
    needs_release_order,
    ordered_symbols,
    program_release_order,
    release_envelope,
    release_facts,
    release_order_brief,
    seal_release_order,
    validate_team_release_order,
)
from etf_agent.decision.selection_ranking import program_ranking, ranking_envelope
from etf_agent.decision.sizing import (
    DRAWDOWN_DERISK_NO_READD_VERSIONS,
    DRAWDOWN_DERISK_VERSIONS,
    drawdown_derisk_v2,
    drawdown_derisk_v3,
    drawdown_derisk_v4,
    drawdown_derisk_v5,
    release_order_binding,
    validate_position_sizing,
)
from etf_agent.decision.trader import REDUCING

from test_budget_and_derisk_gates import reseal, two_positions
from test_cash_fit_and_limits import with_rules
from test_portfolio_risk_decision import policy as base_policy
from test_position_sizing import sized_inputs, sized_policy
from test_risk_limit_gates import CONTROL, flags, orders
from test_swap_nomination import World, first_round_items
from etf_agent.decision import MomentumEngine


def good_output(*symbols):
    """預設順序：2330 先、2317 後；每檔引用自己已採納的依據。"""
    basis = {"2330.TW": ["bear-2330-1", "review:2330.TW:position"], "2317.TW": ["bull-2317-1"]}
    order = symbols or ("2330.TW", "2317.TW")
    return {"sequence": [{"symbol": symbol, "basis_ids": basis[symbol], "reason": "%s 的依據各自成立。" % symbol} for symbol in order]}


class ReleaseWorld(World):
    """換股提名的三檔世界加上釋出順序的信封；兩檔持股都是第一輪 hold，所以可減碼集合是 {2317, 2330}。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release_envelope = release_envelope(self.bundle, self.trade, self.ranking, "release-1")

    def facts(self):  # type: ignore[override]
        return release_facts(self.bundle, self.trade, self.debate, self.review)

    def validator(self, prior_context=None):  # type: ignore[override]
        return ReleaseOrderValidator(self.bundle, self.trade, self.ranking, self.debate, self.review, prior_context=prior_context)

    def validate(self, raw, prior_context=None):  # type: ignore[override]
        return self.validator(prior_context).validate(seal_release_order(self.release_envelope, raw))


def has(errors, token):
    return any(token in error for error in errors)


class FactsTests(unittest.TestCase):
    def test_releasable_holdings_and_their_basis(self):
        facts = ReleaseWorld().facts()
        rows = {row["symbol"]: row for row in facts["releasable"]}
        self.assertEqual(sorted(rows), ["2317.TW", "2330.TW"])
        # 已採納的多空 claim、持股檢視警示與持股事實編號；第一輪已否決的 bull-2330-1 不在內。
        self.assertEqual(rows["2330.TW"]["basis_ids"], ["bear-2330-1", "review:2330.TW:position"])
        self.assertEqual(rows["2317.TW"]["basis_ids"], ["bull-2317-1", "review:2317.TW:position"])
        self.assertEqual(facts["excluded"], [])
        self.assertTrue(needs_release_order(facts))

    def test_first_round_reducing_and_untradable_holdings_are_not_releasable(self):
        for intent in sorted(REDUCING):
            items = first_round_items(**{"2317.TW": dict(first_round_items()[1], intent=intent, conviction=None, adopted_claim_ids=["review:2317.TW:position"], rejected_claim_ids=["bull-2317-1"])})
            facts = ReleaseWorld(items).facts()
            self.assertEqual([row["symbol"] for row in facts["releasable"]], ["2330.TW"], intent)
            self.assertEqual(facts["excluded"], [{"symbol": "2317.TW", "reasons": ["first_round_reducing"]}], intent)
            self.assertFalse(needs_release_order(facts), intent)  # 只剩一檔：沒有先後可排

        def untradable(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "2330.TW", "state": "restricted"}]}

        facts = ReleaseWorld(mutate_bundle=untradable).facts()
        self.assertEqual(facts["excluded"], [{"symbol": "2330.TW", "reasons": ["not_tradable"]}])
        # 持股缺行情時持股檢視本身就建不出來（position_review 先拒絕），所以 no_price 是防呆分支，不在這個世界重現。

    def test_rejected_claims_are_never_basis_but_the_position_fact_always_is(self):
        world = ReleaseWorld(first_round_items(sell_adopted=(), sell_rejected=("bear-2330-1", "bull-2330-1")))
        rows = {row["symbol"]: row for row in world.facts()["releasable"]}
        self.assertEqual(rows["2330.TW"]["basis_ids"], ["review:2330.TW:position"])

    def test_missing_review_fails_closed(self):
        world = ReleaseWorld()
        with self.assertRaises(DecisionToolError):
            release_facts(world.bundle, world.trade, world.debate, None)
        broken = {"items": [item for item in world.review["items"] if item["symbol"] != "2330.TW"]}
        # 2330 沒有持股事實編號、又沒有任何已採納依據：湊不出依據就拒絕，不硬編。
        world = ReleaseWorld(first_round_items(sell_adopted=(), sell_rejected=("bear-2330-1", "bull-2330-1")))
        with self.assertRaisesRegex(DecisionToolError, "沒有任何可引用的依據"):
            release_facts(world.bundle, world.trade, world.debate, broken)

    def test_brief_has_facts_but_no_stance_or_budget(self):
        world = ReleaseWorld()
        brief = release_order_brief(world.bundle, world.trade, world.debate, world.review, MomentumEngine(world.bundle).run())
        self.assertEqual([row["symbol"] for row in brief["holdings"]], ["2317.TW", "2330.TW"])
        row = next(row for row in brief["holdings"] if row["symbol"] == "2330.TW")
        self.assertEqual({basis["id"] for basis in row["basis"]}, {"bear-2330-1", "review:2330.TW:position"})
        self.assertIn("weight", row)
        text = str(brief)
        for forbidden in ("cash_stance", "defensive", "shortfall", "max_derisk_fraction", "stance"):
            self.assertNotIn(forbidden, text)


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.world = ReleaseWorld()

    def test_complete_order_passes_in_either_direction(self):
        self.assertEqual(self.world.validate(good_output()), [])
        self.assertEqual(self.world.validate(good_output("2317.TW", "2330.TW")), [])

    def test_missing_duplicate_unknown_and_unheld_symbols_are_rejected_with_neutral_feedback(self):
        errors = self.world.validate(good_output("2330.TW"))
        self.assertTrue(has(errors, "順序缺少可減碼的持股：2317.TW"), errors)
        self.assertTrue(has(errors, "每檔都必須列出一次"), errors)
        errors = self.world.validate({"sequence": good_output()["sequence"] + [good_output("2330.TW")["sequence"][0]]})
        self.assertTrue(has(errors, "只能出現一次：2330.TW"), errors)
        for symbol, why in (("1101.TW", "未持有"), ("9999.TW", "不在交易池")):
            entry = dict(good_output("2330.TW")["sequence"][0], symbol=symbol)
            errors = self.world.validate({"sequence": good_output()["sequence"] + [entry]})
            self.assertTrue(has(errors, "不在可減碼持股內（%s）" % why), (symbol, errors))
            self.assertTrue(has(errors, "請從順序中移除，或改列其他持股"), errors)  # 中性回饋：不暗示該降級或該硬湊
        entry = dict(good_output("2330.TW")["sequence"][0], symbol="2330.tw")
        self.assertTrue(has(self.world.validate({"sequence": [entry] + good_output("2317.TW")["sequence"]}), "必須是大寫代號字串"))

    def test_excluded_holdings_are_named_with_their_reason(self):
        world = ReleaseWorld(first_round_items(**{"2317.TW": dict(first_round_items()[1], intent="exit", conviction=None, adopted_claim_ids=["review:2317.TW:position"], rejected_claim_ids=["bull-2317-1"])}))
        errors = world.validate(good_output())
        self.assertTrue(has(errors, "2317.TW 不在可減碼持股內（first_round_reducing）"), errors)

    def test_basis_must_be_the_symbols_own_adopted_ids(self):
        cases = {
            "第一輪已否決": ["bull-2330-1"],
            "屬於其他股票": ["bull-2317-1"],
            "不存在": ["bear-9999-1"],
        }
        for why, basis in cases.items():
            output = good_output()
            output["sequence"][0]["basis_ids"] = basis
            errors = self.world.validate(output)
            self.assertTrue(has(errors, "不可引用（%s）" % why), (why, errors))
            self.assertTrue(has(errors, "改引用該股自己已採納的 claim"), errors)
        for bad in ([], None, [""], ["bear-2330-1", "bear-2330-1"]):
            output = good_output()
            output["sequence"][0]["basis_ids"] = bad
            self.assertTrue(self.world.validate(output), bad)

    def test_unadopted_claim_is_not_basis(self):
        world = ReleaseWorld(first_round_items(sell_adopted=("review:2330.TW:position",), sell_rejected=("bull-2330-1",)))
        output = good_output()  # bear-2330-1 既沒採納也沒否決
        errors = world.validate(output)
        self.assertTrue(has(errors, "第一輪沒有採納"), errors)

    def test_reason_must_be_text_and_must_not_cite_the_prior_decision(self):
        output = good_output()
        output["sequence"][0]["reason"] = "  "
        self.assertTrue(has(self.world.validate(output), "reason 必須是非空字串"))
        prior = {"context_id": "prior-ctx-9", "source": {"run_id": "decision-old-1"}}
        output = good_output()
        output["sequence"][1]["reason"] = "沿用 decision-old-1 的看法。"
        self.assertTrue(has(self.world.validate(output, prior_context=prior), "不得引用前次決策背景"))
        self.assertEqual(self.world.validate(good_output(), prior_context=prior), [])

    def test_numbers_and_unknown_fields_are_rejected(self):
        output = good_output()
        output["sequence"][0]["weight"] = "0.1"
        self.assertTrue(has(self.world.validate(output), "未允許欄位"))
        output = dict(good_output(), shares=1000)  # Agent 多給的頂層鍵以 x_ 保留，Validator 拒絕
        self.assertTrue(has(self.world.validate(output), "未允許欄位"))
        self.assertTrue(has(self.world.validate({"sequence": "2330.TW"}), "sequence 必須是陣列"))
        self.assertTrue(self.world.validate("not an object"))

    def test_envelope_and_hash_are_bound(self):
        sealed = seal_release_order(self.world.release_envelope, good_output())
        validator = self.world.validator()
        self.assertEqual(validator.validate(sealed), [])
        for field, value in (("trade_decision_sha256", "0" * 64), ("selection_ranking_sha256", "0" * 64), ("bundle_hash", "0" * 64)):
            self.assertTrue(has(validator.validate(dict(sealed, **{field: value})), "不一致"), field)
        self.assertTrue(has(validator.validate(dict(sealed, sequence=sealed["sequence"][:1])), "content_sha256 與內容不一致"))

    def test_program_shapes(self):
        envelope = self.world.release_envelope
        validator = self.world.validator()
        # 本世界需要 Agent 的順序：not_needed 一律拒絕；exhausted／unavailable 的形狀是空順序加非空 errors。
        self.assertTrue(has(validator.validate(program_release_order(envelope, "not_needed")), "不得封存 not_needed"))
        for status in ("exhausted", "unavailable"):
            self.assertEqual(validator.validate(program_release_order(envelope, status, ["驗證失敗"])), [], status)
            self.assertTrue(has(validator.validate(program_release_order(envelope, status)), "errors 必須是 1～10 筆"), status)
            forged = dict(program_release_order(envelope, status, ["x"]), sequence=good_output()["sequence"])
            self.assertTrue(has(validator.validate(forged), "sequence 必須是空陣列"), status)
        with self.assertRaises(DecisionToolError):
            program_release_order(envelope, "completed")

    def test_not_needed_is_required_when_fewer_than_two_holdings_can_be_released(self):
        world = ReleaseWorld(first_round_items(**{"2317.TW": dict(first_round_items()[1], intent="exit", conviction=None, adopted_claim_ids=["review:2317.TW:position"], rejected_claim_ids=["bull-2317-1"])}))
        validator = world.validator()
        self.assertEqual(validator.validate(program_release_order(world.release_envelope, "not_needed")), [])
        self.assertTrue(has(validator.validate(program_release_order(world.release_envelope, "exhausted", ["x"])), "必須由程式封存 not_needed"))
        self.assertTrue(has(validator.validate(seal_release_order(world.release_envelope, {"sequence": []})), "必須由程式封存 not_needed"))

    def test_tampered_inputs_are_detected_by_recomputing_the_facts(self):
        sealed = seal_release_order(self.world.release_envelope, good_output())
        # 2330 在重算時變成不可交易：同一份順序不再符合事實。
        def untradable(bundle):
            bundle["trading_status_bundle"] = {"bundle_id": "t"}
            bundle["tradability_assessment"] = {"symbols": [{"symbol": "2330.TW", "state": "restricted"}]}

        other = ReleaseWorld(mutate_bundle=untradable)
        errors = ReleaseOrderValidator(other.bundle, self.world.trade, self.world.ranking, self.world.debate, self.world.review).validate(sealed)
        self.assertTrue(has(errors, "不在可減碼持股內"), errors)


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.world = ReleaseWorld()
        self.completed = seal_release_order(self.world.release_envelope, good_output())

    @staticmethod
    def v5_policy(prefilled=False):
        policy = {"position_sizing": {"drawdown_derisk": {"version": "v5", "max_derisk_fraction": "0.25"}}}
        if prefilled:
            policy["position_sizing"]["drawdown_derisk"].update(release_symbols=["9999.TW"], release_ref={"release_order_id": "x", "sha256": "0" * 64, "status": "completed"})
        return policy

    def test_ordered_symbols_only_for_completed(self):
        self.assertEqual(ordered_symbols(self.completed), ["2330.TW", "2317.TW"])
        for status in ("exhausted", "unavailable", "not_needed"):
            self.assertEqual(ordered_symbols(program_release_order(self.world.release_envelope, status, ["x"])), [])
        self.assertEqual(ordered_symbols(None), [])

    def test_apply_binds_the_order_and_clears_any_prefilled_binding(self):
        for prefilled in (False, True):
            bound = apply_release_order(self.v5_policy(prefilled), self.completed)
            derisk = bound["position_sizing"]["drawdown_derisk"]
            self.assertEqual(derisk["release_symbols"], ["2330.TW", "2317.TW"])
            self.assertEqual(derisk["release_ref"], {"release_order_id": "release-1", "sha256": self.completed["content_sha256"], "status": "completed"})
            self.assertNotIn("content_sha256", bound)

    def test_apply_leaves_other_versions_untouched(self):
        for version in ("v1", "v2", "v3", "v4"):
            policy = {"position_sizing": {"drawdown_derisk": {"version": version}}}
            self.assertEqual(apply_release_order(policy, self.completed), policy)
        self.assertEqual(apply_release_order({"position_sizing": {}}, self.completed), {"position_sizing": {}})

    def test_degraded_status_binds_an_empty_order_with_the_status(self):
        degraded = program_release_order(self.world.release_envelope, "exhausted", ["x"])
        derisk = apply_release_order(self.v5_policy(), degraded)["position_sizing"]["drawdown_derisk"]
        self.assertEqual((derisk["release_symbols"], derisk["release_ref"]["status"]), ([], "exhausted"))

    def team(self):
        return {"selection_ranking": self.world.ranking, "position_review": self.world.review, RELEASE_ORDER_KEY: self.completed}

    def policy(self):
        return apply_release_order(self.v5_policy(), self.completed)

    def check(self, policy=None, team=None):
        errors = []
        validate_team_release_order(self.world.bundle, policy or self.policy(), self.world.trade, self.world.debate, team or self.team(), errors)
        return errors

    def test_finalizer_check_accepts_the_matching_binding(self):
        self.assertEqual(self.check(), [])

    def test_finalizer_check_rejects_a_policy_binding_that_differs_from_the_sealed_order(self):
        policy = copy.deepcopy(self.policy())
        policy["position_sizing"]["drawdown_derisk"]["release_symbols"] = ["2317.TW", "2330.TW"]
        self.assertTrue(has(self.check(policy), "release_symbols 與 team_inputs.release_order 不一致"))
        policy = copy.deepcopy(self.policy())
        policy["position_sizing"]["drawdown_derisk"]["release_ref"]["sha256"] = "0" * 64
        self.assertTrue(has(self.check(policy), "release_ref 與 team_inputs.release_order 不一致"))
        self.assertTrue(has(self.check(self.v5_policy()), "release_symbols 與 team_inputs.release_order 不一致"))  # 樣板沒有綁定

    def test_finalizer_check_rejects_a_rewritten_order_and_missing_inputs(self):
        team = self.team()
        team[RELEASE_ORDER_KEY] = dict(self.completed, sequence=list(reversed(self.completed["sequence"])))
        self.assertTrue(has(self.check(team=team), "content_sha256 與內容不一致"))
        for missing in ("selection_ranking", "position_review"):
            team = self.team()
            team.pop(missing)
            self.assertTrue(has(self.check(team=team), "才能重建"), missing)
        team = self.team()
        team[RELEASE_ORDER_KEY] = None
        self.assertTrue(has(self.check(team=team), "必須是物件"))


class SizingGateTests(unittest.TestCase):
    @staticmethod
    def sizing_errors(**derisk):
        from test_cash_fit_and_limits import production_policy

        policy = base_policy()
        policy["position_sizing"] = copy.deepcopy(production_policy()["position_sizing"])
        policy["position_sizing"]["cash_ratio_range"].pop("anchor", None)
        policy["position_sizing"]["drawdown_derisk"] = dict({"version": "v5", "max_derisk_fraction": "0.25"}, **derisk)
        policy["drawdown_control"] = dict(CONTROL)
        errors = []
        validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
        return errors, policy

    def test_version_tables_include_v5_without_changing_v4(self):
        self.assertIn("v5", DRAWDOWN_DERISK_VERSIONS)
        self.assertIn("v5", DRAWDOWN_DERISK_NO_READD_VERSIONS)
        for version, expected in (("v1", (False, False, False, False)), ("v2", (True, False, False, False)), ("v3", (True, True, False, False)),
                                  ("v4", (True, True, True, False)), ("v5", (True, True, True, True))):
            sizing = {"drawdown_derisk": {"version": version}}
            self.assertEqual((drawdown_derisk_v2(sizing), drawdown_derisk_v3(sizing), drawdown_derisk_v4(sizing), drawdown_derisk_v5(sizing)), expected, version)
        self.assertFalse(drawdown_derisk_v5({}))
        self.assertFalse(drawdown_derisk_v5(None))

    def test_v5_without_the_bound_order_validates_as_a_template(self):
        errors, _ = self.sizing_errors()
        self.assertEqual(errors, [])

    def test_v5_requires_selection_ranking(self):
        errors, policy = self.sizing_errors()
        policy["position_sizing"].pop("selection_ranking")
        for field in ("rank_by_symbol", "selection_ranking_id", "selection_ranking_sha256"):
            policy["position_sizing"].pop(field, None)
        errors = []
        validate_position_sizing(policy, ["2330.TW", "2317.TW"], errors)
        self.assertTrue(has(errors, "version=v5 必須與 selection_ranking 同時存在"), errors)

    def test_binding_fields_are_only_for_v5_and_have_a_fixed_shape(self):
        ref = {"release_order_id": "r-1", "sha256": "a" * 64, "status": "completed"}
        self.assertEqual(self.sizing_errors(release_symbols=["2330.TW", "2317.TW"], release_ref=ref)[0], [])
        cases = {
            "只有一個欄位": ({"release_symbols": []}, "必須一起出現"),
            "ref 欄位": ({"release_symbols": [], "release_ref": {"release_order_id": "r"}}, "release_ref 欄位"),
            "未知狀態": ({"release_symbols": [], "release_ref": dict(ref, status="done")}, "status 只允許"),
            "重複代號": ({"release_symbols": ["2330.TW", "2330.TW"], "release_ref": ref}, "不重複的大寫代號"),
            "小寫代號": ({"release_symbols": ["2330.tw"], "release_ref": ref}, "不重複的大寫代號"),
            "降級卻有代號": ({"release_symbols": ["2330.TW"], "release_ref": dict(ref, status="exhausted")}, "必須為空"),
        }
        for label, (fields, token) in cases.items():
            self.assertTrue(has(self.sizing_errors(**fields)[0], token), (label, self.sizing_errors(**fields)[0]))
        errors, _ = self.sizing_errors(version="v4", release_symbols=["2330.TW"], release_ref=ref)
        self.assertTrue(has(errors, "只用於 version=v5"), errors)

    def test_unbound_v5_raises_instead_of_falling_back_silently(self):
        self.assertIsNone(release_order_binding({"drawdown_derisk": {"version": "v4"}}))
        self.assertIsNone(release_order_binding({}))
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定釋出順序"):
            release_order_binding({"drawdown_derisk": {"version": "v5"}})
        sizing = {"drawdown_derisk": {"version": "v5", "release_symbols": ["A"], "release_ref": {"status": "completed"}}}
        self.assertEqual(release_order_binding(sizing), (["A"], "completed"))

    def test_rebound_participation_accepts_v5_next_to_v4_and_still_rejects_older_versions(self):
        for version, ok in (("v3", False), ("v4", True), ("v5", True)):
            errors = []
            sizing = {"drawdown_derisk": {"version": version}, "exposure_rotation": {"version": "v3"},
                      "cash_stance_turn_facts": {"version": "v1"}}
            sizing = {**sizing, "rebound_participation": {}}
            validate_rebound_gate(sizing, errors)
            self.assertEqual(any("drawdown_derisk.version=v4" in error for error in errors), not ok, (version, errors))


# ------------------------------------------------------------------ 配置

def allocation_proposal(version, release=None, status="completed", stance="defensive", cash="30000", shares_2330=1000, shares_2317=8000,
                        max_turnover="1", fraction="0.25"):
    """兩檔持股（2330 一張、2317 八張）都是第一輪 hold；防守姿態、現金 3 萬 → 回撤減碼。回傳 (bundle, policy, body)。"""
    bundle, intent, cash_stance, policy = sized_inputs("medium", "low", stance)
    bundle = two_positions(bundle, cash, shares_2330, shares_2317)
    bundle = with_rules(
        bundle, max_stock_weight="1", special_weight_limits={"2330.TW": "1", "2317.TW": "1"}, max_sector_weight="1", min_positions=1,
    )
    items = [
        dict(item, intent="hold", conviction=None, rejected_claim_ids=[],
             adopted_claim_ids=["bull-2317-1"] if item["symbol"] == "2317.TW" else ["bull-2330-1"])
        for item in intent["items"]
    ]
    intent = reseal(bundle, intent, items)
    policy.update(max_stock_weight="1", special_weight_limits={"2330.TW": "1", "2317.TW": "1"}, max_sector_weight="1",
                  liquidity_fill_rate="1", min_positions=1, max_turnover_rate=max_turnover)
    policy["position_sizing"]["turnover_fit"] = "v1"
    derisk = {"version": version, "max_derisk_fraction": fraction}
    if version == "v5":
        policy["position_sizing"]["selection_ranking"] = "v1"
        derisk.update(release_symbols=list(release or []), release_ref={"release_order_id": "r-1", "sha256": "a" * 64, "status": status})
    policy["position_sizing"]["drawdown_derisk"] = derisk
    policy["drawdown_control"] = dict(CONTROL)
    policy = bind_drawdown(policy, bundle, [])
    policy["content_sha256"] = decision_policy_sha256(policy)
    policy = sized_policy(policy, intent, cash_stance, bundle)
    if version == "v5":
        policy = apply_selection_ranking(policy, program_ranking(ranking_envelope(bundle, intent, "rank-1"), intent))
        policy["content_sha256"] = decision_policy_sha256(policy)
    assert DecisionPolicyValidator(bundle).validate(policy) == [], DecisionPolicyValidator(bundle).validate(policy)
    body = AllocationOrderEngine(bundle, policy).run(intent)
    assert ProposalValidator(bundle, policy, intent).validate(body) == []
    return bundle, policy, body


def sells(body):
    return [(order["symbol"], order["shares"], order["intent"]) for order in body["order_proposal"]["orders"] if order["side"] == "sell"]


def derisk_symbols(body):
    return [flag["symbol"] for flag in body["constraint_flags"] if flag["code"] == "RISK_DRAWDOWN"]


def skipped(body):
    return [(flag["symbol"], flag["code"]) for flag in body["constraint_flags"] if flag["code"].startswith("DERISK_ORDER_SKIPPED")]


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v4 = allocation_proposal("v4")[2]
        cls.atr_order = derisk_symbols(cls.v4)  # v4 實際的先後（ATR 高→低）

    def test_the_control_world_derisks_both_holdings_in_atr_order(self):
        self.assertGreaterEqual(len(self.atr_order), 1)
        self.assertTrue(set(self.atr_order) <= {"2330.TW", "2317.TW"})

    def test_an_order_equal_to_the_atr_order_gives_the_same_orders_as_v4(self):
        """等價測試（replay_reference_only）：給定與 ATR 相同的順序時 v5 與 v4 的訂單、配置逐位相同；只有旗標文字不同。"""
        atr_full = self.atr_order + [s for s in ("2330.TW", "2317.TW") if s not in self.atr_order]
        _, _, v5 = allocation_proposal("v5", atr_full)
        self.assertEqual(v5["order_proposal"], self.v4["order_proposal"])
        self.assertEqual(v5["allocation_proposal"], self.v4["allocation_proposal"])
        self.assertEqual([(f["symbol"], f["code"]) for f in v5["constraint_flags"]], [(f["symbol"], f["code"]) for f in self.v4["constraint_flags"]])
        detail = next(f["detail"] for f in v5["constraint_flags"] if f["code"] == "RISK_DRAWDOWN")
        self.assertIn("依交易 Agent 的釋出順序", detail)
        self.assertNotIn("ATR14", detail)
        self.assertIn("ATR14", next(f["detail"] for f in self.v4["constraint_flags"] if f["code"] == "RISK_DRAWDOWN"))

    def test_the_agent_order_decides_who_is_sold_first(self):
        results = {}
        for first in ("2330.TW", "2317.TW"):
            order = [first] + [s for s in ("2330.TW", "2317.TW") if s != first]
            results[first] = allocation_proposal("v5", order)[2]
            self.assertEqual(derisk_symbols(results[first])[0], first, first)
        self.assertNotEqual(sells(results["2330.TW"]), sells(results["2317.TW"]))  # 賣的股票不同
        for body in results.values():
            self.assertEqual(body["order_proposal"]["orders"], sorted(body["order_proposal"]["orders"], key=lambda o: (o["side"] != "sell", o["symbol"])))

    def test_when_the_first_in_order_cannot_cover_the_shortfall_it_continues_down_the_list(self):
        # 2330 只有一張（約 16 萬）補不到缺口（約 19 萬）：先賣光 2330，再往下賣 2317（單次上限放到 NAV 的 50%）。
        _, _, body = allocation_proposal("v5", ["2330.TW", "2317.TW"], fraction="0.5")
        self.assertEqual(derisk_symbols(body), ["2330.TW", "2317.TW"])
        self.assertEqual({symbol: (shares, intent) for symbol, shares, intent in sells(body)}["2330.TW"], (1000, "exit"))

    def test_the_amount_sold_stays_within_the_v4_budget_whatever_the_order(self):
        _, _, v4_body = allocation_proposal("v4", max_turnover="0.30")
        for order in (["2330.TW", "2317.TW"], ["2317.TW", "2330.TW"]):
            bundle, _, body = allocation_proposal("v5", order, max_turnover="0.30")
            at_reference = sum(Decimal(o["reference_price"]) * o["shares"] for o in body["order_proposal"]["orders"] if o["side"] == "sell")
            nav = Decimal(bundle["account_snapshot"]["nav"])
            self.assertLessEqual(at_reference, nav * Decimal("0.25"), order)  # 單次上限 NAV 的 25%（以參考價計，同 v4）
            self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.30"), order)
        self.assertLessEqual(Decimal(v4_body["order_proposal"]["turnover_rate"]), Decimal("0.30"))

    def test_a_listed_order_that_misses_an_eligible_holding_appends_it_in_the_v4_order(self):
        _, _, body = allocation_proposal("v5", ["2317.TW"])  # 2330 沒列到（正常流程不會發生，Finalizer 要求涵蓋全部）
        self.assertEqual(derisk_symbols(body)[0], "2317.TW")

    def test_a_non_derisk_day_is_bit_for_bit_the_same_as_v4(self):
        for stance in ("neutral", "aggressive"):
            _, _, control = allocation_proposal("v4", stance=stance)
            _, _, v5 = allocation_proposal("v5", ["2317.TW", "2330.TW"], stance=stance)
            self.assertEqual(v5["order_proposal"], control["order_proposal"], stance)
            self.assertEqual(v5["constraint_flags"], control["constraint_flags"], stance)
            self.assertEqual(skipped(v5), [], stance)

    def test_degraded_statuses_fall_back_to_the_v4_order_and_leave_a_flag(self):
        for status in ("exhausted", "unavailable"):
            _, _, body = allocation_proposal("v5", [], status=status)
            self.assertEqual(body["order_proposal"]["orders"], self.v4["order_proposal"]["orders"], status)
            self.assertEqual(derisk_symbols(body), self.atr_order, status)
            self.assertEqual(skipped(body), [("PORTFOLIO", "DERISK_ORDER_SKIPPED:order_%s" % status)], status)
            # 其餘旗標與 v4 相同（退回的是確定性順序，不拖垮整天）；ATR 文字也與 v4 相同。
            others = [f for f in body["constraint_flags"] if not f["code"].startswith("DERISK_ORDER_SKIPPED")]
            self.assertEqual(others, self.v4["constraint_flags"], status)

    def test_the_fallback_flag_only_appears_when_the_derisk_actually_sells(self):
        for stance in ("neutral", "aggressive"):
            _, _, body = allocation_proposal("v5", [], status="exhausted", stance=stance)
            self.assertEqual(skipped(body), [], stance)
        # 現金充足的防守日：沒有缺口就沒有減碼、也沒有旗標。
        _, _, body = allocation_proposal("v5", [], status="unavailable", cash="400000", shares_2317=3000)
        self.assertEqual((derisk_symbols(body), skipped(body)), ([], []))

    def test_not_needed_uses_the_v4_order_without_a_flag(self):
        _, _, body = allocation_proposal("v5", [], status="not_needed")
        self.assertEqual(body["order_proposal"]["orders"], self.v4["order_proposal"]["orders"])
        self.assertEqual(skipped(body), [])

    def test_the_marker_flag_is_not_a_derisk_symbol(self):
        """退回旗標是 PORTFOLIO 層級：不進減碼持股名單（v3 的加碼排除）、也不單獨構成減碼日。"""
        _, _, body = allocation_proposal("v5", [], status="exhausted")
        self.assertNotIn("PORTFOLIO", derisk_symbols(body))
        self.assertEqual([f["symbol"] for f in body["constraint_flags"] if f["code"] == "RISK_DRAWDOWN"], self.atr_order)

    def test_an_unbound_v5_policy_is_refused_by_the_engine(self):
        bundle, policy, _ = allocation_proposal("v5", ["2330.TW", "2317.TW"])
        unbound = copy.deepcopy(policy)
        for field in ("release_symbols", "release_ref"):
            unbound["position_sizing"]["drawdown_derisk"].pop(field)
        unbound["content_sha256"] = decision_policy_sha256(unbound)
        _, intent, _, _ = sized_inputs("medium", "low", "defensive")
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定釋出順序"):
            AllocationOrderEngine(bundle, unbound).run(self.intent_for(bundle))

    @staticmethod
    def intent_for(bundle):
        _, intent, _, _ = sized_inputs("medium", "low", "defensive")
        items = [dict(item, intent="hold", conviction=None, rejected_claim_ids=[],
                      adopted_claim_ids=["bull-2317-1"] if item["symbol"] == "2317.TW" else ["bull-2330-1"]) for item in intent["items"]]
        return reseal(bundle, intent, items)


class ReplayTests(unittest.TestCase):
    """回放沒有封存 run 的釋出順序：v5 樣板回放成 v4（拿掉綁定欄位、notes 標記），其餘 notes 與 policy 逐鍵不變。"""

    def test_a_v5_template_is_replayed_as_v4(self):
        from etf_agent.evaluation.replay import bind_replay_policy
        from test_cash_ratio_range import STANCE as LEGACY_STANCE, chain_inputs
        from test_daily_pipeline import live_without_offense

        bundle, _, debate, intent, archived, _, _, _ = chain_inputs()

        def replay(version):
            template = live_without_offense()
            template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
            for gate in ("agent_swap", "exposure_view"):
                template["position_sizing"].pop(gate, None)
            template["position_sizing"]["drawdown_derisk"]["version"] = version
            return bind_replay_policy(template, bundle, archived, intent, debate, dict(LEGACY_STANCE), [])

        v4_policy, v4_notes = replay("v4")
        v5_policy, v5_notes = replay("v5")
        self.assertEqual(v5_notes["drawdown_derisk_v5"], "replayed_as_v4")
        self.assertEqual({k: v for k, v in v5_notes.items() if k != "drawdown_derisk_v5"}, v4_notes)
        self.assertNotIn("drawdown_derisk_v5", v4_notes)
        self.assertEqual(v5_policy["position_sizing"]["drawdown_derisk"], v4_policy["position_sizing"]["drawdown_derisk"])
        self.assertEqual(v5_policy["content_sha256"], v4_policy["content_sha256"])


if __name__ == "__main__":
    unittest.main()
