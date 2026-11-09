"""position_sizing.exposure_rotation v1 的端到端測試（補 test_exposure_rotation.py 沒有走到的整條鏈與互動路徑）。

覆蓋（每項都有正向與 fail-closed 反向）：

1. 真實 ``DecisionFinalizer`` 整條鏈：閘門開啟＋team_inputs 2.7 approved；``exposure_review`` 被竄改／缺漏／在沒有閘門的
   policy 下出現／閘門開啟但 team_inputs 停在 2.6 一律 rejected 且點名 exposure；``held_since_by_symbol`` 與帳本不符、
   帳本讀不到或歷史不符一律 rejected。
2. 與 ``drawdown_derisk`` 互斥：減碼日（RISK_DRAWDOWN）整個略過曝險換股並留下 ``…_SKIPPED:derisk_day``；防守姿態不觸發並留下旗標。
3. 換出者隔日出現在 ``reentry_cooldown`` 的 ``blocked_symbols``（以真實 Finalizer 結果存進決策庫，再由 ``bind_reentry_cooldown`` 綁定、
   配置時擋下再買進）。
4. 回放：``bind_replay_policy`` 對帶 exposure_rotation 的樣板不換股，notes 標 ``exposure_rotation: ignored_in_replay``；
   沒有閘門的舊回放 notes 與 policy 逐位元不變。
5. 與 turnover_fit、min_positions、sector_fit 的互動：換手上限縮減或取消換股、不讓持股數低於 min_positions、不讓產業超過上限，
   而且任何情況都不會出現只賣不買。

夾具說明（既有夾具組不出來的部分，自建的最小夾具）：

- ``ChainWorld``：test_exposure_rotation 的三檔世界（``exposure_bundle``、``exposure_sizing``、``decisions``）只做到「配置」為止，
  沒有 Finalizer 需要的整串輸入。這裡另組一份 2.7 整鏈：event 報告用 2.2（``seal_event_market_report``，team_inputs 2.1 以上的要求）、
  bundle 補 ``fundamental_period_policy`` 與成對的交易狀態包（``cash_ratio_range.selection_check`` 要目標交易時段）、
  policy 另綁帳本 NAV 歷史的 ``history_ref`` 與入選排序，再以假帳本提供 NAV 與持股歷史。這些步驟與每日 pipeline 的順序相同。
- 三檔多空辯論與 event 報告複製自 ``three_symbol_intent``（它只回傳交易決策、event 報告是舊版，過不了 2.1 以上的 Finalizer 驗證）。
- 配置互動測試用的 ``allocate`` 與 ``run_world`` 相同，只多了現金子等級（減碼日要 defensive／lower）與可調的產業分類。
"""

import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from etf_agent.data import TradingStatusBundleBuilder, TradingStatusRequest
from etf_agent.decision import (
    AllocationOrderEngine,
    CompetitionGuardV2,
    DecisionFinalizer,
    DecisionRepository,
    DecisionResultValidator,
    DecisionToolError,
    MomentumEngine,
    ProposalValidator,
    RevisionHistoryBuilder,
    ScenarioEngine,
    apply_selection_ranking,
    apply_trade_decision,
    bind_drawdown,
    bind_reentry_cooldown,
    build_exposure_review,
    build_portfolio_slots,
    build_position_review,
    build_prior_decision_context,
    build_research_debate,
    build_stance_brief,
    build_team_inputs,
    canonical_sha256,
    compute_fundamental_metrics,
    decision_bundle_sha256,
    decision_policy_sha256,
    program_ranking,
    ranking_envelope,
    recent_exit_symbols,
    seal_stance_packet,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.exposure import validate_held_since
from etf_agent.decision.analysts import report_envelope, seal_event_market_report
from etf_agent.decision.input_validator import FUNDAMENTAL_PERIOD_PER_SYMBOL, FUNDAMENTAL_PERIOD_POLICY_FIELD
from etf_agent.evaluation.replay import ReplayError, bind_replay_policy

from test_analysts import event_items, finding, report, technical_items
from test_cash_ratio_range import STANCE as LEGACY_STANCE, chain_inputs
from test_daily_pipeline import gateless_policy_template, live_policy_template
from test_exposure_rotation import (
    GATE, LIMITS, NARROW, REVIEW_POLICY, decisions, exposure_bundle, exposure_sizing, flag_map, gate_with,
    hold_item,
)
from test_gate_prerequisites import anchored_sizing, decision_item, sized, stance, three_symbol_intent
from test_portfolio_risk_decision import policy as base_policy, risk_review
from test_risk_limit_gates import CONTROL, guard, orders
from test_trading_status import CATEGORIES, coverage

S1 = "2026-09-21T09:00:00+08:00"  # 第一天的目標交易時段（cutoff 為 fixture 的 2026-09-20T00:55Z）
DAY2_CUTOFF = "2026-09-21T00:55:00+00:00"
FIRST_SEEN = "2026-09-01T06:00:00+00:00"
SECOND_SEEN = "2026-09-02T06:00:00+00:00"
EXPOSURE_CODES = ("RISK_EXPOSURE_ROTATION", "EXPOSURE_ROTATION_FILL", "RISK_CONVICTION_GAP_SWAP", "CONVICTION_GAP_SWAP_FILL")
SYMBOLS = ("1101.TW", "2317.TW", "2330.TW")


# ---------------------------------------------------------------- 夾具：交易時段、整鏈、假帳本
def with_session(bundle, start, cutoff=None, snapshot_id=None):
    """補上三檔的成對交易狀態包（目標時段 start）；cutoff／snapshot_id 有給時一併改成「隔日」的 bundle。"""
    if cutoff is not None:
        bundle["snapshot_id"] = bundle["snapshot"]["snapshot_id"] = snapshot_id or "snapshot-day-2"
        bundle["decision_cutoff"] = bundle["snapshot"]["decision_cutoff"] = cutoff
        bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
    request = TradingStatusRequest(
        request_id="status-%s" % bundle["snapshot_id"], snapshot_id=bundle["snapshot_id"], universe_version="universe-1",
        universe_symbols=SYMBOLS, decision_cutoff=bundle["decision_cutoff"], target_session_start=start,
        target_session_end=start.replace("09:00", "13:30"), required_categories=tuple(CATEGORIES),
        source_config_version="source-config-1", policy_version="policy-1",
    )
    status_bundle, assessment = TradingStatusBundleBuilder(request, [], coverage()).build()
    bundle["trading_status_bundle"] = status_bundle
    bundle["tradability_assessment"] = assessment
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def integrated_chain(bundle, items):
    """三檔的 2.1 以上整合鏈：三份分析報告（event 2.2）、多空辯論與封存的交易決策。回傳 (momentum, debate, trade, reports)。"""
    momentum = MomentumEngine(bundle).run()
    technical = [{"symbol": "1101.TW", "outlook": "positive", "findings": [finding("tech-1101-1", "price-1101")], "data_gaps": []},
                 *technical_items()]
    unknown = [{"symbol": s, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]} for s in SYMBOLS]
    events = [{"symbol": "1101.TW", "outlook": "unknown", "findings": [], "data_gaps": ["NO_MATERIAL_EVENT_IN_WINDOW"], "events": []},
              *event_items()]
    events = [{**item, "event_outlook": item["outlook"]} for item in events]
    reports = {
        "technical": report(bundle, "technical", technical),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": seal_event_market_report(bundle, report_envelope(bundle, "event", "report-event"), events),
    }

    def claim(claim_id, evidence):
        return {"claim_id": claim_id, "text": "依價格證據。", "evidence_ids": [evidence], "finding_ids": []}

    bull_items = [
        {"symbol": "1101.TW", "strength": "moderate", "claims": [claim("bull-1101-1", "price-1101")], "invalidation_conditions": []},
        {"symbol": "2317.TW", "strength": "moderate", "claims": [claim("bull-2317-1", "price-2317")], "invalidation_conditions": []},
        {"symbol": "2330.TW", "strength": "weak", "claims": [claim("bull-2330-1", "price-2330")], "invalidation_conditions": []},
    ]
    bear_items = [{"symbol": s, "strength": "none", "claims": [], "invalidation_conditions": []} for s in ("1101.TW", "2317.TW")] + [
        {"symbol": "2330.TW", "strength": "moderate", "claims": [claim("bear-2330-1", "price-2330")], "invalidation_conditions": []}]
    bull = seal_stance_packet(build_stance_brief(bundle, momentum, reports, {}, "bull")["packet_envelope"], bull_items)
    bear = seal_stance_packet(build_stance_brief(bundle, momentum, reports, {}, "bear")["packet_envelope"], bear_items)
    debate = build_research_debate(bundle, bull, bear, "debate-3")
    trade = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-3"), items)
    return momentum, debate, trade, reports


def ledger_providers(nav_rows, holding_rows):
    """假帳本：NAV 歷史來源附 ``holdings_history`` 屬性（與 ``ledger_nav_history_provider`` 同形）。"""
    def nav_provider(head):
        return [dict(row) for row in nav_rows]

    def holdings_provider(head):
        return [dict(row, symbols=list(row["symbols"])) for row in holding_rows]

    nav_provider.holdings_history = holdings_provider  # type: ignore[attr-defined]
    return nav_provider, holdings_provider


class ChainWorld:
    """曝險換股整條鏈（2.7）：決策輸入、policy（含帳本 history_ref、入選排序、hold 信心）、team_inputs 與全部下游 artifacts。

    ``with_gate=False`` 是對照組：同一個世界、沒有閘門（team_inputs 2.6、hold 不給信心），驗證舊路徑不受影響。
    ``held_since`` 只改 policy 裡綁的持有起點（帳本仍是真的），用來造出「policy 與帳本不符」。
    """

    def __init__(self, directory, positions=None, cash=None, with_gate=True, held_since=None):
        kwargs = {}
        if cash is not None:
            kwargs["cash"] = cash
        if positions is not None:
            kwargs["positions"] = positions
        bundle = exposure_bundle(**kwargs)
        bundle[FUNDAMENTAL_PERIOD_POLICY_FIELD] = FUNDAMENTAL_PERIOD_PER_SYMBOL
        self.bundle = with_session(bundle, S1)
        held = sorted(str(p["symbol"]) for p in self.bundle["account_snapshot"]["positions"])
        self.nav_rows = [{"run_id": "ledger-1", "as_of": FIRST_SEEN, "nav": "990000"},
                         {"run_id": "ledger-2", "as_of": SECOND_SEEN, "nav": "995000"}]
        self.holding_rows = [{"run_id": "ledger-1", "as_of": FIRST_SEEN, "symbols": held},
                             {"run_id": "ledger-2", "as_of": SECOND_SEEN, "symbols": held}]
        self.nav_provider, self.holdings_provider = ledger_providers(self.nav_rows, self.holding_rows)
        items = decisions() if with_gate else decisions(c1101=None, c2330=None)
        self.momentum, self.debate, self.trade, self.reports = integrated_chain(self.bundle, items)
        if with_gate:
            gate = gate_with(held_since_by_symbol=held_since or {symbol: FIRST_SEEN for symbol in held})
            sizing = exposure_sizing(self.bundle, gate, **NARROW)
        else:
            sizing = anchored_sizing(self.bundle, **NARROW)
        base = base_policy()
        base["sector_by_symbol"]["1101.TW"] = "水泥"
        base.update(position_review=copy.deepcopy(REVIEW_POLICY), liquidity_fill_rate="1", max_turnover_rate="1", **LIMITS)
        base["position_sizing"] = sizing
        base["drawdown_control"] = dict(CONTROL)
        base = bind_drawdown(base, self.bundle, self.nav_rows)  # 帶 history_ref：head_run_id = ledger-2
        base["content_sha256"] = decision_policy_sha256(base)
        self.stance = stance("neutral", "middle", "concentrated")
        policy = apply_trade_decision(base, self.trade, self.stance, self.bundle, self.debate)
        self.ranking = program_ranking(ranking_envelope(self.bundle, self.trade, "r-1"), self.trade)
        self.policy = apply_selection_ranking(policy, self.ranking)
        self.policy["content_sha256"] = decision_policy_sha256(self.policy)
        slots = build_portfolio_slots(self.bundle, self.policy, self.debate)
        self.prior = build_prior_decision_context(self.bundle, None)
        self.position_review = build_position_review(self.bundle, self.policy)
        self.metrics = compute_fundamental_metrics(self.bundle)
        self.exposure_review = build_exposure_review(self.bundle, self.policy) if with_gate else None
        self.team = build_team_inputs(
            self.reports, None, self.stance, self.prior, slots, self.position_review, self.ranking, self.metrics,
            self.exposure_review,
        )
        self.repo = Path(directory) / ("empty-repo-%d" % id(self))  # 沒有前次 run：anchor 取帳戶現金比例
        self.proposal = AllocationOrderEngine(self.bundle, self.policy).run(self.trade)
        self.scenario = ScenarioEngine(self.bundle, self.policy).run(self.proposal)
        self.guard = CompetitionGuardV2(self.bundle, self.policy).run(self.proposal, self.scenario)
        self.risk = risk_review(self.bundle, self.proposal, self.scenario, self.guard,
                                decision="approve" if self.guard["passed"] else "reject")
        self.history = RevisionHistoryBuilder(self.bundle, self.policy, self.trade).create(
            self.proposal, self.scenario, self.guard, self.risk)
        self.result = self.finalize()

    def finalize(self, policy=None, team=None, nav_provider=None, holdings_provider=None, bundle=None):
        finalizer = DecisionFinalizer(
            bundle or self.bundle, policy or self.policy, self.momentum, self.debate, self.trade, team or self.team, self.repo,
            nav_provider or self.nav_provider, holdings_provider,
        )
        return finalizer.run(self.proposal, self.scenario, self.guard, self.risk, self.history)

    def artifacts(self):
        return {
            "decision_input": self.bundle, "policy": self.policy, "momentum": self.momentum, "debate": self.debate,
            "intent": self.trade, "decision": self.result, "team_inputs": self.team, "proposal": self.proposal,
            "scenario": self.scenario, "guard": self.guard, "risk_review": self.risk, "revision_history": self.history,
        }

    def tampered_policy(self, mutate):
        policy = copy.deepcopy(self.policy)
        mutate(policy)
        policy["content_sha256"] = decision_policy_sha256(policy)
        return policy

    def tampered_team(self, mutate):
        team = copy.deepcopy(self.team)
        mutate(team)
        return team


def mentions(result, token):
    return [error for error in result["errors"] if token in error]


# ---------------------------------------------------------------- 1. 真實 Finalizer 整條鏈
class FinalizerChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.world = ChainWorld(cls.directory.name)
        cls.legacy = ChainWorld(cls.directory.name, with_gate=False)
        # 單一 NAV 歷史，但 policy 綁的持有起點與帳本不同（只有近期才買進，所以也不會換股）。
        recent = {"1101.TW": "2026-09-17T06:00:00+00:00", "2330.TW": "2026-09-17T06:00:00+00:00"}
        cls.stale = ChainWorld(cls.directory.name, held_since=recent)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def assertRejectedWith(self, result, *tokens):
        self.assertEqual(result["status"], "rejected", result["errors"])
        for token in tokens:
            self.assertTrue(mentions(result, token), (token, result["errors"]))

    # -- 正向
    def test_gate_with_team_inputs_2_7_is_approved_and_rebuilt(self):
        world = self.world
        self.assertEqual(world.team["schema_version"], "2.7")
        self.assertEqual(world.result["status"], "approved", world.result["errors"])
        self.assertEqual(world.result["team_inputs_sha256"], canonical_sha256(world.team))
        self.assertEqual({(o["symbol"], o["side"], o["intent"]) for o in world.result["orders"]},
                         {("2330.TW", "sell", "exit"), ("2317.TW", "buy", "buy")})
        self.assertIn(("2330.TW", "RISK_EXPOSURE_ROTATION"), flag_map(world.proposal))
        self.assertIn(("2317.TW", "EXPOSURE_ROTATION_FILL"), flag_map(world.proposal))
        validator = DecisionResultValidator(world.bundle, world.policy, world.momentum, world.debate, world.trade, world.team,
                                            world.repo, world.nav_provider)
        self.assertEqual(validator.validate(world.proposal, world.scenario, world.guard, world.risk, world.history, world.result), [])
        # 交易 Agent 的原意圖沒有被改寫：2330 仍是 hold。
        self.assertEqual(next(i for i in world.trade["items"] if i["symbol"] == "2330.TW")["intent"], "hold")

    def test_the_same_world_without_the_gate_is_approved_as_2_6_and_has_no_exposure_artifacts(self):
        legacy = self.legacy
        self.assertEqual(legacy.team["schema_version"], "2.6")
        self.assertNotIn("exposure_review", legacy.team)
        self.assertEqual(legacy.result["status"], "no_trade", legacy.result["errors"])  # 沒有閘門：兩檔持股續抱、沒有訂單
        self.assertEqual(legacy.result["errors"], [])
        self.assertNotIn("exposure_rotation", legacy.policy["position_sizing"])
        self.assertNotIn("hold_conviction_by_symbol", legacy.policy["position_sizing"])
        for flag in legacy.proposal["constraint_flags"]:
            self.assertNotIn("EXPOSURE", flag["code"])

    # -- exposure_review 竄改、缺漏、版本與閘門不同步
    def test_a_tampered_number_in_exposure_review_is_rejected(self):
        for mutate in (
            lambda team: team["exposure_review"]["portfolio"].update(portfolio_beta="0.900000"),
            lambda team: team["exposure_review"]["items"][0].update(beta="1.500000"),
            lambda team: team["exposure_review"]["portfolio"].update(low_beta_share="0.000000"),
        ):
            with self.subTest():
                result = self.world.finalize(team=self.world.tampered_team(mutate))
                self.assertRejectedWith(result, "exposure_review")
                self.assertTrue(mentions(result, "與重算結果不同"), result["errors"])

    def test_a_missing_or_reshaped_exposure_review_is_rejected(self):
        world = self.world
        cases = {
            "整個欄位缺漏": lambda team: team.pop("exposure_review"),
            "欄位是 null": lambda team: team.update(exposure_review=None),
            "欄位是空物件": lambda team: team.update(exposure_review={}),
            "少一檔持股": lambda team: team["exposure_review"].update(items=team["exposure_review"]["items"][:1]),
            "多出未允許欄位": lambda team: team["exposure_review"].update(advice="買進"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.assertRejectedWith(world.finalize(team=world.tampered_team(mutate)), "exposure_review")

    def test_exposure_review_without_a_gate_in_the_policy_is_rejected(self):
        legacy, gated = self.legacy, self.world
        # 2.7 的 team_inputs 加上別處算好的曝險檢視，但 policy 沒有閘門。
        as_2_7 = legacy.tampered_team(lambda team: team.update(schema_version="2.7", exposure_review=gated.exposure_review))
        result = legacy.finalize(team=as_2_7)
        self.assertRejectedWith(result, "exposure_rotation")
        self.assertTrue(mentions(result, "必須與 team_inputs 2.7 同時存在"), result["errors"])
        # 版本仍是 2.6 卻多帶 exposure_review：欄位白名單不符。
        # （訊息列出該版本允許的欄位清單，不點名多出來的 exposure_review；拒絕本身才是重點。）
        sneaked = legacy.tampered_team(lambda team: team.update(exposure_review=gated.exposure_review))
        self.assertRejectedWith(legacy.finalize(team=sneaked), "team_inputs 欄位必須剛好是")

    def test_gate_with_team_inputs_stuck_at_2_6_is_rejected(self):
        world = self.world
        downgraded = world.tampered_team(lambda team: (team.pop("exposure_review"), team.update(schema_version="2.6")))
        result = world.finalize(team=downgraded)
        self.assertRejectedWith(result, "exposure_rotation")
        self.assertTrue(mentions(result, "必須與 team_inputs 2.7 同時存在"), result["errors"])
        # 版本號改成 2.5 也不行（2.5 沒有基本面指標與曝險檢視，bundle 與 policy 的版本閘門都不符）。
        older = world.tampered_team(lambda team: (team.pop("exposure_review"), team.pop("fundamental_metrics"),
                                                  team.update(schema_version="2.5")))
        self.assertEqual(world.finalize(team=older)["status"], "rejected")

    def test_tampered_hold_conviction_binding_is_rejected(self):
        world = self.world
        forged = world.tampered_policy(lambda p: p["position_sizing"]["hold_conviction_by_symbol"].update({"2330.TW": "high"}))
        self.assertRejectedWith(world.finalize(policy=forged), "hold_conviction_by_symbol")

    # -- held_since 與帳本
    def test_held_since_that_differs_from_the_ledger_is_rejected(self):
        stale = self.stale
        self.assertEqual(stale.proposal["order_proposal"]["orders"], [])  # 持有不到 min_holding_days：不換股
        result = stale.finalize()
        self.assertRejectedWith(result, "held_since_by_symbol")
        self.assertTrue(mentions(result, "與帳本重算結果不一致"), result["errors"])
        # 對照：同一個世界、帳本也是近期才買進，就通過。
        recent_rows = [dict(row, as_of=row["as_of"].replace("09-01", "09-17")) for row in stale.holding_rows[:1]] + [
            dict(row, as_of=row["as_of"].replace("09-02", "09-18")) for row in stale.holding_rows[1:]]
        _, recent_holdings = ledger_providers(stale.nav_rows, recent_rows)
        self.assertEqual(mentions(stale.finalize(holdings_provider=recent_holdings), "held_since_by_symbol"), [])

    def test_ledger_that_cannot_be_read_is_rejected(self):
        world = self.world

        def broken(head):
            raise RuntimeError("帳本損毀")

        result = world.finalize(holdings_provider=broken)
        self.assertRejectedWith(result, "held_since_by_symbol")
        self.assertTrue(mentions(result, "無法由帳本重算") and mentions(result, "帳本損毀"), result["errors"])
        # 沒有任何帳本來源（NAV 來源沒有附持股歷史）。
        def nav_only(head):
            return [dict(row) for row in world.nav_rows]

        result = world.finalize(nav_provider=nav_only)
        self.assertRejectedWith(result, "held_since_by_symbol")
        self.assertTrue(mentions(result, "未提供帳本來源"), result["errors"])

    def test_ledger_history_that_does_not_match_is_rejected(self):
        world = self.world
        held = world.holding_rows[0]["symbols"]
        cases = {
            "最新一筆不是 head_run_id": [dict(row, run_id="ledger-other") for row in world.holding_rows],
            "與帳戶持股不一致": [dict(row, symbols=["2330.TW"]) for row in world.holding_rows],
            "歷史是空的": [],
            "晚於 decision_cutoff": [world.holding_rows[0], dict(world.holding_rows[1], as_of="2026-09-30T06:00:00+00:00")],
            "持有起點較晚": [dict(world.holding_rows[0], symbols=[]), world.holding_rows[1]],
        }
        self.assertEqual(held, ["1101.TW", "2330.TW"])
        for message, rows in cases.items():
            with self.subTest(message):
                _, provider = ledger_providers(world.nav_rows, rows)
                result = world.finalize(holdings_provider=provider)
                self.assertRejectedWith(result, "held_since_by_symbol")

    def test_unbound_held_since_never_reaches_approval(self):
        # held_since 沒綁：validate_held_since 回報「尚未綁定」，但後面重建配置的驗證器（RevisionHistoryValidator）讀到同一份 policy
        # 時由引擎拋 DecisionToolError，所以 Finalizer.run 不產生結果、直接例外（沒有任何路徑能 approved）。
        world = self.world
        unbound = world.tampered_policy(lambda p: p["position_sizing"]["exposure_rotation"].pop("held_since_by_symbol"))
        errors = []
        validate_held_since(unbound, world.bundle, world.holdings_provider, errors)
        self.assertTrue(any("尚未綁定" in error for error in errors), errors)
        with self.assertRaisesRegex(DecisionToolError, "held_since_by_symbol"):
            world.finalize(policy=unbound)

    def test_missing_history_anchor_is_rejected(self):
        world = self.world
        no_anchor = world.tampered_policy(lambda p: p["drawdown_control"].pop("history_ref"))
        self.assertRejectedWith(world.finalize(policy=no_anchor), "history_ref")

    def test_nav_ledger_that_cannot_be_read_is_rejected_too(self):
        world = self.world

        def broken(head):
            raise RuntimeError("NAV 帳本損毀")

        broken.holdings_history = world.holdings_provider  # type: ignore[attr-defined]
        result = world.finalize(nav_provider=broken)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(mentions(result, "NAV 帳本損毀"), result["errors"])


# ---------------------------------------------------------------- 配置互動的共用夾具
def allocate(bundle, items=None, level="neutral", grade="middle", gate=None, overrides=None, policy_fields=None,
             sector_1101="水泥", sizing=None):
    """與 ``run_world`` 相同，另可指定現金子等級與 1101 的產業分類；綁定、驗證、配置與重建比對都由 ``sized`` 完成。"""
    intent = three_symbol_intent(bundle, items if items is not None else decisions())
    sizing = sizing if sizing is not None else exposure_sizing(bundle, gate, **{**NARROW, **(overrides or {})})
    base = base_policy()
    base["sector_by_symbol"]["1101.TW"] = sector_1101
    fields = dict(position_review=copy.deepcopy(REVIEW_POLICY), liquidity_fill_rate="1", max_turnover_rate="1", **LIMITS)
    fields.update(policy_fields or {})
    policy, body = sized(bundle, intent, stance(level, grade, "concentrated"), sizing, base, **fields)
    return intent, policy, body


def codes(body):
    return {flag["code"] for flag in body["constraint_flags"]}


def sold_and_bought(body):
    rows = body["order_proposal"]["orders"]
    return {o["symbol"] for o in rows if o["side"] == "sell"}, {o["symbol"] for o in rows if o["side"] == "buy"}


# ---------------------------------------------------------------- 2. 與 drawdown_derisk 互斥
class DeriskExclusionTests(unittest.TestCase):
    # 現金 13.6%（低於 defensive 的 15% 子等級緩衝）：defensive 時 drawdown_derisk 減碼；neutral 時同樣的持股會觸發曝險換股。
    BUNDLE_ARGS = dict(cash="136000", positions={"2330.TW": 1000, "1101.TW": 7000})

    def test_defensive_derisk_day_skips_rotation_and_leaves_flags(self):
        bundle = exposure_bundle(**self.BUNDLE_ARGS)
        _, policy, body = allocate(bundle, level="defensive", grade="lower")
        found = codes(body)
        self.assertIn("RISK_DRAWDOWN", found)
        self.assertIn("EXPOSURE_ROTATION_SKIPPED:derisk_day", found)
        self.assertIn("CONVICTION_GAP_SWAP_SKIPPED:derisk_day", found)
        self.assertFalse(found & set(EXPOSURE_CODES), found)
        skip = next(f for f in body["constraint_flags"] if f["code"] == "EXPOSURE_ROTATION_SKIPPED:derisk_day")
        self.assertEqual(skip["symbol"], "PORTFOLIO")
        self.assertIn("互斥", skip["detail"])
        sold, bought = sold_and_bought(body)
        self.assertTrue(sold, "減碼日應有賣單（由 drawdown_derisk 產生，不是曝險換股）")
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_same_holdings_in_neutral_rotate_and_have_no_derisk_flag(self):
        bundle = exposure_bundle(**self.BUNDLE_ARGS)
        _, policy, body = allocate(bundle, level="neutral", grade="lower")
        found = codes(body)
        self.assertIn("RISK_EXPOSURE_ROTATION", found)
        self.assertNotIn("RISK_DRAWDOWN", found)
        self.assertNotIn("EXPOSURE_ROTATION_SKIPPED:derisk_day", found)
        sold, bought = sold_and_bought(body)
        self.assertEqual((sold, bought), ({"2330.TW"}, {"2317.TW"}))

    def test_defensive_without_derisk_is_not_triggered_and_leaves_the_stance_flag(self):
        bundle = exposure_bundle()  # 現金 33.6%：defensive 不需要減碼
        _, _, body = allocate(bundle, level="defensive", grade="lower")
        found = codes(body)
        self.assertIn("EXPOSURE_ROTATION_SKIPPED:stance_not_triggered", found)
        self.assertNotIn("RISK_DRAWDOWN", found)
        self.assertFalse(found & set(EXPOSURE_CODES), found)
        self.assertEqual(body["order_proposal"]["orders"], [])

    def test_no_run_ever_has_both_derisk_and_rotation_flags(self):
        seen = {"derisk": False, "rotation": False}
        for cash, positions in (("336000", None), ("136000", {"2330.TW": 1000, "1101.TW": 7000}),
                                ("60000", {"2330.TW": 1000, "1101.TW": 7000})):
            bundle = exposure_bundle(cash=cash, positions=positions) if positions else exposure_bundle()
            for level in ("aggressive", "neutral", "defensive"):
                with self.subTest(cash=cash, level=level):
                    _, _, body = allocate(bundle, level=level, grade="lower")
                    found = codes(body)
                    derisk = "RISK_DRAWDOWN" in found
                    rotated = bool(found & {"RISK_EXPOSURE_ROTATION", "RISK_CONVICTION_GAP_SWAP"})
                    self.assertFalse(derisk and rotated, (cash, level, found))
                    seen["derisk"] |= derisk
                    seen["rotation"] |= rotated
        self.assertEqual(seen, {"derisk": True, "rotation": True}, "掃描要同時走到減碼日與換股日，否則互斥沒有被檢驗")

    def test_forged_proposal_mixing_both_is_rejected_by_the_rebuild(self):
        bundle = exposure_bundle(**self.BUNDLE_ARGS)
        for level, extra in (("defensive", "RISK_EXPOSURE_ROTATION"), ("neutral", "RISK_DRAWDOWN")):
            with self.subTest(level):
                intent, policy, body = allocate(bundle, level=level, grade="lower")
                self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
                forged = copy.deepcopy(body)
                forged["constraint_flags"].append({"symbol": "1101.TW", "code": extra, "detail": "竄改"})
                self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))


# ---------------------------------------------------------------- 3. 換出者隔日出現在冷卻名單
class ReentryCooldownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.world = ChainWorld(cls.directory.name)
        cls.root = Path(cls.directory.name) / "decisions"
        DecisionRepository(cls.root).save("run-day-1", cls.world.artifacts(), day="2026-09-20")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def day_two(self, start="2026-09-22T09:00:00+08:00", cutoff=DAY2_CUTOFF):
        """隔日的帳戶：2330 已賣出、換進 2317（現金 16.4 萬）。"""
        bundle = exposure_bundle(cash="164000", positions={"1101.TW": 5000, "2317.TW": 3000})
        return with_session(bundle, start, cutoff)

    def test_the_swapped_out_symbol_is_blocked_the_next_day(self):
        world = self.world
        self.assertEqual(world.result["status"], "approved", world.result["errors"])
        victims = {symbol for symbol, code in flag_map(world.proposal) if code == "RISK_EXPOSURE_ROTATION"}
        exits = {o["symbol"] for o in world.result["orders"] if o["side"] == "sell" and o["intent"] == "exit"}
        self.assertEqual(victims, {"2330.TW"})
        self.assertEqual(exits, victims)  # 程式把賣單標成 exit，所以進冷卻名單
        bundle = self.day_two()
        self.assertEqual(recent_exit_symbols(self.root, bundle, 3), (["2330.TW"], ["run-day-1"]))
        bound = bind_reentry_cooldown({"position_sizing": {"reentry_cooldown": {"version": "v1", "days": 3}}}, self.root, bundle)
        self.assertEqual(bound["position_sizing"]["reentry_cooldown"],
                         {"version": "v1", "days": 3, "blocked_symbols": ["2330.TW"], "source_run_ids": ["run-day-1"]})

    def test_next_day_allocation_refuses_to_buy_the_swapped_out_symbol_back(self):
        bundle = self.day_two()
        items = [
            hold_item("1101.TW", "medium", adopt=["bull-1101-1"]),
            decision_item("2330.TW", "buy", "high", adopt=["bull-2330-1"], reject=["bear-2330-1"]),
            hold_item("2317.TW", "high", adopt=["bull-2317-1"]),
        ]
        held_since = {"1101.TW": FIRST_SEEN, "2317.TW": SECOND_SEEN}
        sizing = exposure_sizing(bundle, gate_with(held_since_by_symbol=held_since), **NARROW)
        sizing = bind_reentry_cooldown({"position_sizing": sizing}, self.root, bundle)["position_sizing"]
        self.assertEqual(sizing["reentry_cooldown"]["blocked_symbols"], ["2330.TW"])
        _, policy, body = allocate(bundle, items, sizing=sizing)
        self.assertIn(("2330.TW", "RISK_REENTRY_COOLDOWN"), flag_map(body))
        self.assertNotIn("2330.TW", sold_and_bought(body)[1])
        self.assertNotIn(("2330.TW", "EXPOSURE_ROTATION_FILL"), flag_map(body))
        self.assertTrue(guard(bundle, policy, body)["passed"])
        # 對照：沒有前次 run 的決策庫，名單是空的、不設冷卻。
        empty = Path(self.directory.name) / "no-history"
        self.assertEqual(recent_exit_symbols(empty, bundle, 3), ([], []))
        sizing = exposure_sizing(bundle, gate_with(held_since_by_symbol=held_since), **NARROW)
        sizing = bind_reentry_cooldown({"position_sizing": sizing}, empty, bundle)["position_sizing"]
        self.assertEqual(sizing["reentry_cooldown"]["blocked_symbols"], [])
        _, _, body = allocate(bundle, items, sizing=sizing)
        self.assertNotIn("RISK_REENTRY_COOLDOWN", codes(body))

    def test_cooldown_window_is_inclusive_of_the_third_calendar_day_and_then_expires(self):
        edge = self.day_two(start="2026-09-24T09:00:00+08:00")  # 與第一天的時段相隔剛好 3 日
        self.assertEqual(recent_exit_symbols(self.root, edge, 3)[0], ["2330.TW"])
        late = self.day_two(start="2026-09-25T09:00:00+08:00")  # 4 日：冷卻結束
        self.assertEqual(recent_exit_symbols(self.root, late, 3), ([], []))
        self.assertEqual(recent_exit_symbols(self.root, edge, 1), ([], []))

    def test_a_tampered_archive_is_rejected_instead_of_clearing_the_cooldown(self):
        run_dir = next(self.root.glob("*/run-day-1"))
        path = run_dir / "decision.json"
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(original.replace('"intent": "exit"', '"intent": "trim"'), encoding="utf-8")
            with self.assertRaises(Exception) as caught:
                recent_exit_symbols(self.root, self.day_two(), 3)
            self.assertIsInstance(caught.exception, (DecisionToolError, ValueError, RuntimeError))
        finally:
            path.write_text(original, encoding="utf-8")
        self.assertEqual(recent_exit_symbols(self.root, self.day_two(), 3)[0], ["2330.TW"])


# ---------------------------------------------------------------- 4. 回放
class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, self.debate, self.intent, self.archived, _, _, _ = chain_inputs()

    def template(self, factory):
        template = factory()
        template.update(policy_id="replay-A", available_at="2026-09-19T00:00:00+00:00")
        return template

    def replay(self, template):
        return bind_replay_policy(template, self.bundle, self.archived, self.intent, self.debate, dict(LEGACY_STANCE), [])

    def test_replay_ignores_the_gate_and_adds_one_note(self):
        for factory in (live_policy_template, gateless_policy_template):
            with self.subTest(factory.__name__):
                plain = self.template(factory)
                plain["position_sizing"].pop("exposure_rotation", None)  # 正式樣板已開啟；「沒有閘門」的對照組要先拿掉
                plain_policy, plain_notes = self.replay(plain)
                self.assertNotIn("exposure_rotation", plain_notes)
                gated = copy.deepcopy(plain)
                gated["position_sizing"].update(
                    rotation=gated["position_sizing"]["rotation"], held_add_priority="v1", selection_ranking="v1",
                    reentry_cooldown={"version": "v1", "days": 3}, turnover_fit="v1", cash_buffer_floor="v1",
                    exposure_rotation=copy.deepcopy(GATE),
                )
                gated.setdefault("position_review", copy.deepcopy(REVIEW_POLICY))
                policy, notes = self.replay(gated)
                self.assertEqual(notes["exposure_rotation"], "ignored_in_replay")
                self.assertEqual({k: v for k, v in notes.items() if k != "exposure_rotation"}, plain_notes)  # 其餘 notes 逐鍵不變
                self.assertNotIn("exposure_rotation", policy["position_sizing"])
                self.assertNotIn("hold_conviction_by_symbol", policy["position_sizing"])
                self.assertEqual(policy["position_sizing"], plain_policy["position_sizing"])
                self.assertEqual(policy["content_sha256"], plain_policy["content_sha256"])

    def test_replayed_allocation_never_rotates(self):
        gated = self.template(live_policy_template)
        gated["position_sizing"]["exposure_rotation"] = copy.deepcopy(GATE)
        policy, _ = self.replay(gated)
        body = AllocationOrderEngine(self.bundle, policy).run(self.intent)
        self.assertEqual(ProposalValidator(self.bundle, policy, self.intent).validate(body), [])
        self.assertFalse([c for c in codes(body) if "EXPOSURE" in c or "GAP_SWAP" in c], codes(body))
        # 與沒有閘門的回放配置逐位元相同。
        plain_policy, _ = self.replay(self.template(live_policy_template))
        self.assertEqual(AllocationOrderEngine(self.bundle, plain_policy).run(self.intent)["content_sha256"], body["content_sha256"])

    def test_replay_of_the_live_template_notes_ignored_gates(self):
        _, notes = self.replay(self.template(live_policy_template))
        self.assertEqual(
            set(notes),
            {"policy_id", "content_sha256", "available_at_overridden", "archived_cash_stance", "cash_stance", "stance_floor",
             "stance_raised_to_floor", "selection_ranking_applied", "breadth_unavailable", "cash_buffer_rate", "drawdown",
             "cash_grade", "cash_stance_recency", "exposure_rotation"},  # 正式樣板已開這兩個閘門，回放各標一次略過
        )

    def test_replay_still_rejects_a_malformed_gate_or_missing_prerequisite(self):
        def attempt(mutate):
            template = self.template(live_policy_template)
            template["position_sizing"]["exposure_rotation"] = copy.deepcopy(GATE)
            mutate(template)
            with self.assertRaises(ReplayError):
                self.replay(template)

        attempt(lambda t: t["position_sizing"]["exposure_rotation"].update(version="v2"))
        attempt(lambda t: t["position_sizing"].pop("held_add_priority"))
        attempt(lambda t: t.pop("position_review"))
        attempt(lambda t: t["position_sizing"]["exposure_rotation"].pop("max_swaps_per_run"))


# ---------------------------------------------------------------- 5. 與 turnover_fit、min_positions、sector_fit 的互動
class InteractionTests(unittest.TestCase):
    def assertNeverSellOnly(self, body):
        """曝險／信心差換股的每個換出者都有賣單、每個換入者都有買單，且兩邊一對一；沒有買單就沒有任何換股賣單。"""
        sold, bought = sold_and_bought(body)
        victims = {f["symbol"] for f in body["constraint_flags"] if f["code"] in ("RISK_EXPOSURE_ROTATION", "RISK_CONVICTION_GAP_SWAP")}
        fills = {f["symbol"] for f in body["constraint_flags"] if f["code"] in ("EXPOSURE_ROTATION_FILL", "CONVICTION_GAP_SWAP_FILL")}
        self.assertLessEqual(victims, sold)
        self.assertLessEqual(fills, bought)
        self.assertEqual(len(victims), len(fills))
        if not bought:
            self.assertEqual(victims, set())
        for order in body["order_proposal"]["orders"]:
            if order["side"] == "sell" and order["intent"] == "exit" and order["symbol"] in victims:
                self.assertTrue(bought, "換股的賣單必須有買單")

    def test_turnover_cap_shrinks_then_cancels_the_swap_but_never_leaves_a_sell_only_proposal(self):
        expected = {  # 上限 → (買 2317 的股數 或 None 代表整個換股被取消)
            "0.60": 3000, "0.45": 2000, "0.40": 2000, "0.30": 1000, "0.20": None, "0.10": None,
        }
        for cap, shares in expected.items():
            with self.subTest(cap=cap):
                bundle = exposure_bundle()
                _, policy, body = allocate(bundle, policy_fields={"max_turnover_rate": cap})
                self.assertNeverSellOnly(body)
                self.assertLessEqual(Decimal(body["order_proposal"]["turnover_rate"]), Decimal(cap))
                self.assertTrue(guard(bundle, policy, body)["passed"])
                if shares is None:
                    self.assertEqual(body["order_proposal"]["orders"], [])
                    self.assertNotIn("RISK_EXPOSURE_ROTATION", codes(body))
                    self.assertIn("EXPOSURE_ROTATION_SKIPPED:fill_cancelled", codes(body))
                else:
                    self.assertEqual(orders(body)[("2317.TW", "buy")]["shares"], shares)
                    self.assertEqual(orders(body)[("2330.TW", "sell")]["intent"], "exit")
                    self.assertEqual("TURNOVER_LIMIT" in codes(body), shares < 3000)

    def test_swap_never_drops_the_position_count_below_min_positions(self):
        for cap in ("0.60", "0.30", "0.20", "0.10"):
            with self.subTest(cap=cap):
                bundle = exposure_bundle(min_positions=2)
                _, policy, body = allocate(bundle, policy_fields={"max_turnover_rate": cap, "min_positions": 2})
                self.assertGreaterEqual(len(body["allocation_proposal"]["positions"]), 2)
                self.assertNotIn("RISK_MIN_POSITIONS", codes(body))  # 換股一對一、取消時原持股原封不動，不需要程式留住別的 exit
                self.assertNeverSellOnly(body)
                self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_trade_agent_exit_frees_the_slot_so_no_rotation_is_needed_and_min_positions_holds(self):
        # 交易 Agent 的 exit 優先（名額優先序：exit／trim ＞ forced_swap ＞ 曝險換股）：1101 賣出騰出名額，2317 直接買進，持股數仍是 2。
        bundle = exposure_bundle(min_positions=2)
        items = [
            decision_item("1101.TW", "exit", None, adopt=["bull-1101-1"]),
            decision_item("2317.TW", "buy", "high", adopt=["bull-2317-1"]),
            hold_item("2330.TW", "medium", adopt=["bull-2330-1", "bear-2330-1"]),
        ]
        _, policy, body = allocate(bundle, items, policy_fields={"min_positions": 2})
        self.assertEqual({s for s, _ in orders(body)}, {"1101.TW", "2317.TW"})
        self.assertEqual({p["symbol"] for p in body["allocation_proposal"]["positions"]}, {"2330.TW", "2317.TW"})
        self.assertFalse(codes(body) & set(EXPOSURE_CODES), codes(body))
        self.assertNeverSellOnly(body)
        self.assertTrue(guard(bundle, policy, body)["passed"])

    def test_sector_cap_shrinks_or_cancels_the_swap_and_never_pushes_a_sector_over_the_limit(self):
        cases = [  # (產業上限, 2317 與 1101 同產業?, 買 2317 的股數 或 None 代表換股被取消)
            ("0.90", True, 3000), ("0.70", True, 1000), ("0.55", True, None), ("0.55", False, 3000),
        ]
        for limit, same_sector, shares in cases:
            with self.subTest(limit=limit, same_sector=same_sector):
                bundle = exposure_bundle(max_sector_weight=limit)
                sector = "電子製造" if same_sector else "水泥"
                _, policy, body = allocate(bundle, policy_fields={"max_sector_weight": limit}, sector_1101=sector)
                self.assertNeverSellOnly(body)
                self.assertTrue(guard(bundle, policy, body)["passed"])
                weights = {}
                for position in body["allocation_proposal"]["positions"]:
                    key = policy["sector_by_symbol"][position["symbol"]]
                    weights[key] = weights.get(key, Decimal(0)) + Decimal(position["weight"])
                for key, weight in weights.items():
                    before = Decimal("0.5") if key == sector else Decimal(0)
                    self.assertLessEqual(weight, max(Decimal(limit), before), (key, weight))
                if shares is None:
                    self.assertEqual(body["order_proposal"]["orders"], [])
                    self.assertNotIn("RISK_EXPOSURE_ROTATION", codes(body))
                else:
                    self.assertEqual(orders(body)[("2317.TW", "buy")]["shares"], shares)
                    self.assertIn("RISK_EXPOSURE_ROTATION", codes(body))
                    self.assertEqual("RISK_SECTOR_WEIGHT" in codes(body), shares < 3000)

    def test_forged_sell_only_proposal_is_rejected_by_the_rebuild(self):
        bundle = exposure_bundle()
        intent, policy, body = allocate(bundle)
        self.assertEqual(ProposalValidator(bundle, policy, intent).validate(body), [])
        forged = copy.deepcopy(body)
        forged["order_proposal"]["orders"] = [o for o in forged["order_proposal"]["orders"] if o["side"] == "sell"]
        self.assertTrue(ProposalValidator(bundle, policy, intent).validate(forged))


if __name__ == "__main__":
    unittest.main()
