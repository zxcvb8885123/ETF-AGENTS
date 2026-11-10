"""position_sizing 版本閘門前提表（``sizing.GATE_PREREQUISITES``）與閘門交互作用的測試（審查 G-2）。

- 前提表是程式內單一來源：白名單內每個閘門都在表中、前提／互斥／版本要求逐條由 ``validate_position_sizing`` 強制，
  只由人設定的閘門不是策略反思旋鈕。
- 審查報告列出的未測交互各有一組 end-to-end 測試：drawdown_derisk v4 × turnover_fit × RISK_TURNOVER、
  high_conviction_cash_priority × limit_scaled_conviction × cash_ratio_range、forced_swap.confirmation × 減碼日 ×
  cash_buffer_floor、cash_ceiling_fix × reuse_sell_proceeds=false；「舊 run 重建」的自動回歸見 test_archived_runs_replay。
- 「現行 config 全開」組合：live 樣板跑完整配置、情境與 Guard，並以固定快照釘住 proposal 雜湊。

fixture 只有兩檔（2317／2330）時部分交互看不到，所以另建三檔世界（加上 1101.TW）。
"""

import copy
import unittest
from decimal import Decimal

from etf_agent.automation.daily_pipeline import degraded_research_result
from etf_agent.decision import (
    AllocationOrderEngine,
    DecisionPolicyValidator,
    MomentumEngine,
    ProposalValidator,
    bind_drawdown,
    build_research_debate,
    build_stance_brief,
    canonical_sha256,
    decision_bundle_sha256,
    decision_policy_sha256,
    decision_rules_sha256,
    seal_stance_packet,
    seal_trade_decision,
    sentiment_unavailable_report,
    trade_decision_envelope,
)
from etf_agent.decision.cash_ratio import account_cash_ratio
from etf_agent.decision.sizing import (
    CASH_FIT_MIN_FILL_RATIO,
    GATE_FIELDS,
    GATE_PREREQUISITES,
    POSITION_SIZING_FIELDS,
    SIZING_BINDING_FIELDS,
    SIZING_CORE_FIELDS,
    SIZING_METHOD,
    SIZING_METHOD_V2,
    GateRule,
    cash_fit_min_fill_ratio,
    validate_gate_prerequisites,
    validate_position_sizing,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES, KNOBS

from test_analysts import event_items, finding, report, technical_items
from test_budget_and_derisk_gates import reseal, two_positions
from test_cash_fit_and_limits import gated, live_production_policy, production_policy, with_rules
from test_portfolio_decision_agent import refresh_artifact_hash
from test_portfolio_risk_decision import chain_inputs, policy as base_policy, valid_inputs
from test_position_sizing import SIZING, sized_inputs, sized_policy
from test_risk_limit_gates import CONTROL, account, failed_rules, flags, guard, orders
from test_team_chain import tradable_bundle

PRICES = {"2330.TW": Decimal("164"), "2317.TW": Decimal("112")}
# 2026-11-08 的正式 position_sizing 快照（不讀 live 檔）：固定 fixture 下的 proposal 雜湊釘在 PRODUCTION_SNAPSHOT_SHA256。
PRODUCTION_SIZING_2026_11_08 = {
    "method": "conviction_volatility_v2",
    "conviction_multipliers": {"high": "1.5", "medium": "0.6", "low": "0.3"},
    "volatility_floor": "0.01",
    "volatility_exponent": "0",
    "stock_weight_fit": "v1",
    "selection_ranking": "v1",
    "held_add_priority": "v1",
    "cash_buffer_floor": "v1",
    "cash_fit_order": "v1",
    "held_weight_budget": "v1",
    "cash_fit_min_fill_ratio": "0.5",
    "high_conviction_cash_priority": "v1",
    "limit_scaled_conviction": {"version": "v1", "alpha": "0.6"},
    "cash_ratio_range": {
        "version": "v1",
        "grid": {
            "aggressive": {"lower": "0.03", "middle": "0.05", "upper": "0.08"},
            "neutral": {"lower": "0.08", "middle": "0.10", "upper": "0.13"},
            "defensive": {"lower": "0.15", "middle": "0.18", "upper": "0.21"},
        },
        "max_daily_raise": "0.03",
    },
    "cash_ceiling_fix": "v1",
    "stock_limit_headroom": "v1",
    "buy_gap_reserve": "v1",
    "buy_gap_reserve_rate": "0.03",
    "insufficient_funds_policy": "void_batch",
    "sector_fit": "v1",
    "turnover_fit": "v1",
    "reentry_cooldown": {"version": "v1", "days": 3},
    "drawdown_derisk": {"version": "v4", "max_derisk_fraction": "0.25"},
    "rotation": {
        "version": "v1", "max_swaps_per_run": 5, "min_new_conviction": "medium",
        "forced_swap": {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 2, "confirmation": "v1"},
    },
    "cash_buffer_by_stance": {"aggressive": "0.03", "neutral": "0.10", "defensive": "0.20"},
    "positions_by_breadth": {"concentrated": 22, "balanced": 26, "diversified": 30},
}
PRODUCTION_SNAPSHOT_SHA256 = "4e5b131dcde6a9e527a0bf105faf202cf6d8f44532ba5b12cddc5590c2ec476d"


def anchored_sizing(bundle, sizing=None, **overrides):
    """固定版本 position_sizing 加上 account_cash anchor（測試不經決策庫）與覆寫欄位。"""
    result = copy.deepcopy(sizing if sizing is not None else production_policy()["position_sizing"])
    result["cash_ratio_range"]["anchor"] = {"status": "account_cash", "ratio": account_cash_ratio(bundle), "source": None}
    result.update(overrides)
    return result


def stance(level="neutral", grade="middle", breadth="balanced"):
    return {"level": level, "breadth": breadth, "cash_grade": grade, "rationale": "市場廣度中性。", "evidence_ids": ["price-2330"]}


def sized(bundle, intent, stance_obj, sizing, policy=None, control=None, **policy_fields):
    """綁定 sizing 與回撤控制、驗證 Policy、跑配置並重建比對；回傳 (policy, body)。"""
    policy = copy.deepcopy(policy if policy is not None else base_policy())
    policy.update(policy_fields)
    policy["position_sizing"] = copy.deepcopy(sizing)
    if "drawdown_derisk" in sizing:
        policy["drawdown_control"] = dict(control or CONTROL)
        policy = bind_drawdown(policy, bundle, [])
    policy["content_sha256"] = decision_policy_sha256(policy)
    policy = sized_policy(policy, intent, stance_obj, bundle)
    errors = DecisionPolicyValidator(bundle).validate(policy)
    if errors:
        raise AssertionError(errors)
    body = AllocationOrderEngine(bundle, policy).run(intent)
    rebuild = ProposalValidator(bundle, policy, intent).validate(body)
    if rebuild:
        raise AssertionError(rebuild)
    return policy, body


def three_symbol_bundle(cash, positions, price_1101="100", spread="5", **rules):
    """三檔交易池：fixture 的 2317／2330 加上 1101.TW（價格 price_1101、日內振幅 ±spread 以控制 ATR）。"""
    bundle = tradable_bundle()
    snapshot = bundle["snapshot"]
    close = Decimal(price_1101)
    series = copy.deepcopy(bundle["price_series"][0])
    series.update(symbol="1101.TW", evidence_id="price-1101")
    for bar in series["bars"]:
        bar.update(open=str(close - 1), high=str(close + Decimal(spread)), low=str(close - Decimal(spread)), close=str(close))
    series["series_sha256"] = canonical_sha256(series["bars"])
    bundle["price_series"].append(series)
    snapshot["source_evidence"].append({
        "evidence_id": "price-1101", "source": "FIXTURE", "authority": "vendor", "data_type": "daily_price",
    })
    snapshot["latest_prices"].append({
        "symbol": "1101.TW", "trade_date": "2026-09-19", "analysis_close_price": price_1101,
        "close_price": price_1101, "source_evidence_id": "price-1101",
    })
    snapshot.update(universe_size=3, tradable_symbols=["1101.TW", "2317.TW", "2330.TW"])
    bundle["rules"].update(rules)
    bundle["rules"]["config_sha256"] = decision_rules_sha256(bundle["rules"])
    prices = {**PRICES, "1101.TW": close}
    account_snapshot = bundle["account_snapshot"]
    account_snapshot["cash"] = account_snapshot["settled_cash"] = str(cash)
    account_snapshot["positions"] = [
        {"symbol": symbol, "shares": shares, "average_cost": "100"} for symbol, shares in sorted(positions.items())
    ]
    account_snapshot["nav"] = str(Decimal(cash) + sum(Decimal(shares) * prices[symbol] for symbol, shares in positions.items()))
    bundle["snapshot_sha256"] = canonical_sha256(snapshot)
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def three_symbol_intent(bundle, decisions):
    """三檔多空辯論（2330 有空頭 claim，其餘只有多頭）與封存的交易決策。"""
    momentum = MomentumEngine(bundle).run()
    technical = [
        {"symbol": "1101.TW", "outlook": "positive", "findings": [finding("tech-1101-1", "price-1101")], "data_gaps": []},
        *technical_items(),
    ]
    unknown = [
        {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
        for symbol in ("1101.TW", "2317.TW", "2330.TW")
    ]
    events = [
        {"symbol": "1101.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_MATERIAL_EVENT_IN_WINDOW"], "events": []},
        *event_items(),
    ]
    reports = {
        "technical": report(bundle, "technical", technical),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": report(bundle, "event", events),
        "sentiment": sentiment_unavailable_report(bundle),
    }
    research = degraded_research_result(bundle["snapshot"], "research-1")

    def claim(claim_id, evidence):
        return {"claim_id": claim_id, "text": "依價格證據。", "evidence_ids": [evidence], "finding_ids": []}

    bull_items = [
        {"symbol": "1101.TW", "strength": "moderate", "claims": [claim("bull-1101-1", "price-1101")], "invalidation_conditions": []},
        {"symbol": "2317.TW", "strength": "moderate", "claims": [claim("bull-2317-1", "price-2317")], "invalidation_conditions": []},
        {"symbol": "2330.TW", "strength": "weak", "claims": [claim("bull-2330-1", "price-2330")], "invalidation_conditions": []},
    ]
    bear_items = [
        {"symbol": symbol, "strength": "none", "claims": [], "invalidation_conditions": []} for symbol in ("1101.TW", "2317.TW")
    ] + [{"symbol": "2330.TW", "strength": "moderate", "claims": [claim("bear-2330-1", "price-2330")], "invalidation_conditions": []}]
    bull = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"], bull_items)
    bear = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bear")["packet_envelope"], bear_items)
    debate = build_research_debate(bundle, bull, bear, "debate-3")
    return seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-3"), decisions)


def decision_item(symbol, intent, conviction=None, adopt=(), reject=()):
    return {
        "symbol": symbol, "intent": intent, "conviction": conviction, "rationale": "依辯論取捨。",
        "adopted_claim_ids": list(adopt), "rejected_claim_ids": list(reject),
        "unresolved_questions": [], "invalidation_conditions": [],
    }


def sizing_errors(sizing, universe=("2330.TW", "2317.TW")):
    policy = base_policy()
    policy["position_sizing"] = sizing
    errors = []
    validate_position_sizing(policy, list(universe), errors)
    return errors


def rule_for(field):
    return next(rule for rule in GATE_PREREQUISITES if rule.field == field)


def nested_set(sizing, path, value):
    node = sizing
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node[parts[-1]] = value


def nested_pop(sizing, path):
    node = sizing
    parts = path.split(".")
    for part in parts[:-1]:
        node = node[part]
    node.pop(parts[-1])


# ---------------------------------------------------------------- 前提表
class GateTableTests(unittest.TestCase):
    def setUp(self):
        self.production = copy.deepcopy(PRODUCTION_SIZING_2026_11_08)
        # 不綁定 anchor 的樣板可直接通過 validate_position_sizing（綁定交易決策時才要求 anchor）。
        self.live = copy.deepcopy(production_policy()["position_sizing"])

    def test_table_is_the_single_inventory_of_gate_fields(self):
        top_level = {field for field in GATE_FIELDS if "." not in field}
        self.assertEqual(top_level, set(POSITION_SIZING_FIELDS) - set(SIZING_CORE_FIELDS) - set(SIZING_BINDING_FIELDS))
        self.assertEqual(len({rule.field for rule in GATE_PREREQUISITES}), len(GATE_PREREQUISITES))  # 沒有重複條目
        for rule in GATE_PREREQUISITES:
            self.assertIsInstance(rule, GateRule)
            for target in rule.requires + rule.excludes + tuple(field for field, _ in rule.requires_values):
                self.assertIn(target, GATE_FIELDS | SIZING_CORE_FIELDS, (rule.field, target))
            if "." in rule.field:
                self.assertIn(rule.field.rsplit(".", 1)[0], GATE_FIELDS, rule.field)  # 巢狀閘門的上層也在表中
        # 現行 config 用到的每個閘門都在表中。
        self.assertTrue({field for field in self.live if field not in SIZING_CORE_FIELDS} <= GATE_FIELDS)

    def test_human_only_gates_are_not_reflection_knobs_and_knobs_are_declared(self):
        for rule in GATE_PREREQUISITES:
            knob_hits = [knob for knob in KNOBS if knob == rule.field or knob.startswith(rule.field + ".")]
            if rule.human_only:
                self.assertEqual(knob_hits, [], rule.field)
                protected = any(rule.field == prefix or rule.field.startswith(prefix) for prefix in FORBIDDEN_PREFIXES)
                # held_weight_budget 不在 FORBIDDEN_PREFIXES，但也不在 KNOBS：check_proposal 對不在白名單的旋鈕一律拒絕。
                self.assertTrue(protected or rule.field == "held_weight_budget", rule.field)
            else:
                self.assertTrue(knob_hits, rule.field)

    def test_production_snapshot_and_live_config_satisfy_every_rule(self):
        for sizing in (self.production, self.live):
            errors = []
            validate_gate_prerequisites(sizing, errors)
            self.assertEqual(errors, [])
            self.assertEqual(sizing_errors(sizing), [])

    def test_every_requirement_in_the_table_is_enforced(self):
        """逐條列舉：拿掉前提、改掉必要值、加上互斥欄位、把 drawdown_derisk 退回 v2，都必須被拒絕並點名該閘門。"""
        checked = 0
        for rule in GATE_PREREQUISITES:
            present = copy.deepcopy(self.production)
            if rule.field == "high_conviction_cash_floor":
                present.pop("cash_ratio_range")
                present["high_conviction_cash_floor"] = {"version": "v1", "floor_ratio": "0.75"}
            elif rule.field == "cash_grade":
                present["cash_grade"] = "middle"
            elif rule.field not in ("rotation.forced_swap", "rotation.forced_swap.confirmation", "rotation", "cash_ratio_range"):
                if rule.field not in present:
                    continue
            errors = []
            validate_gate_prerequisites(present, errors)
            self.assertEqual([e for e in errors if "position_sizing.%s" % rule.field in e], [], rule.field)
            for required in rule.requires:
                broken = copy.deepcopy(present)
                nested_pop(broken, required)
                errors = sizing_errors(broken) if rule.field != "cash_grade" else []
                if rule.field == "cash_grade":
                    validate_gate_prerequisites(broken, errors)
                self.assertTrue(any(rule.field in e and required in e for e in errors), (rule.field, required, errors))
                checked += 1
            for field, expected in rule.requires_values:
                broken = copy.deepcopy(present)
                broken[field] = SIZING_METHOD if expected == SIZING_METHOD_V2 else "unexpected"
                errors = []
                validate_gate_prerequisites(broken, errors)
                self.assertTrue(any(rule.field in e and str(expected) in e for e in errors), (rule.field, field, errors))
                checked += 1
            for other in rule.excludes:
                broken = copy.deepcopy(present)
                broken[other] = {"version": "v1", "floor_ratio": "0.75"} if other == "high_conviction_cash_floor" else "v1"
                errors = []
                validate_gate_prerequisites(broken, errors)
                self.assertTrue(any(rule.field in e and other in e and "同時啟用" in e for e in errors), (rule.field, other, errors))
                checked += 1
            if rule.derisk_versions:
                for version in ("v1", "v2"):
                    broken = copy.deepcopy(present)
                    broken["drawdown_derisk"] = {"version": version, "max_derisk_fraction": "0.25"}
                    errors = []
                    validate_gate_prerequisites(broken, errors)
                    self.assertTrue(any(rule.field in e and "drawdown_derisk.version" in e for e in errors), (rule.field, version))
                    checked += 1
                ok = copy.deepcopy(present)
                ok["drawdown_derisk"] = {"version": "v3", "max_derisk_fraction": "0.25"}
                errors = []
                validate_gate_prerequisites(ok, errors)
                self.assertEqual([e for e in errors if "drawdown_derisk.version" in e], [], rule.field)
        self.assertGreaterEqual(checked, 12)

    def test_scalar_gates_only_accept_the_declared_versions(self):
        for rule in GATE_PREREQUISITES:
            if not rule.allowed:
                continue
            with self.subTest(gate=rule.field):
                sizing = copy.deepcopy(self.production)
                for bad in ("v9", True, None, 1):
                    broken = copy.deepcopy(sizing)
                    nested_set(broken, rule.field, bad)
                    errors = []
                    validate_gate_prerequisites(broken, errors)
                    self.assertTrue(any("position_sizing.%s 只允許" % rule.field in e for e in errors), (rule.field, bad, errors))
                for good in rule.allowed:
                    ok = copy.deepcopy(sizing)
                    nested_set(ok, rule.field, good)
                    errors = []
                    validate_gate_prerequisites(ok, errors)
                    self.assertEqual([e for e in errors if "position_sizing.%s 只允許" % rule.field in e], [], (rule.field, good))

    def test_requirement_errors_surface_through_the_policy_validator(self):
        bundle = sized_inputs()[0]
        policy = base_policy()
        policy["position_sizing"] = {**copy.deepcopy(SIZING), "rotation": {"version": "v1", "max_swaps_per_run": 1, "min_new_conviction": "low"}}
        policy["content_sha256"] = decision_policy_sha256(policy)
        errors = DecisionPolicyValidator(bundle).validate(policy)
        self.assertTrue(any("position_sizing.rotation 必須與 held_add_priority 同時存在" in e for e in errors), errors)
        # volatility_exponent 的雙向要求：v1 不得出現、v2 必須出現（後者仍由 _validate_volatility_exponent 檢查）。
        self.assertTrue(any("volatility_exponent" in e and SIZING_METHOD_V2 in e for e in sizing_errors({**SIZING, "volatility_exponent": "0"})))
        self.assertTrue(any("必要欄位" in e for e in sizing_errors({**SIZING, "method": SIZING_METHOD_V2})))

    def test_turnover_fit_without_cash_fit_order_stays_allowed_and_uses_the_default_fill_ratio(self):
        """表中以 note 記錄、不強制的隱含相依：turnover_fit 單獨啟用仍是合法的舊組合（封存 run 以外的回放樣板），
        _fit_turnover 的新股殘量門檻退回預設 0.5；cash_fit_min_fill_ratio 本身則必須與 cash_fit_order 同時出現。"""
        self.assertEqual(rule_for("turnover_fit").requires, ())
        self.assertIn("0.5", rule_for("turnover_fit").note)
        self.assertEqual(sizing_errors({**SIZING, "turnover_fit": "v1"}), [])
        self.assertEqual(cash_fit_min_fill_ratio({"turnover_fit": "v1"}), CASH_FIT_MIN_FILL_RATIO)
        self.assertTrue(any("cash_fit_min_fill_ratio" in e and "cash_fit_order" in e
                            for e in sizing_errors({**SIZING, "turnover_fit": "v1", "cash_fit_min_fill_ratio": "0.5"})))

    def test_nested_gate_paths_are_checked(self):
        sizing = copy.deepcopy(self.production)
        sizing["rotation"]["forced_swap"]["confirmation"] = "v2"
        errors = sizing_errors(sizing)
        self.assertTrue(any("position_sizing.rotation.forced_swap.confirmation 只允許 v1" in e for e in errors), errors)
        sizing = copy.deepcopy(self.production)
        sizing["rotation"] = "v1"  # 上層不是物件：巢狀路徑視為不存在，由 _validate_rotation 的形狀檢查報錯
        errors = sizing_errors(sizing)
        self.assertTrue(any("position_sizing.rotation 必須是物件" in e for e in errors), errors)
        self.assertFalse(any("forced_swap" in e for e in errors), errors)


# ---------------------------------------------------------------- 現行 config 全開
class ProductionAllGatesTests(unittest.TestCase):
    """2317 high 新買、2330 medium 加碼（持 1 張）、現金 200 萬、neutral／middle（緩衝 10%）。"""

    def world(self, sizing=None):
        bundle, intent, _, policy = sized_inputs("high", "medium", "neutral")
        bundle = account(bundle, "2000000", 1000)
        return bundle, intent, sized(bundle, intent, stance(), anchored_sizing(bundle, sizing), policy)

    def test_live_config_runs_end_to_end_and_exposes_every_gate(self):
        bundle, _, (policy, body) = self.world()
        effective = body["effective_policy"]
        for field in ("cash_fit_order", "cash_buffer_floor", "high_conviction_cash_priority", "cash_ceiling_fix",
                      "stock_limit_headroom", "buy_gap_reserve", "sector_fit", "turnover_fit"):
            self.assertEqual(effective[field], "v1", field)
        self.assertEqual(effective["cash_buffer_rate"], "0.10000000")  # grid[neutral][middle]，不是 cash_buffer_by_stance 的 0.10／0.03
        self.assertEqual(body["position_sizing"]["limit_scaled_conviction"], {"version": "v1", "alpha": "0.60000000"})
        self.assertEqual(orders(body)[("2317.TW", "buy")]["shares"], 9000)
        self.assertNotIn(("2330.TW", "buy"), orders(body))
        self.assertIn(("2330.TW", "TURNOVER_LIMIT"), flags(body))
        result = guard(bundle, policy, body)
        self.assertTrue(result["passed"], failed_rules(result))

    def test_production_snapshot_rebuilds_the_pinned_hash(self):
        """固定快照（不讀 live 檔）：配置程式任何無意的行為改變都會改掉這個雜湊。"""
        _, _, (_, body) = self.world(PRODUCTION_SIZING_2026_11_08)
        self.assertEqual(body["content_sha256"], PRODUCTION_SNAPSHOT_SHA256)

    def test_live_config_enables_cash_stance_recency(self):
        """2026-11-09 人決定開啟；要回滾就刪掉這個欄位，並同步改本測試與 docs/status.md。"""
        self.assertEqual(live_production_policy()["position_sizing"].get("cash_stance_recency"), {"version": "v1"})

    def test_live_config_enables_exposure_rotation_with_the_agreed_numbers(self):
        """2026-11-09 人決定開啟（17:50 由 v1 切到 v2，23:45 切到 v3 為 rebound_participation 的前提）；數字改動或回滾（刪欄位）都要同步改本測試與 docs/status.md。"""
        gate = live_production_policy()["position_sizing"].get("exposure_rotation")
        self.assertEqual(gate, {
            "version": "v3", "beta_window_days": 60,
            "beta_target_by_stance": {"aggressive": "0.90", "neutral": "0.55", "defensive": None},
            "max_swaps_per_run": 2, "min_holding_days": 5, "hold_conviction": "v1",  # 2026-11-10 20:10 agent_swap v2 開啟時移除 conviction_gap_swap（互斥）
        })

    def test_live_config_matches_the_snapshot_except_for_reflection_knobs(self):
        """live 樣板與快照只允許在策略反思旋鈕上不同；版本閘門不同代表快照（與本測試）需要人更新。"""
        live = copy.deepcopy(production_policy()["position_sizing"])
        # selection_check 只在事後重建時多驗前次 run 的挑選，不改變配置結果，所以不影響快照雜湊。
        live.get("cash_ratio_range", {}).pop("selection_check", None)
        knob_roots = {knob.split(".")[0] for knob in KNOBS}
        for field in set(live) | set(PRODUCTION_SIZING_2026_11_08):
            if field in knob_roots:
                continue
            self.assertEqual(live.get(field), PRODUCTION_SIZING_2026_11_08.get(field), field)


# ---------------------------------------------------------------- 互動 1：derisk v4 × turnover_fit × RISK_TURNOVER
class DeriskTurnoverAllGatesTests(unittest.TestCase):
    """全開組合、defensive／lower（15%）、換手上限 20%：現金 3 萬、2330 1 張標 exit、2317 10 張續抱（NAV 131 萬）。

    出場後現金 14.7% 仍低於 15%：v3 的減碼把賣單推過換手上限 → RISK_TURNOVER 留住交易 Agent 的 exit、改賣 2 張 2317；
    v4 的減碼預算扣掉 exit 後不足一張 → 不減碼，交易 Agent 的 exit 照賣、換手 12%。"""

    def world(self, version):
        bundle, intent, _, policy = sized_inputs("medium", "low", "defensive")
        bundle = two_positions(bundle, "30000", 1000, 10000)
        limits = dict(max_stock_weight="1", special_weight_limits={"2330.TW": "1", "2317.TW": "1"}, max_sector_weight="1")
        bundle = with_rules(bundle, min_positions=1, **limits)
        items = [
            decision_item("2317.TW", "hold", adopt=["bull-2317-1"]) if item["symbol"] == "2317.TW"
            else decision_item("2330.TW", "exit", adopt=["bear-2330-1"], reject=["bull-2330-1"])
            for item in intent["items"]
        ]
        intent = reseal(bundle, intent, items)
        sizing = anchored_sizing(bundle, drawdown_derisk={"version": version, "max_derisk_fraction": "0.25"})
        policy, body = sized(bundle, intent, stance("defensive", "lower"), sizing, policy,
                             liquidity_fill_rate="1", min_positions=1, max_turnover_rate="0.20", **limits)
        return bundle, policy, body

    def test_v4_keeps_the_trader_exit_within_the_turnover_cap(self):
        bundle, policy, body = self.world("v4")
        self.assertEqual({k: v["intent"] for k, v in orders(body).items()}, {("2330.TW", "sell"): "exit"})
        self.assertEqual(flags(body), set())
        self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal("0.20"))
        # 換手餘額不足一張：現金缺口留待下次，不違反緩衝以外的任何硬規則。
        self.assertLess(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.15"))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_v3_under_the_same_gates_still_fights_the_turnover_fit(self):
        bundle, policy, body = self.world("v3")
        self.assertEqual({k: (v["shares"], v["intent"]) for k, v in orders(body).items()}, {("2317.TW", "sell"): (2000, "trim")})
        self.assertEqual(flags(body), {("2317.TW", "RISK_DRAWDOWN"), ("2330.TW", "RISK_TURNOVER")})
        self.assertTrue(guard(bundle, policy, body)["passed"])


# ---------------------------------------------------------------- 互動 4：cash_ceiling_fix × reuse_sell_proceeds=false
class CashCeilingWithoutProceedsReuseTests(unittest.TestCase):
    """2330 出場後現金達 45% 上限：閘門留住 exit 的判斷只看 base 情境期末現金，與賣款可否再投（reuse_sell_proceeds）無關。"""

    def world(self, reuse, gate):
        bundle, _, _, _ = valid_inputs()
        with_rules(bundle, cash_weight_must_be_below="0.45", min_positions=0, reuse_sell_proceeds=reuse)
        settings = base_policy()
        settings.update({"cash_weight_ceiling": "0.45", "min_positions": 0, "max_turnover_rate": "1", "reuse_sell_proceeds": reuse})
        settings["content_sha256"] = decision_policy_sha256(settings)
        bundle, _, _, intent, policy, _, _, _ = chain_inputs(bundle, None, settings)
        intent = copy.deepcopy(intent)
        item = next(row for row in intent["items"] if row["symbol"] == "2330.TW")
        item.update(intent="exit", adopted_claim_ids=["bear-2330-1"], rejected_claim_ids=["bull-2330-1"])
        refresh_artifact_hash(intent)
        if gate:
            policy = gated(policy, cash_ceiling_fix="v1")
        self.assertEqual(DecisionPolicyValidator(bundle).validate(policy), [])
        body = AllocationOrderEngine(bundle, policy).run(intent)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        return bundle, policy, body

    def test_gate_keeps_the_exit_regardless_of_proceeds_reuse(self):
        kept = {}
        for reuse in (True, False):
            bundle, policy, body = self.world(reuse, gate=True)
            self.assertIn(("2330.TW", "RISK_CASH_CEILING"), flags(body))
            self.assertEqual({k: v["shares"] for k, v in orders(body).items()}, {("2317.TW", "buy"): 2000})
            self.assertLess(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.45"))
            self.assertIs(body["effective_policy"]["reuse_sell_proceeds"], reuse)
            self.assertTrue(guard(bundle, policy, body)["passed"])
            kept[reuse] = (orders(body), flags(body), body["allocation_proposal"]["cash_weight"])
        self.assertEqual(kept[True], kept[False])

    def test_without_gate_guard_rejects_under_both_settlement_views(self):
        for reuse in (True, False):
            bundle, policy, body = self.world(reuse, gate=False)
            self.assertIn(("2330.TW", "sell"), orders(body))
            self.assertGreaterEqual(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.45"))
            self.assertIn("CASH_WEIGHT", failed_rules(guard(bundle, policy, body)))


# ---------------------------------------------------------------- 互動 2：high_conviction_cash_priority × limit_scaled_conviction × cash_ratio_range
class CashPriorityLimitScaledRatioRangeTests(unittest.TestCase):
    """三檔世界、全開組合、aggressive／lower（grid 3%）：2317 high 新買、1101 medium 新買（價 50）、2330 medium 加碼（持 3 張）。

    現金 30 萬、NAV 79.2 萬：2330 現有權重 62% 高於份額 → held_weight_budget 凍結、RISK_STOCK_WEIGHT 減碼；剩餘預算
    由 limit_scaled_conviction 先給 high 至少 α × 單股上限 = 0.6 × 0.5 = 30%，1101 只剩 4.9%（不足一張）；沒有 α 閘門時
    兩檔新股各一張。現金緩衝 3% 來自 grid，不是 cash_buffer_by_stance 的 3%／10%／20% 階梯。"""

    LIMITS = dict(max_stock_weight="0.5", special_weight_limits={"2330.TW": "0.5"}, max_sector_weight="1")

    def world(self, cash="300000", shares_2330=3000, drop=()):
        bundle = three_symbol_bundle(cash, {"2330.TW": shares_2330}, price_1101="50", **self.LIMITS)
        intent = three_symbol_intent(bundle, [
            decision_item("1101.TW", "buy", "medium", adopt=["bull-1101-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("2330.TW", "add", "medium", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
        ])
        sizing = anchored_sizing(bundle)
        for field in drop:
            sizing.pop(field)
        policy = base_policy()
        policy["sector_by_symbol"]["1101.TW"] = "水泥"
        policy, body = sized(bundle, intent, stance("aggressive", "lower"), sizing, policy,
                             liquidity_fill_rate="1", max_turnover_rate="1", **self.LIMITS)
        return bundle, intent, policy, body

    def test_alpha_floor_takes_the_remaining_budget_before_the_medium_new_buy(self):
        bundle, _, policy, body = self.world()
        targets = {symbol: Decimal(weight) for symbol, weight in body["position_sizing"]["target_weights"].items()}
        self.assertEqual(targets["2317.TW"], Decimal("0.30000000"))  # α × 單股上限
        self.assertLess(targets["1101.TW"], Decimal("0.05"))
        self.assertEqual(body["effective_policy"]["cash_buffer_rate"], "0.03000000")
        self.assertEqual(body["position_sizing"]["limit_scaled_conviction"]["alpha"], "0.60000000")
        self.assertEqual({k: v["shares"] for k, v in orders(body).items()}, {("2317.TW", "buy"): 2000, ("2330.TW", "sell"): 1000})
        self.assertEqual(flags(body), {("1101.TW", "LOT_SIZE"), ("2330.TW", "RISK_HELD_WEIGHT"), ("2330.TW", "RISK_STOCK_WEIGHT")})
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_without_alpha_floor_both_new_buys_get_a_lot(self):
        bundle, _, policy, body = self.world(drop=("limit_scaled_conviction",))
        targets = {symbol: Decimal(weight) for symbol, weight in body["position_sizing"]["target_weights"].items()}
        self.assertLess(targets["2317.TW"], Decimal("0.30"))
        self.assertEqual({k: v["shares"] for k, v in orders(body).items()},
                         {("1101.TW", "buy"): 1000, ("2317.TW", "buy"): 1000, ("2330.TW", "sell"): 1000})
        self.assertNotIn("limit_scaled_conviction", body["position_sizing"])
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_priority_gate_is_inert_when_the_budget_already_nets_the_buffer(self):
        """held_weight_budget 讓預算已扣掉現有權重與緩衝：全開組合下 high 不會被現金縮減，priority 閘門不改任何輸出。"""
        with_priority = self.world()[3]
        without = self.world(drop=("high_conviction_cash_priority",))[3]
        self.assertEqual(with_priority["order_proposal"]["orders"], without["order_proposal"]["orders"])
        self.assertEqual(with_priority["constraint_flags"], without["constraint_flags"])
        self.assertNotIn("CASH_BUFFER", {flag["code"] for flag in with_priority["constraint_flags"]})

    def test_priority_blocks_the_medium_new_buy_only_when_high_was_cash_clipped(self):
        """直接測 _fit_cash（全開 effective policy、grid 緩衝 3%）：現金 35 萬買不滿 2317 三張 → 縮為兩張並擋住未持有的
        1101；已持有的 2330 加碼只受一般現金限制。拿掉 priority 時 1101 可買一張。"""
        bundle, _, policy, _ = self.world()
        nav, cash, current = Decimal("1000000"), Decimal("350000"), {"2330.TW": 1000}

        def fit(priority):
            engine = AllocationOrderEngine(bundle, policy)
            if not priority:
                engine.policy = {**engine.policy, "position_sizing": {k: v for k, v in engine.policy["position_sizing"].items()
                                                                        if k != "high_conviction_cash_priority"}}
            effective = engine._effective_policy({})
            buys = [engine._order("2317.TW", "buy", 3000, "buy", effective), engine._order("1101.TW", "buy", 2000, "buy", effective),
                    engine._order("2330.TW", "buy", 1000, "add", effective)]
            fitted, _, fit_flags = engine._fit_cash(buys, cash, cash, nav, effective, current)
            return {o["symbol"]: int(o["shares"]) for o in fitted}, {(f["symbol"], f["code"]): f["detail"] for f in fit_flags}

        shares, details = fit(True)
        self.assertEqual(shares, {"2317.TW": 2000})
        self.assertIn("剩下的現金優先留給高信心", details[("1101.TW", "CASH_BUFFER")])
        self.assertNotIn("優先留給高信心", details[("2330.TW", "CASH_BUFFER")])
        self.assertIn(("2317.TW", "CASH_BUFFER"), details)
        shares, details = fit(False)
        self.assertEqual(shares, {"2317.TW": 2000, "1101.TW": 1000})
        self.assertNotIn(("1101.TW", "RISK_CASH_FIT"), details)


# ---------------------------------------------------------------- 互動 3：forced_swap.confirmation × 減碼日 × cash_buffer_floor
class ForcedSwapOnDeriskDayTests(unittest.TestCase):
    """三檔世界、全開組合、defensive／lower（15%）、滿檔 2 檔：2330 2 張（hold、論點被推翻、本次採納空頭 claim）、
    1101 4 張（價 100、振幅 ±10 所以 ATR 最高）、現金 3 萬（4.2%）；2317 high 新買。

    減碼日：程式先依 ATR 減碼 1101 一張（RISK_DRAWDOWN），強制換股仍成立（賣款含減碼收入）換出 2330，2317 的買單在
    減碼日以完整 15% 緩衝計（cash_buffer_floor 不放行原有現金）、由 3 張縮為 2 張（CASH_BUFFER）。沒有 derisk 閘門時
    不減碼、cash_buffer_floor 生效，2317 同樣 2 張但現金只剩 17%。"""

    LIMITS = dict(max_positions=2, min_positions=0, max_stock_weight="1",
                  special_weight_limits={"2330.TW": "1", "2317.TW": "1", "1101.TW": "1"}, max_sector_weight="1")

    def world(self, drop=()):
        bundle = three_symbol_bundle("30000", {"2330.TW": 2000, "1101.TW": 4000}, price_1101="100", spread="10", **self.LIMITS)
        intent = three_symbol_intent(bundle, [
            decision_item("1101.TW", "hold", adopt=["bull-1101-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            decision_item("2330.TW", "hold", adopt=["bull-2330-1", "bear-2330-1"]),
        ])
        sizing = anchored_sizing(bundle)
        sizing["rotation"]["forced_swap"].update(
            broken_symbols=["2330.TW"],
            verdicts_ref={"verdicts_id": "v-1", "as_of": "2026-09-19T10:00:00+00:00", "sha256": "a" * 64},
        )
        for field in drop:
            sizing.pop(field)
        policy = base_policy()
        policy["sector_by_symbol"]["1101.TW"] = "水泥"
        policy, body = sized(bundle, intent, stance("defensive", "lower", "concentrated"), sizing, policy,
                             liquidity_fill_rate="1", max_turnover_rate="1", **self.LIMITS)
        return bundle, policy, body

    def test_derisk_day_swap_uses_the_full_buffer_for_the_replacement(self):
        bundle, policy, body = self.world()
        self.assertEqual({k: (v["shares"], v["intent"]) for k, v in orders(body).items()},
                         {("1101.TW", "sell"): (1000, "trim"), ("2330.TW", "sell"): (2000, "exit"), ("2317.TW", "buy"): (2000, "buy")})
        self.assertEqual(flags(body), {("1101.TW", "RISK_DRAWDOWN"), ("2330.TW", "RISK_FORCED_SWAP"), ("2317.TW", "CASH_BUFFER")})
        # 交易 Agent 的意圖未被改寫：2330 仍是 hold，由程式標 exit；2317 買到 2 張後現金 30% ≥ 完整緩衝 15%。
        self.assertEqual(next(o for o in body["order_proposal"]["orders"] if o["symbol"] == "2330.TW")["intent"], "exit")
        self.assertEqual(next(i for i in body["constraint_flags"] if i["code"] == "RISK_FORCED_SWAP")["symbol"], "2330.TW")
        self.assertGreaterEqual(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.15"))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_cash_buffer_floor_is_neutralised_on_a_derisk_day(self):
        """減碼日 floor_disabled：有沒有 cash_buffer_floor 閘門，訂單與旗標逐位元相同。"""
        with_floor = self.world()[2]
        without = self.world(drop=("cash_buffer_floor",))[2]
        self.assertEqual(with_floor["order_proposal"]["orders"], without["order_proposal"]["orders"])
        self.assertEqual(with_floor["constraint_flags"], without["constraint_flags"])

    def test_without_derisk_the_swap_still_happens_and_the_floor_applies(self):
        bundle, policy, body = self.world(drop=("drawdown_derisk",))
        self.assertEqual({k: (v["shares"], v["intent"]) for k, v in orders(body).items()},
                         {("2330.TW", "sell"): (2000, "exit"), ("2317.TW", "buy"): (2000, "buy")})
        self.assertEqual(flags(body), {("2330.TW", "RISK_FORCED_SWAP")})
        # cash_buffer_floor：緩衝只擋原有現金（3 萬），賣款可全部再投，現金落在 15%～18%。
        cash_weight = Decimal(body["allocation_proposal"]["cash_weight"])
        self.assertGreaterEqual(cash_weight, Decimal("0.15"))
        self.assertLess(cash_weight, Decimal("0.18"))
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_single_holding_swap_collides_with_the_turnover_cap_and_keeps_the_victim(self):
        """兩檔 fixture、滿檔 1 檔：換出 96% 再買進必超過 100% 換手，替補被 turnover_fit 砍光 → 現金 100% 觸發
        RISK_CASH_CEILING 留住被換出者 → 該輪不再減碼。重建一致、Guard 通過，但現金維持在緩衝之下（已知邊界，記錄於報告）。"""
        bundle, intent, _, policy = sized_inputs("high", "low", "defensive")
        bundle = account(bundle, "30000", 4000)
        limits = dict(max_positions=1, min_positions=0, max_stock_weight="1", special_weight_limits={"2330.TW": "1", "2317.TW": "1"},
                      max_sector_weight="1")
        bundle = with_rules(bundle, **limits)
        items = [
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]) if item["symbol"] == "2317.TW"
            else decision_item("2330.TW", "hold", adopt=["bull-2330-1", "bear-2330-1"])
            for item in intent["items"]
        ]
        intent = reseal(bundle, intent, items)
        sizing = anchored_sizing(bundle)
        sizing["rotation"]["forced_swap"].update(
            broken_symbols=["2330.TW"], verdicts_ref={"verdicts_id": "v-1", "as_of": "2026-09-19T10:00:00+00:00", "sha256": "a" * 64},
        )
        policy, body = sized(bundle, intent, stance("defensive", "lower", "concentrated"), sizing, policy,
                             liquidity_fill_rate="1", max_turnover_rate="1", **limits)
        self.assertEqual(body["order_proposal"]["orders"], [])
        self.assertEqual(flags(body), {("2317.TW", "MAX_POSITIONS"), ("2330.TW", "RISK_CASH_CEILING")})
        self.assertLess(Decimal(body["allocation_proposal"]["cash_weight"]), Decimal("0.15"))
        self.assertTrue(guard(bundle, policy, body)["passed"])


if __name__ == "__main__":
    unittest.main()
