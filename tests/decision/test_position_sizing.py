import copy
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionPolicyValidator,
    DecisionResultValidator,
    DecisionToolError,
    ProposalValidator,
    RevisionHistoryBuilder,
    ScenarioEngine,
    apply_trade_decision,
    build_decision_policy,
    decision_bundle_sha256,
    decision_policy_sha256,
    decision_rules_sha256,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.contracts import DecisionContext
from etf_agent.decision.sizing import (
    HELD_ADD_PRIORITY,
    ROTATION_VERSION,
    SELECTION_TIEBREAKS,
    SIZING_METHOD_V2,
    conviction_weights,
    validate_cash_stance,
    validate_position_sizing,
)

from test_portfolio_risk_decision import chain_inputs, policy as base_policy, risk_review, valid_inputs


SIZING = {
    "method": "conviction_volatility_v1",
    "conviction_multipliers": {"high": "1.5", "medium": "1", "low": "0.5"},
    "volatility_floor": "0.01",
    "cash_buffer_by_stance": {"aggressive": "0.03", "neutral": "0.10", "defensive": "0.20"},
}


def sized_inputs(tier_2317="high", tier_2330="low", stance="neutral", chain_bundle=None):
    """(bundle, intent, cash_stance, policy)：2317 買進、2330 加碼的交易決策，policy 為尚未綁定的基礎 policy。"""
    from test_trader import items as trade_items  # 延後匯入：test_trader 也匯入本模組的 SIZING

    bundle, _, debate, _, _, _, _, _ = chain_inputs(chain_bundle)
    decisions = trade_items()
    decisions[0] = dict(decisions[0], conviction=tier_2317)
    decisions[1] = dict(
        decisions[1], intent="add", conviction=tier_2330,
        adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1"],
    )
    intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), decisions)
    cash_stance = {"level": stance, "rationale": "市場廣度中性。", "evidence_ids": ["price-2330"]}
    policy = base_policy()
    policy["position_sizing"] = copy.deepcopy(SIZING)
    policy["content_sha256"] = decision_policy_sha256(policy)
    return bundle, intent, cash_stance, policy


def sized_policy(policy, intent, cash_stance, bundle):
    result = apply_trade_decision(policy, intent, cash_stance, bundle)
    result["content_sha256"] = decision_policy_sha256(result)
    return result


class CashStanceValidatorTests(unittest.TestCase):
    def test_cash_stance_requires_known_level_and_existing_evidence(self):
        bundle, _, stance, _ = sized_inputs()
        context = DecisionContext(bundle)
        ok = []
        validate_cash_stance(context, stance, "cash_stance", ok)
        self.assertEqual(ok, [])
        cases = (
            (dict(stance, level="all_in"), "cash_stance.level"),
            (dict(stance, evidence_ids=["price-9999"]), "引用不存在"),
            (dict(stance, evidence_ids=[]), "evidence_ids 不得為空"),
            (dict(stance, weight="0.3"), "未允許欄位"),
            (None, "cash_stance 必須是物件"),
        )
        for case, message in cases:
            errors = []
            validate_cash_stance(context, case, "cash_stance", errors)
            self.assertTrue(any(message in error for error in errors), (message, errors))


class ConvictionAllocationTests(unittest.TestCase):
    def test_water_filling_caps_and_redistributes_budget(self):
        weights = conviction_weights(
            ["A", "B", "C"],
            {"A": "high", "B": "medium", "C": "low"},
            {"high": Decimal("1.5"), "medium": Decimal("1"), "low": Decimal("0.5")},
            {"A": Decimal("0.01"), "B": Decimal("0.02"), "C": Decimal("0.02")},
            Decimal("0.01"),
            {"A": Decimal("0.3"), "B": Decimal("0.3"), "C": Decimal("0.3")},
            Decimal("0.8"),
        )
        self.assertEqual(weights["A"], Decimal("0.3"))
        self.assertEqual(weights["B"], Decimal("0.3"))
        self.assertEqual(weights["C"], Decimal("0.2"))

    def test_sized_proposal_follows_tiers_and_rebuilds(self):
        bundle, intent, stance, policy = sized_inputs("high", "low")
        policy = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        proposal = AllocationOrderEngine(bundle, policy).run(intent)

        targets = {key: Decimal(value) for key, value in proposal["position_sizing"]["target_weights"].items()}
        self.assertGreater(targets["2317.TW"], targets["2330.TW"])
        self.assertLessEqual(sum(targets.values()), Decimal("0.95"))
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(proposal), [])

        _, flipped_intent, flipped_stance, flipped_policy = sized_inputs("low", "high")
        flipped = AllocationOrderEngine(
            bundle, sized_policy(flipped_policy, flipped_intent, flipped_stance, bundle)
        ).run(flipped_intent)
        flipped_targets = {key: Decimal(value) for key, value in flipped["position_sizing"]["target_weights"].items()}
        self.assertLess(flipped_targets["2317.TW"], targets["2317.TW"])

    def test_sized_policy_replays_through_guard_history_and_finalizer(self):
        bundle, momentum, debate, intent, settings, team, _, _ = chain_inputs()
        proposal = AllocationOrderEngine(bundle, settings).run(intent)
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), ["2317.TW"])
        scenario = ScenarioEngine(bundle, settings).run(proposal)
        guard = CompetitionGuardV2(bundle, settings).run(proposal, scenario)
        self.assertTrue(guard["passed"])
        review = risk_review(bundle, proposal, scenario, guard)
        history = RevisionHistoryBuilder(bundle, settings, intent).create(proposal, scenario, guard, review)
        result = DecisionFinalizer(bundle, settings, momentum, debate, intent, team).run(
            proposal, scenario, guard, review, history
        )
        self.assertEqual(result["status"], "approved", result["errors"])
        self.assertEqual(
            DecisionResultValidator(bundle, settings, momentum, debate, intent, team).validate(
                proposal, scenario, guard, review, history, result
            ),
            [],
        )

    def test_agent_cash_stance_sets_buffer_and_invested_budget(self):
        bundle, intent, stance, policy = sized_inputs(stance="aggressive")
        aggressive = sized_policy(policy, intent, stance, bundle)
        self.assertEqual(aggressive["cash_buffer_rate"], "0.03")
        self.assertEqual(aggressive["position_sizing"]["cash_stance"], "aggressive")
        _, _, defensive_stance, defensive_base = sized_inputs(stance="defensive")
        defensive = sized_policy(defensive_base, intent, defensive_stance, bundle)
        self.assertEqual(defensive["cash_buffer_rate"], "0.20")

        def invested(settings):
            proposal = AllocationOrderEngine(bundle, settings).run(intent)
            return sum(Decimal(value) for value in proposal["position_sizing"]["target_weights"].values())

        self.assertGreater(invested(aggressive), invested(defensive))
        self.assertEqual(DecisionPolicyValidator(bundle).validate(defensive), [])

    def test_policy_rejects_cash_buffer_not_matching_stance_or_above_ceiling(self):
        bundle, intent, stance, policy = sized_inputs()
        tampered = sized_policy(policy, intent, stance, bundle)
        tampered["cash_buffer_rate"] = "0.01"
        tampered["content_sha256"] = decision_policy_sha256(tampered)
        errors = DecisionPolicyValidator(bundle).validate(tampered)
        self.assertTrue(any("cash_stance 對應" in error for error in errors), errors)

        policy["position_sizing"]["cash_buffer_by_stance"]["defensive"] = policy["cash_weight_ceiling"]
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("低於 cash_weight_ceiling" in error for error in errors), errors)

    def test_sizing_enabled_without_trade_decision_binding_fails_closed(self):
        bundle, intent, _, policy = sized_inputs()
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定交易決策"):
            AllocationOrderEngine(bundle, policy).run(intent)

    def test_policy_rejects_invalid_sizing_config(self):
        bundle, intent, stance, policy = sized_inputs()
        policy["position_sizing"]["conviction_multipliers"]["low"] = "2"
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("high ≥ medium ≥ low" in error for error in errors))
        with self.assertRaisesRegex(DecisionToolError, "尚未綁定的基礎 policy"):
            apply_trade_decision(sized_policy(sized_inputs()[3], intent, stance, bundle), intent, stance, bundle)

    def test_stock_weight_fit_always_runs_regardless_of_policy_version_field(self):
        bundle, intent, stance, policy = sized_inputs()
        legacy = sized_policy(policy, intent, stance, bundle)
        self.assertNotIn("stock_weight_fit", legacy["position_sizing"])
        self.assertEqual(DecisionPolicyValidator(bundle).validate(legacy), [])
        fit = AllocationOrderEngine._fit_stock_weights

        with patch.object(AllocationOrderEngine, "_fit_stock_weights", autospec=True, side_effect=fit) as spy:
            AllocationOrderEngine(bundle, legacy).run(intent)
        spy.assert_called_once()  # 單股減碼不再依 Policy 版本欄位開關

        current = copy.deepcopy(policy)
        current["position_sizing"]["stock_weight_fit"] = "v1"
        current["content_sha256"] = decision_policy_sha256(current)
        current = sized_policy(current, intent, stance, bundle)
        self.assertEqual(DecisionPolicyValidator(bundle).validate(current), [])
        with patch.object(AllocationOrderEngine, "_fit_stock_weights", autospec=True, side_effect=fit) as spy:
            AllocationOrderEngine(bundle, current).run(intent)
        spy.assert_called_once()

    def test_policy_rejects_unknown_stock_weight_fit_version(self):
        bundle, _, _, policy = sized_inputs()
        policy["position_sizing"]["stock_weight_fit"] = "v0"
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("stock_weight_fit" in error for error in errors))


class DecisionPolicyBuilderTests(unittest.TestCase):
    def template(self):
        policy = base_policy()
        for field in (
            "lot_size", "commission_rate", "sell_tax_rate", "minimum_commission",
            "max_stock_weight", "special_weight_limits", "max_sector_weight",
            "min_positions", "max_positions", "cash_weight_ceiling",
            "minimum_active_share", "reuse_sell_proceeds", "sector_classification_version",
            "sector_by_symbol", "content_sha256",
        ):
            policy.pop(field)
        return policy

    def sectors(self, available_at="2026-09-19T00:00:00+00:00"):
        return {
            "version": "sector:fixture",
            "available_at": available_at,
            "sector_by_symbol": {"2330.TW": "industry:24", "2317.TW": "industry:31"},
        }

    def test_copies_hard_rules_and_sector_map(self):
        bundle = valid_inputs()[0]
        policy = build_decision_policy(self.template(), bundle, self.sectors())
        self.assertEqual(policy["max_stock_weight"], bundle["rules"]["max_stock_weight"])
        self.assertEqual(policy["cash_weight_ceiling"], bundle["rules"]["cash_weight_must_be_below"])
        self.assertEqual(policy["sector_by_symbol"]["2330.TW"], "industry:24")
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])

    def test_rejects_template_hard_fields_future_or_incomplete_sectors(self):
        bundle = valid_inputs()[0]
        template = self.template()
        template["max_stock_weight"] = "0.9"
        with self.assertRaisesRegex(DecisionToolError, "硬性規則"):
            build_decision_policy(template, bundle, self.sectors())
        with self.assertRaisesRegex(DecisionToolError, "晚於 decision_cutoff"):
            build_decision_policy(self.template(), bundle, self.sectors("2026-09-30T00:00:00+00:00"))
        partial = self.sectors()
        partial["sector_by_symbol"].pop("2317.TW")
        with self.assertRaisesRegex(DecisionToolError, "未覆蓋"):
            build_decision_policy(self.template(), bundle, partial)


class PolicyBuilderCliTests(unittest.TestCase):
    def test_cli_build_policy_produces_valid_base_policy(self):
        bundle = valid_inputs()[0]
        root = Path(__file__).resolve().parents[2]
        template = json.loads((root / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        # 內附樣板的時間晚於 fixture cutoff；只替換時間以驗證其餘欄位可直接使用。
        template["available_at"] = "2026-09-19T00:00:00+00:00"
        sectors = DecisionPolicyBuilderTests().sectors()
        with tempfile.TemporaryDirectory() as directory:
            paths = {name: Path(directory) / ("%s.json" % name) for name in ("bundle", "template", "sectors")}
            for name, payload in (("bundle", bundle), ("template", template), ("sectors", sectors)):
                paths[name].write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            output = Path(directory) / "policy.json"
            completed = subprocess.run(
                [
                    sys.executable, str(root / "cli" / "portfolio_decision.py"), "--bundle", str(paths["bundle"]),
                    "build-policy", "--template", str(paths["template"]), "--sector", str(paths["sectors"]),
                    "--output", str(output),
                ],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            policy = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])


class FullPortfolioSlotTests(unittest.TestCase):
    def full_portfolio_proposal(self, tier_new, tier_held, held_add_priority=True):
        # 2330 已持有（唯一一檔），max_positions=1 即滿檔；2317 是想新買的股票。
        from test_team_chain import tradable_bundle  # 延後匯入：test_team_chain 也匯入本模組的 SIZING

        full = tradable_bundle()
        full["rules"]["max_positions"] = 1
        full["rules"]["min_positions"] = 1
        full["rules"]["config_sha256"] = decision_rules_sha256(full["rules"])
        full["bundle_sha256"] = decision_bundle_sha256(full)
        bundle, intent, stance, policy = sized_inputs(tier_new, tier_held, chain_bundle=full)
        policy["max_positions"] = 1
        policy["min_positions"] = 1
        if held_add_priority:
            policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        return AllocationOrderEngine(bundle, policy).run(intent)

    def test_new_buy_does_not_take_slot_from_held_add_when_full(self):
        # 新股信心高於持股加碼：舊邏輯會把唯一名額給買不進去的 2317，讓 2330 沒有目標權重。
        proposal = self.full_portfolio_proposal("high", "low")
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), ["2330.TW"])
        flagged = {flag["symbol"] for flag in proposal["constraint_flags"] if flag["code"] == "MAX_POSITIONS"}
        self.assertEqual(flagged, {"2317.TW"})
        self.assertNotIn("2317.TW", {order["symbol"] for order in proposal["order_proposal"]["orders"]})

    def test_policy_without_held_add_priority_keeps_old_slot_order(self):
        # 沒有 held_add_priority 的舊 Decision run：名額仍給排名較前的新股，重建結果必須與當時相同。
        proposal = self.full_portfolio_proposal("high", "low", held_add_priority=False)
        self.assertEqual(list(proposal["position_sizing"]["target_weights"]), ["2317.TW"])

    def test_unknown_held_add_priority_version_is_rejected(self):
        bundle, intent, stance, policy = sized_inputs()
        policy["position_sizing"]["held_add_priority"] = "v2"
        errors: list = []
        validate_position_sizing(policy, sorted(DecisionContext(bundle).universe), errors)
        self.assertTrue(any("held_add_priority" in error for error in errors), errors)

    def test_policy_template_enables_held_add_priority(self):
        # 正式樣板必須啟用，否則新決策會悄悄退回舊的名額分配。
        root = Path(__file__).resolve().parents[2]
        template = json.loads((root / "config" / "decision_policy.json").read_text(encoding="utf-8"))
        self.assertEqual(template["position_sizing"].get("held_add_priority"), HELD_ADD_PRIORITY)


class RotationSlotTests(unittest.TestCase):
    """滿檔換股名額：growth（廣度之下的真正空位）與 swap（本次出場釋出）由程式配對。"""

    EFFECTIVE = {"cash_buffer_rate": Decimal("0.1"), "max_stock_weight": Decimal("0.5")}

    def engine(self, tier_new="high", max_swaps=3, min_new="medium", rotation=True, hard_max=1):
        # 2330 已持有（唯一一檔）；hard_max=1 代表滿檔，2317 是想新買的股票。
        from test_team_chain import tradable_bundle  # 延後匯入：test_team_chain 也匯入本模組的 SIZING

        full = tradable_bundle()
        full["rules"]["max_positions"] = hard_max
        full["rules"]["min_positions"] = 1
        full["rules"]["config_sha256"] = decision_rules_sha256(full["rules"])
        full["bundle_sha256"] = decision_bundle_sha256(full)
        bundle, intent, stance, policy = sized_inputs(tier_new, "low", chain_bundle=full)
        policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        if rotation:
            policy["position_sizing"]["rotation"] = {
                "version": ROTATION_VERSION,
                "max_swaps_per_run": max_swaps,
                "min_new_conviction": min_new,
            }
        policy["max_positions"] = hard_max
        policy["min_positions"] = 1
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        return AllocationOrderEngine(bundle, policy)

    def targets(self, engine, active, candidates=("2317.TW", "2330.TW"), held_count=1, breadth=None):
        """直接檢驗名額配對；active 為本次出場後仍持有的股票，breadth 為風險 Agent 的廣度目標。"""
        if breadth is not None:
            engine.policy["position_sizing"].update(
                positions_by_breadth={"concentrated": breadth}, position_breadth="concentrated"
            )
        return engine._conviction_targets(
            list(candidates), set(active), {symbol: 1000 for symbol in active}, Decimal("1000000"),
            int(engine.policy["max_positions"]), self.EFFECTIVE, held_count,
        )

    def test_full_without_exit_keeps_new_symbol_out(self):
        # 沒有出場 → 沒有釋出名額，新股不得擠掉已持有的加碼（#107 的前提不變）。
        _, weights, skipped = self.targets(self.engine(), active=("2330.TW",))
        self.assertEqual(sorted(weights), ["2330.TW"])
        self.assertEqual(skipped, {"2317.TW": "MAX_POSITIONS"})

    def test_exit_releases_slot_for_new_symbol(self):
        # 2330 本次出場 → 釋出 1 個名額，信心足夠的 2317 換進來。
        _, weights, skipped = self.targets(self.engine(), active=(), candidates=("2317.TW",))
        self.assertEqual(sorted(weights), ["2317.TW"])
        self.assertEqual(skipped, {})

    def test_released_slot_requires_minimum_conviction(self):
        # 換股名額只給信心不低於 min_new_conviction 的新股；low < medium 應被擋下。
        _, weights, skipped = self.targets(
            self.engine(tier_new="low"), active=(), candidates=("2317.TW",)
        )
        self.assertNotIn("2317.TW", weights)
        self.assertEqual(skipped, {"2317.TW": "ROTATION_CONVICTION"})

    def test_max_swaps_zero_blocks_the_released_slot(self):
        _, weights, skipped = self.targets(
            self.engine(max_swaps=0), active=(), candidates=("2317.TW",)
        )
        self.assertNotIn("2317.TW", weights)
        self.assertEqual(skipped, {"2317.TW": "MAX_POSITIONS"})

    def test_old_path_already_rotates_on_exit(self):
        """沒有 rotation 的舊路徑在出場後就已經讓新股進來；rotation 只是加上換股上限與信心門檻。"""
        _, weights, skipped = self.targets(
            self.engine(rotation=False), active=(), candidates=("2317.TW",)
        )
        self.assertEqual(sorted(weights), ["2317.TW"])
        self.assertEqual(skipped, {})

    def test_old_path_allows_weak_new_symbol_into_released_slot(self):
        """舊路徑沒有信心門檻，low 信心的新股也會用掉釋出的名額；rotation 才擋得住。"""
        _, weights, _ = self.targets(
            self.engine(tier_new="low", rotation=False), active=(), candidates=("2317.TW",)
        )
        self.assertEqual(sorted(weights), ["2317.TW"])

    # 廣度收窄的凍結：持有 2 檔（2330、2317），只有 2330 是本次 add 候選，廣度目標收到 1 檔。
    NARROW = {"active": ("2330.TW", "2317.TW"), "candidates": ("2330.TW",), "breadth": 1, "held_count": 2}

    def test_breadth_below_held_count_freezes_adds_on_old_path(self):
        """廣度目標低於目前持股數時，舊路徑的 slots 變成 0／負數，連既有持股的 add 都被凍住。"""
        _, weights, _ = self.targets(self.engine(rotation=False, hard_max=5), **self.NARROW)
        self.assertEqual(sorted(weights), [])

    def test_rotation_keeps_adds_alive_when_breadth_narrows(self):
        """rotation 的 cap 以持股數為底：廣度收窄只停止淨增加，不凍結既有持股的加碼。"""
        _, weights, skipped = self.targets(self.engine(hard_max=5), **self.NARROW)
        self.assertEqual(sorted(weights), ["2330.TW"])
        self.assertEqual(skipped, {})


class RotationEndToEndTests(unittest.TestCase):
    """完整走 AllocationOrderEngine.run 與 ProposalValidator：2330 出場釋出滿檔的唯一名額。"""

    def proposal(self, tier_new="high", min_new="medium", rotation=True):
        from test_team_chain import tradable_bundle  # 延後匯入：test_team_chain 也匯入本模組的 SIZING
        from test_trader import items as trade_items

        full = tradable_bundle()
        full["rules"]["max_positions"] = 1
        full["rules"]["min_positions"] = 1
        full["rules"]["config_sha256"] = decision_rules_sha256(full["rules"])
        full["bundle_sha256"] = decision_bundle_sha256(full)
        bundle, _, debate, _, _, _, _, _ = chain_inputs(full)
        decisions = trade_items()
        decisions[0] = dict(decisions[0], conviction=tier_new)
        # 出場必須採納至少一個空頭 claim（trader.py 的 REDUCING 規則）。
        decisions[1] = dict(
            decisions[1], intent="exit",
            adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"],
        )
        intent = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), decisions)
        stance = {"level": "neutral", "rationale": "市場廣度中性。", "evidence_ids": ["price-2330"]}
        policy = base_policy()
        policy["position_sizing"] = copy.deepcopy(SIZING)
        policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        if rotation:
            policy["position_sizing"]["rotation"] = {
                "version": ROTATION_VERSION, "max_swaps_per_run": 3, "min_new_conviction": min_new,
            }
        policy["max_positions"] = 1
        policy["min_positions"] = 1
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return body

    def flags(self, body):
        return {flag["symbol"]: flag["code"] for flag in body["constraint_flags"]}

    def test_high_conviction_new_symbol_takes_the_released_slot(self):
        body = self.proposal()
        sides = {order["symbol"]: order["side"] for order in body["order_proposal"]["orders"]}
        self.assertEqual(sides.get("2317.TW"), "buy")
        self.assertEqual(sides.get("2330.TW"), "sell")
        self.assertEqual(list(body["position_sizing"]["target_weights"]), ["2317.TW"])

    def test_conviction_floor_blocks_swap_and_min_positions_keeps_the_exit(self):
        """low 信心被門檻擋下 → 持股掉到 0 低於 min_positions → 程式留住 2330 續抱。"""
        body = self.proposal(tier_new="low")
        flags = self.flags(body)
        self.assertEqual(flags.get("2330.TW"), "RISK_MIN_POSITIONS")
        self.assertEqual(flags.get("2317.TW"), "MAX_POSITIONS")
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertEqual(list(body["position_sizing"]["target_weights"]), [])

    def test_without_rotation_low_conviction_new_symbol_still_gets_the_slot(self):
        """對照組：舊路徑沒有信心門檻，low 信心也拿到名額（rotation 才是更嚴格的那一邊）。"""
        body = self.proposal(tier_new="low", rotation=False)
        self.assertEqual(list(body["position_sizing"]["target_weights"]), ["2317.TW"])


class VolatilityExponentTests(unittest.TestCase):
    """v2 的波動度指數：1 等同 v1、0 完全不看波動度；v1 路徑不得改變。"""

    ORDER = ["A", "B", "C"]
    TIERS = {"A": "high", "B": "medium", "C": "medium"}
    MULTIPLIERS = {"high": Decimal("1.5"), "medium": Decimal("1"), "low": Decimal("0.5")}
    VOL = {"A": Decimal("0.02"), "B": Decimal("0.02"), "C": Decimal("0.08")}
    LIMITS = {symbol: Decimal("1") for symbol in ORDER}

    def weights(self, exponent=None):
        return conviction_weights(
            self.ORDER, self.TIERS, self.MULTIPLIERS, self.VOL,
            Decimal("0.01"), self.LIMITS, Decimal("0.9"), exponent,
        )

    def test_exponent_one_matches_v1_exactly(self):
        self.assertEqual(self.weights(Decimal("1")), self.weights(None))

    # Decimal 以 28 位有效位數運算，比例分配會留下尾差；比較權重時給極小容差。
    TOLERANCE = Decimal("1e-25")

    def assertClose(self, left, right, msg=None):
        self.assertLessEqual(abs(left - right), self.TOLERANCE, msg or (left, right))

    def test_exponent_zero_ignores_volatility(self):
        weights = self.weights(Decimal("0"))
        # B 與 C 同為 medium，波動度差 4 倍，指數 0 時權重必須相同。
        self.assertEqual(weights["B"], weights["C"])
        # 等級乘數仍然有效：high 是 medium 的 1.5 倍。
        self.assertClose(weights["A"], weights["B"] * Decimal("1.5"))

    def test_exponent_half_sits_between(self):
        ratio_at = {
            exponent: self.weights(Decimal(exponent))["B"] / self.weights(Decimal(exponent))["C"]
            for exponent in ("0", "0.5", "1")
        }
        self.assertEqual(ratio_at["0"], Decimal("1"))
        self.assertTrue(ratio_at["0"] < ratio_at["0.5"] < ratio_at["1"], ratio_at)

    def test_budget_is_still_fully_allocated(self):
        for exponent in (None, Decimal("0"), Decimal("0.5"), Decimal("1")):
            total = sum(self.weights(exponent).values())
            self.assertClose(total, Decimal("0.9"), "exponent=%s 的總權重為 %s" % (exponent, total))


class SizingMethodValidationTests(unittest.TestCase):
    def errors_for(self, **overrides):
        bundle, intent, stance, policy = sized_inputs()
        policy["position_sizing"].update(overrides)
        errors: list = []
        validate_position_sizing(policy, sorted(DecisionContext(bundle).universe), errors)
        return errors

    def test_v2_with_exponent_passes(self):
        self.assertEqual(
            self.errors_for(method=SIZING_METHOD_V2, volatility_exponent="0"), []
        )

    def test_v2_without_exponent_is_rejected(self):
        errors = self.errors_for(method=SIZING_METHOD_V2)
        self.assertTrue(any("volatility_exponent 是" in error for error in errors), errors)

    def test_v1_with_exponent_is_rejected(self):
        errors = self.errors_for(volatility_exponent="0")
        self.assertTrue(any("只允許用於" in error for error in errors), errors)

    def test_exponent_out_of_range_is_rejected(self):
        for bad in ("-0.1", "1.1"):
            errors = self.errors_for(method=SIZING_METHOD_V2, volatility_exponent=bad)
            self.assertTrue(any("介於 0 與 1" in error for error in errors), (bad, errors))

    def test_unknown_method_is_rejected(self):
        errors = self.errors_for(method="conviction_volatility_v3")
        self.assertTrue(any("method 必須為" in error for error in errors), errors)

    def test_unknown_tiebreak_is_rejected(self):
        errors = self.errors_for(selection_tiebreak="random")
        self.assertTrue(any("selection_tiebreak" in error for error in errors), errors)

    def test_known_tiebreaks_pass(self):
        for value in SELECTION_TIEBREAKS:
            self.assertEqual(self.errors_for(selection_tiebreak=value), [], value)


class SelectionTiebreakTests(unittest.TestCase):
    """同等級同支持數的入選順序；省略欄位必須與舊行為相同（ATR 低者優先）。"""

    def ordered(self, tiebreak=None):
        bundle, intent, stance, policy = sized_inputs("medium", "medium")
        policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        if tiebreak is not None:
            policy["position_sizing"]["selection_tiebreak"] = tiebreak
        policy["content_sha256"] = decision_policy_sha256(policy)
        policy = sized_policy(policy, intent, stance, bundle)
        engine = AllocationOrderEngine(bundle, policy)
        # 兩檔同等級、支持數相同，第三順位的 tiebreak 才會決定順序。
        engine.policy["position_sizing"].update(
            conviction_by_symbol={"2317.TW": "medium", "2330.TW": "medium"},
            support_by_symbol={"2317.TW": 0, "2330.TW": 0},
            positions_by_breadth={"concentrated": 2}, position_breadth="concentrated",
        )
        order, _, _ = engine._conviction_targets(
            ["2317.TW", "2330.TW"], set(), {}, Decimal("1000000"),
            int(engine.policy["max_positions"]),
            {"cash_buffer_rate": Decimal("0.1"), "max_stock_weight": Decimal("0.5")}, 0,
        )
        return order, engine

    def test_default_matches_atr_low(self):
        default_order, engine = self.ordered()
        atr = engine._atr_pct()
        self.assertEqual(default_order, sorted(default_order, key=lambda s: atr[s]))
        self.assertEqual(default_order, self.ordered("atr_low")[0])

    def test_symbol_tiebreak_ignores_volatility(self):
        order, _ = self.ordered("symbol")
        self.assertEqual(order, sorted(order))

    def test_momentum_high_prefers_stronger_recent_return(self):
        order, engine = self.ordered("momentum_high")
        returns = engine._returns_20d()
        self.assertEqual(order, sorted(order, key=lambda s: -returns[s]))


class RotationPolicyValidationTests(unittest.TestCase):
    def errors_for(self, rotation, held_add_priority=True):
        bundle, intent, stance, policy = sized_inputs()
        if held_add_priority:
            policy["position_sizing"]["held_add_priority"] = HELD_ADD_PRIORITY
        policy["position_sizing"]["rotation"] = rotation
        errors: list = []
        validate_position_sizing(policy, sorted(DecisionContext(bundle).universe), errors)
        return errors

    def valid(self):
        return {"version": ROTATION_VERSION, "max_swaps_per_run": 3, "min_new_conviction": "medium"}

    def test_valid_rotation_passes(self):
        self.assertEqual(self.errors_for(self.valid()), [])

    def test_rotation_requires_held_add_priority(self):
        errors = self.errors_for(self.valid(), held_add_priority=False)
        self.assertTrue(any("held_add_priority 同時存在" in error for error in errors), errors)

    def test_unknown_version_is_rejected(self):
        errors = self.errors_for(dict(self.valid(), version="v2"))
        self.assertTrue(any("rotation.version" in error for error in errors), errors)

    def test_negative_or_boolean_swaps_are_rejected(self):
        for bad in (-1, True, "3", None):
            errors = self.errors_for(dict(self.valid(), max_swaps_per_run=bad))
            self.assertTrue(any("max_swaps_per_run" in error for error in errors), (bad, errors))

    def test_unknown_conviction_floor_is_rejected(self):
        errors = self.errors_for(dict(self.valid(), min_new_conviction="none"))
        self.assertTrue(any("min_new_conviction" in error for error in errors), errors)

    def test_unknown_field_is_rejected(self):
        errors = self.errors_for(dict(self.valid(), extra="x"))
        self.assertTrue(any("rotation" in error for error in errors), errors)

    def test_non_mapping_is_rejected(self):
        errors = self.errors_for("v1")
        self.assertTrue(any("rotation 必須是物件" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
