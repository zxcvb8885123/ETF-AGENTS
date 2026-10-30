"""前次決策背景（PriorDecisionContext）：依交易時段挑前次 run、建立、重建驗證、team_inputs 2.2 與交易 Agent 不可引用。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline
from etf_agent.decision import (
    DecisionRepository,
    DecisionToolError,
    MomentumEngine,
    PriorDecisionContextValidator,
    TradeDecisionValidator,
    apply_trade_decision,
    build_prior_decision_context,
    build_research_debate,
    build_stance_brief,
    build_team_inputs,
    canonical_sha256,
    decision_bundle_sha256,
    decision_policy_sha256,
    find_prior_decision_run,
    prior_decision_brief,
    read_decision_run,
    seal_stance_packet,
    seal_trade_decision,
    trade_decision_envelope,
)
from etf_agent.decision.analysts import report_envelope, seal_event_market_report
from etf_agent.decision.prior_decision import seal_prior_decision_context

from etf_agent.data import TradingStatusBundleBuilder, TradingStatusRequest

from test_analysts import event_items, report, technical_items
from test_daily_pipeline import FakeRunner
from test_portfolio_risk_decision import policy as base_policy
from test_position_sizing import SIZING
from test_stance import bear_items, bull_items
from test_team_chain import finalize, team_world, tradable_bundle
from test_trader import STANCE, items as trade_items
from test_trading_status import CATEGORIES, coverage


ROOT = Path(__file__).resolve().parents[2]
# 時間線（UTC cutoff／台北時段）：S1=09-21、S2=09-22、S3=09-23 三個目標交易時段；
# 每個時段有前一晚 21:00 備案與當天 07:00 重做兩個 run。
S1, S2, S3 = "2026-09-21T09:00:00+08:00", "2026-09-22T09:00:00+08:00", "2026-09-23T09:00:00+08:00"
PRIOR_CUTOFF = "2026-09-20T00:55:00+00:00"   # fixture 原 cutoff
LATER_CUTOFF = "2026-09-21T00:55:00+00:00"


def with_session(bundle, start):
    """fixture 沒有交易狀態包；用真實 Builder 補上帶目標交易時段的成對狀態包後重算 bundle 雜湊。"""
    request = TradingStatusRequest(
        request_id="status-%s" % bundle["snapshot_id"], snapshot_id=bundle["snapshot_id"], universe_version="universe-1",
        universe_symbols=("2330.TW", "2317.TW"), decision_cutoff=bundle["decision_cutoff"],
        target_session_start=start, target_session_end=start.replace("09:00", "13:30"),
        required_categories=tuple(CATEGORIES), source_config_version="source-config-1", policy_version="policy-1",
    )
    status_bundle, assessment = TradingStatusBundleBuilder(request, [], coverage()).build()
    bundle["trading_status_bundle"] = status_bundle
    bundle["tradability_assessment"] = assessment
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


def session_bundle(snapshot_id="snapshot-decision-2", cutoff=LATER_CUTOFF, session=S2, account_id=None):
    """同一份 fixture 資料、指定 cutoff／snapshot_id／目標時段的 bundle。"""
    bundle = tradable_bundle()
    bundle["snapshot_id"] = snapshot_id
    bundle["snapshot"]["snapshot_id"] = snapshot_id
    bundle["decision_cutoff"] = cutoff
    bundle["snapshot"]["decision_cutoff"] = cutoff
    if account_id is not None:
        bundle["account_snapshot"]["account_id"] = account_id
    bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
    return with_session(bundle, session)


def later_bundle(snapshot_id="snapshot-decision-2", cutoff=LATER_CUTOFF, account_id=None):
    """「本次」bundle：S2 時段的 07:00 重做；前次 run 用原 fixture cutoff 與 S1 時段。"""
    return session_bundle(snapshot_id, cutoff, S2, account_id)


def prior_bundle():
    return session_bundle("snapshot-decision-1", PRIOR_CUTOFF, S1)


def sealed_run(bundle=None, decisions=None):
    """以 team_world 做出一份完整封存內容（approved）；回傳可直接存進 DecisionRepository 的 artifacts。"""
    bundle, momentum, debate, trade, policy, team, _, _ = team_world(bundle=bundle or prior_bundle(), decisions=decisions)
    artifacts, result = finalize(bundle, momentum, debate, trade, policy, team)
    return {"decision_input": bundle, "momentum": momentum, "debate": debate, "intent": trade, "policy": policy,
            "decision": result, "team_inputs": team, **artifacts}


def prior_payload(run_id="decision-prior", artifacts=None):
    """build_prior_decision_context 需要的前次 run 身分檔（與 read_decision_run 回傳格式相同）。"""
    artifacts = artifacts or sealed_run()
    manifest = DecisionRepository(Path("/nonexistent")).build_manifest(run_id, artifacts)
    return {"run_id": run_id, "decision_input": artifacts["decision_input"], "decision": artifacts["decision"],
            "intent": artifacts["intent"], "manifest": manifest}


def integrated_world(bundle, prior, decisions=None):
    """2.1／2.2 整合鏈的 fixture：三份分析報告（event 2.2）、沒有獨立事件研究、team_inputs 附前次決策背景。"""
    momentum = MomentumEngine(bundle).run()
    unknown = [
        {"symbol": symbol, "outlook": "unknown", "findings": [], "data_gaps": ["NO_FINANCIAL_STATEMENTS"]}
        for symbol in ("2317.TW", "2330.TW")
    ]
    events = [{**item, "event_outlook": item["outlook"]} for item in event_items()]
    reports = {
        "technical": report(bundle, "technical", technical_items()),
        "fundamental": report(bundle, "fundamental", unknown),
        "event": seal_event_market_report(bundle, report_envelope(bundle, "event", "report-event"), events),
    }
    research = {}
    bull = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bull")["packet_envelope"], bull_items())
    bear = seal_stance_packet(build_stance_brief(bundle, momentum, reports, research, "bear")["packet_envelope"], bear_items())
    debate = build_research_debate(bundle, bull, bear, "debate-1")
    trade = seal_trade_decision(trade_decision_envelope(bundle, debate, "trade-1"), decisions or trade_items())
    base = base_policy()
    base["position_sizing"] = copy.deepcopy(SIZING)
    policy = apply_trade_decision(base, trade, STANCE, bundle)
    policy["content_sha256"] = decision_policy_sha256(policy)
    team = build_team_inputs(reports, None, STANCE, prior)
    return momentum, debate, trade, policy, team, reports


class BuildPriorDecisionContextTests(unittest.TestCase):
    def setUp(self):
        self.bundle = later_bundle()
        self.prior = prior_payload()

    def test_without_prior_run_is_unavailable_not_error(self):
        context = build_prior_decision_context(self.bundle, None)
        self.assertEqual(context["status"], "unavailable")
        self.assertEqual(context["reason"], "NO_PRIOR_DECISION_RUN")
        self.assertIsNone(context["source"])
        self.assertEqual(context["items"], [])
        self.assertEqual(context["uncovered_symbols"], ["2330.TW"])
        self.assertFalse(context["citable"])
        self.assertEqual(PriorDecisionContextValidator(self.bundle).validate(context), [])

    def test_prior_run_yields_held_symbol_items_and_cross_snapshot_flag(self):
        context = build_prior_decision_context(self.bundle, self.prior, selection="ledger_applied")
        self.assertEqual(context["status"], "available")
        self.assertTrue(context["cross_snapshot_exception"])
        self.assertFalse(context["same_snapshot"])
        self.assertEqual(context["source"]["run_id"], "decision-prior")
        self.assertEqual(context["source"]["decision_status"], "approved")
        self.assertEqual(context["source"]["target_session_start"], S1)
        self.assertEqual(context["source"]["selection"], "ledger_applied")
        self.assertEqual(context["source"]["intent_sha256"], self.prior["intent"]["content_sha256"])
        self.assertEqual(context["source"]["manifest_sha256"], self.prior["manifest"]["manifest_sha256"])
        # fixture 只持有 2330；前次對它的意圖是 hold。未持有的 2317 不進背景。
        self.assertEqual(context["items"], [{"symbol": "2330.TW", "intent": "hold", "conviction": None, "invalidation_conditions": []}])
        self.assertEqual(context["uncovered_symbols"], [])
        self.assertFalse(context["citable"])
        self.assertEqual(PriorDecisionContextValidator(self.bundle).validate(context), [])
        background, by_symbol = prior_decision_brief(context)
        self.assertNotIn("run_id", background["source"])
        self.assertFalse(background["citable"])
        self.assertEqual(by_symbol["2330.TW"]["intent"], "hold")

    def test_same_snapshot_prior_is_not_an_exception(self):
        context = build_prior_decision_context(later_bundle(snapshot_id="snapshot-decision-1"), self.prior)
        self.assertTrue(context["same_snapshot"])
        self.assertFalse(context["cross_snapshot_exception"])

    def test_prior_cutoff_must_be_earlier_and_timezone_aware(self):
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(prior_bundle(), self.prior)  # 同一 cutoff
        naive = copy.deepcopy(self.prior)
        naive["decision_input"]["decision_cutoff"] = "2026-09-20T00:55:00"
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(self.bundle, naive)

    def test_same_session_backup_is_refused_even_with_earlier_cutoff(self):
        # 21:00 備案（cutoff 較早）與 07:00 重做是同一時段；備案不能當前次決策。
        backup = prior_payload(artifacts=sealed_run(bundle=session_bundle("snapshot-backup", PRIOR_CUTOFF, S2)))
        with self.assertRaises(DecisionToolError) as raised:
            build_prior_decision_context(self.bundle, backup)
        self.assertIn("同一時段", str(raised.exception))
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(self.bundle, self.prior, selection="guess")
        without_session = tradable_bundle()
        without_session["decision_cutoff"] = LATER_CUTOFF
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(without_session, self.prior)

    def test_rejected_other_account_or_tampered_prior_is_refused(self):
        rejected = copy.deepcopy(self.prior)
        rejected["decision"]["status"] = "rejected"
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(self.bundle, rejected)
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(later_bundle(account_id="account-2"), self.prior)
        tampered = copy.deepcopy(self.prior)
        tampered["intent"]["items"][1]["intent"] = "exit"
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(self.bundle, tampered)
        mismatched = copy.deepcopy(self.prior)
        mismatched["manifest"]["run_id"] = "decision-other"
        with self.assertRaises(DecisionToolError):
            build_prior_decision_context(self.bundle, mismatched)


class PriorDecisionContextValidatorTests(unittest.TestCase):
    def setUp(self):
        self.bundle = later_bundle()
        self.context = build_prior_decision_context(self.bundle, prior_payload())
        self.validator = PriorDecisionContextValidator(self.bundle)

    def resealed(self, **changes):
        context = copy.deepcopy(self.context)
        context.update(changes)
        return seal_prior_decision_context(context)

    def test_tampered_content_hash_is_rejected(self):
        tampered = copy.deepcopy(self.context)
        tampered["items"][0]["intent"] = "add"
        tampered["items"][0]["conviction"] = "high"
        errors = self.validator.validate(tampered)
        self.assertTrue(any("content_sha256" in error for error in errors), errors)
        self.assertTrue(any("context_id" in error for error in errors), errors)

    def test_context_is_rebuilt_from_source_run_when_repository_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DecisionRepository(root).save("decision-prior", sealed_run(), day="2026-09-20")
            bundle = later_bundle()
            context = build_prior_decision_context(bundle, read_decision_run(root, "decision-prior"))
            validator = PriorDecisionContextValidator(bundle, root)
            self.assertEqual(validator.validate(context), [])
            forged = copy.deepcopy(context)
            forged["items"][0]["invalidation_conditions"] = ["改寫過的重估條件"]
            forged = seal_prior_decision_context(forged)
            self.assertEqual(PriorDecisionContextValidator(bundle).validate(forged), [])  # 只驗自身一致時看不出來
            self.assertTrue(any("來源 run" in error for error in validator.validate(forged)))
            missing = copy.deepcopy(context)
            missing["source"]["run_id"] = "decision-gone"
            self.assertTrue(validator.validate(seal_prior_decision_context(missing)))

    def test_resealed_violations_are_rejected(self):
        source = self.context["source"]
        cases = {
            "citable": self.resealed(citable=True),
            "不是目前持股": self.resealed(items=[*self.context["items"], {"symbol": "2317.TW", "intent": "buy", "conviction": "low", "invalidation_conditions": []}]),
            "必須早於本次 cutoff": self.resealed(source={**source, "decision_cutoff": LATER_CUTOFF}),
            "必須早於本次目標交易時段": self.resealed(source={**source, "target_session_start": S2}),
            "selection": self.resealed(source={**source, "selection": "guess"}),
            "含未允許欄位": self.resealed(weight="0.1"),
            "same_snapshot": self.resealed(same_snapshot=True),
            "涵蓋目前持股": self.resealed(items=[], uncovered_symbols=[]),
            "與本次 bundle 不一致": self.resealed(snapshot_id="snapshot-other"),
            "schema_version": self.resealed(schema_version="9.9"),
        }
        for expected, context in cases.items():
            with self.subTest(expected=expected):
                errors = self.validator.validate(context)
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_unavailable_shape_is_enforced(self):
        unavailable = build_prior_decision_context(self.bundle, None)
        self.assertEqual(self.validator.validate(unavailable), [])
        with_source = seal_prior_decision_context({**unavailable, "source": self.context["source"]})
        self.assertTrue(any("不得有來源" in error for error in self.validator.validate(with_source)))
        no_reason = seal_prior_decision_context({**unavailable, "reason": ""})
        self.assertTrue(any("reason" in error for error in self.validator.validate(no_reason)))
        self.assertEqual(self.validator.validate("not-a-mapping"), ["PriorDecisionContext 必須是物件"])


class FindPriorDecisionRunTests(unittest.TestCase):
    """排程：每個時段有前一晚 21:00 備案與當天 07:00 重做；前次決策必須是更早時段真正交出去的 run。"""

    def save(self, root, run_id, snapshot_id, cutoff, session, **changes):
        artifacts = sealed_run(bundle=session_bundle(snapshot_id, cutoff, session))
        for name, value in changes.items():
            artifacts[name].update(value)
        DecisionRepository(root).save(run_id, artifacts, day="2026-09-20")

    def populate(self, root):
        # fixture 行情只到 09-19，cutoff 全部落在 09-20 當天，只靠先後順序與目標時段表達「21:00 備案／07:00 重做」。
        self.save(root, "d1-s1-backup", "snap-1", "2026-09-20T01:00:00+00:00", S1)   # S1 的 21:00 備案
        self.save(root, "d2-s1-redo", "snap-2", "2026-09-20T02:00:00+00:00", S1)     # S1 的 07:00 重做，帳本結算的是它
        self.save(root, "d3-s2-backup", "snap-3", "2026-09-20T03:00:00+00:00", S2)   # S2 的 21:00 備案

    def test_morning_redo_skips_same_session_backup(self):
        current = session_bundle("snap-current", "2026-09-20T04:00:00+00:00", S2)  # S2 的 07:00 重做
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(find_prior_decision_run(root, current))
            self.populate(root)
            # 同時段的 d3 備案 cutoff 最新但不算；S1 的兩個 run 中帳本記錄的 d2 優先。
            self.assertEqual(find_prior_decision_run(root, current, ["d2-s1-redo"]), {"run_id": "d2-s1-redo", "selection": "ledger_applied"})
            # 沒有帳本記錄時取 S1 時段 cutoff 最新者，仍是 d2。
            self.assertEqual(find_prior_decision_run(root, current), {"run_id": "d2-s1-redo", "selection": "latest_cutoff"})
            # 帳本若記錄的是備案（例如早上重做失敗、還原後交了備案），就取備案，即使 cutoff 較早。
            self.assertEqual(find_prior_decision_run(root, current, ["d1-s1-backup"]), {"run_id": "d1-s1-backup", "selection": "ledger_applied"})
            prior = read_decision_run(root, "d2-s1-redo")
            self.assertEqual(set(prior), {"run_id", "decision_input", "decision", "intent", "manifest"})
            context = build_prior_decision_context(current, prior, selection="ledger_applied")
            self.assertEqual(context["source"]["target_session_start"], S1)
            self.assertEqual(PriorDecisionContextValidator(current).validate(context), [])

    def test_next_evening_backup_takes_the_delivered_run_of_the_previous_session(self):
        current = session_bundle("snap-current", "2026-09-20T05:00:00+00:00", S3)  # S3 的 21:00 備案
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.populate(root)
            self.save(root, "d4-s2-redo", "snap-4", "2026-09-20T04:30:00+00:00", S2)  # S2 的 07:00 重做
            self.assertEqual(find_prior_decision_run(root, current, ["d2-s1-redo", "d4-s2-redo"]), {"run_id": "d4-s2-redo", "selection": "ledger_applied"})
            self.assertEqual(find_prior_decision_run(root, current), {"run_id": "d4-s2-redo", "selection": "latest_cutoff"})
            self.assertEqual(find_prior_decision_run(root, current, ["d3-s2-backup"]), {"run_id": "d3-s2-backup", "selection": "ledger_applied"})

    def test_filters_status_account_and_future_sessions(self):
        current = session_bundle("snap-current", "2026-09-20T04:00:00+00:00", S2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, "rejected", "snap-r", "2026-09-20T02:00:00+00:00", S1, decision={"status": "rejected"})
            self.save(root, "later-session", "snap-l", "2026-09-20T02:30:00+00:00", S3)
            other = sealed_run(bundle=session_bundle("snap-o", "2026-09-20T02:40:00+00:00", S1, account_id="account-2"))
            DecisionRepository(root).save("other-account", other, day="2026-09-20")
            self.assertIsNone(find_prior_decision_run(root, current))
            self.save(root, "ok", "snap-ok", "2026-09-20T01:30:00+00:00", S1)
            self.assertEqual(find_prior_decision_run(root, current)["run_id"], "ok")

    def test_ambiguous_choices_are_refused(self):
        current = session_bundle("snap-current", "2026-09-20T04:00:00+00:00", S2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.save(root, "twin-a", "snap-a", PRIOR_CUTOFF, S1)
            self.save(root, "twin-b", "snap-b", PRIOR_CUTOFF, S1)
            with self.assertRaises(DecisionToolError):
                find_prior_decision_run(root, current)  # 同時段同 cutoff，無帳本記錄
            with self.assertRaises(DecisionToolError):
                find_prior_decision_run(root, current, ["twin-a", "twin-b"])  # 帳本記錄兩個
            self.assertEqual(find_prior_decision_run(root, current, ["twin-b"])["run_id"], "twin-b")
        with self.assertRaises(DecisionToolError):
            find_prior_decision_run(Path("/nonexistent"), tradable_bundle())  # 本次沒有目標時段

    def test_read_decision_run_requires_valid_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            DecisionRepository(root).save("decision-a", sealed_run(), day="2026-09-20")
            run_dir = DecisionRepository(root).verify("decision-a")
            (run_dir / "intent.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(DecisionToolError):
                read_decision_run(root, "decision-a")


class TeamInputs22Tests(unittest.TestCase):
    def setUp(self):
        self.bundle = later_bundle()
        self.prior = build_prior_decision_context(self.bundle, prior_payload())

    def world(self, decisions=None, prior=None):
        bundle = self.bundle
        momentum, debate, trade, policy, team, reports = integrated_world(bundle, prior or self.prior, decisions)
        return bundle, momentum, debate, trade, policy, team, reports

    def test_build_team_inputs_versions(self):
        _, _, _, _, _, team, reports = self.world()
        self.assertEqual(build_team_inputs(reports, None, STANCE)["schema_version"], "2.1")
        self.assertEqual(team["schema_version"], "2.2")
        self.assertEqual(set(team), {"schema_version", "analyst_reports", "cash_stance", "prior_decision_context"})
        with self.assertRaises(DecisionToolError):
            build_team_inputs(reports, {"status": "degraded"}, STANCE, self.prior)

    def test_finalizer_accepts_2_2_and_binds_hash(self):
        for prior in (self.prior, build_prior_decision_context(self.bundle, None)):
            bundle, momentum, debate, trade, policy, team, _ = self.world(prior=prior)
            self.assertEqual(team["prior_decision_context"]["status"], prior["status"])
            _, result = finalize(bundle, momentum, debate, trade, policy, team)
            self.assertEqual(result["status"], "approved", result["errors"])
            self.assertEqual(result["team_inputs_sha256"], canonical_sha256(team))

    def test_legacy_2_0_world_still_validates(self):
        bundle, momentum, debate, trade, policy, team, _, _ = team_world()
        self.assertEqual(team["schema_version"], "2.0")
        _, result = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "approved", result["errors"])

    def test_finalizer_rejects_tampered_or_missing_prior_context(self):
        bundle, momentum, debate, trade, policy, team, _ = self.world()
        tampered = copy.deepcopy(team)
        tampered["prior_decision_context"]["items"][0]["intent"] = "add"
        _, result = finalize(bundle, momentum, debate, trade, policy, tampered)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("prior_decision_context" in error for error in result["errors"]), result["errors"])
        missing = {**team}
        missing.pop("prior_decision_context")
        _, result = finalize(bundle, momentum, debate, trade, policy, missing)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("欄位必須剛好是" in error for error in result["errors"]), result["errors"])
        unknown_version = {**team, "schema_version": "2.3"}
        _, result = finalize(bundle, momentum, debate, trade, policy, unknown_version)
        self.assertTrue(any("schema_version" in error for error in result["errors"]), result["errors"])

    def test_trader_text_citing_prior_background_is_rejected(self):
        citing = trade_items()
        citing[1]["rationale"] = "前次 decision-prior 已決定續抱，維持不變。"
        bundle, momentum, debate, trade, policy, team, _ = self.world(decisions=citing)
        errors = TradeDecisionValidator(bundle, momentum, debate, prior_context=self.prior).validate(trade)
        self.assertTrue(any("不得引用前次決策背景" in error and "decision-prior" in error for error in errors), errors)
        self.assertEqual(TradeDecisionValidator(bundle, momentum, debate).validate(trade), [])
        _, result = finalize(bundle, momentum, debate, trade, policy, team)
        self.assertEqual(result["status"], "rejected")
        self.assertTrue(any("不得引用前次決策背景" in error for error in result["errors"]), result["errors"])
        keyed = trade_items()
        keyed[0]["invalidation_conditions"] = ["prior_decision 的條件出現時重估。"]
        _, _, debate2, trade2, _, _, _ = self.world(decisions=keyed)
        self.assertTrue(TradeDecisionValidator(bundle, momentum, debate2, prior_context=self.prior).validate(trade2))


class PipelinePriorDecisionTests(unittest.TestCase):
    def test_load_prior_decision_context_reads_repository_and_resumes(self):
        bundle = later_bundle()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            DecisionRepository(repo).save("decision-prior", sealed_run(), day="2026-09-20")
            logs = []
            pipeline = DailyDecisionPipeline(ROOT, root / "run", FakeRunner({}), repo, log=logs.append)
            (root / "run").mkdir()
            context = pipeline.load_prior_decision_context(bundle, ["decision-prior"])
            self.assertEqual(context["status"], "available")
            self.assertEqual(context["source"]["run_id"], "decision-prior")
            self.assertEqual(context["source"]["selection"], "ledger_applied")
            saved = json.loads((root / "run" / "prior_decision_context.json").read_text(encoding="utf-8"))
            self.assertEqual(saved, context)
            self.assertTrue(any("跨 Snapshot 例外" in line and "ledger_applied" in line for line in logs))

            # 續跑時沿用已存檔版本，即使決策庫之後多了同時段更新的 run。
            newer = sealed_run(bundle=session_bundle("snapshot-mid", "2026-09-20T12:00:00+00:00", S1))
            DecisionRepository(repo).save("decision-newer", newer, day="2026-09-20")
            resumed = DailyDecisionPipeline(ROOT, root / "run", FakeRunner({}), repo, log=logs.append, resume=True)
            self.assertEqual(resumed.load_prior_decision_context(bundle), context)
            fresh = DailyDecisionPipeline(ROOT, root / "run2", FakeRunner({}), repo, log=logs.append)
            (root / "run2").mkdir()
            source = fresh.load_prior_decision_context(bundle)["source"]
            self.assertEqual((source["run_id"], source["selection"]), ("decision-newer", "latest_cutoff"))

            # 本次沒有目標交易時段：無法比較時段，背景為 unavailable，不是錯誤。
            no_session = DailyDecisionPipeline(ROOT, root / "run3", FakeRunner({}), repo, log=logs.append)
            (root / "run3").mkdir()
            context = no_session.load_prior_decision_context(tradable_bundle())
            self.assertEqual((context["status"], context["reason"]), ("unavailable", "NO_TARGET_SESSION"))

    def test_trader_brief_carries_background_and_validator_blocks_citation(self):
        bundle = later_bundle()
        prior = build_prior_decision_context(bundle, prior_payload())
        momentum, debate, _, _, _, reports = integrated_world(bundle, prior)
        citing = trade_items()
        citing[1]["unresolved_questions"] = ["decision-prior 的重估條件是否出現？"]
        runner = FakeRunner({"trader_b0": [{"items": citing}, {"items": trade_items()}]})
        with tempfile.TemporaryDirectory() as directory:
            pipeline = DailyDecisionPipeline(ROOT, Path(directory), runner, Path(directory) / "repo", log=lambda _: None)
            trade = pipeline.run_trader(bundle, momentum, reports, debate, "run-1", prior)
            brief = json.loads((Path(directory) / "brief_trader_b0.json").read_text(encoding="utf-8"))

        self.assertEqual([item["intent"] for item in trade["items"]], ["buy", "hold"])
        self.assertEqual(len(runner.prompts), 2)
        self.assertIn("prior_decision_background", runner.prompts[0][1])
        self.assertIn("不得引用前次決策背景", runner.prompts[1][1])
        self.assertEqual(brief["prior_decision_background"]["status"], "available")
        self.assertFalse(brief["prior_decision_background"]["citable"])
        self.assertNotIn("run_id", brief["prior_decision_background"]["source"])
        by_symbol = {row["symbol"]: row.get("prior_decision") for row in brief["symbols"]}
        self.assertEqual(by_symbol["2330.TW"]["intent"], "hold")
        self.assertIsNone(by_symbol["2317.TW"])


if __name__ == "__main__":
    unittest.main()
