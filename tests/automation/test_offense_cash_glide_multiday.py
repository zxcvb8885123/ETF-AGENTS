"""offense.cash_glide v1（已開啟）的跨日完整流程測試。

單元測試只驗單日規則；這裡用真實 daily pipeline 與真實 Finalizer 連跑多個交易日，證明「前次 run 放寬現金 → 隔日只能回升 max_daily_raise → 之後回到格點」：

- 樣板是 config/decision_policy.json 原樣（只在記憶體內把 entry_reserve.max_entry_weight 調成 fixture 買得到整張的 0.2，與 test_offense_pipeline 相同）。
- 每一天是獨立的 ``PipelineWorld``（同一個決策庫 repository，cutoff 逐日往後一個交易日，目標交易時段逐日往後），pipeline 以
  ``applied_decision_run_ids`` 讓前一天的 run 成為「帳本已套用的前次 run」，前次現金比例 anchor 與 ``prior_offense_loosened`` 都由 pipeline 從決策庫算出。
- 第 1 天風險 Agent 填 defensive／lower＋probe（現金 0.15 → 0.10）或 start_rebuild（→ 0.08）；第 2 天起填 keep_defensive。
  probe 鏈：0.10 → 0.13 → 0.15；start_rebuild 鏈：0.08 → 0.11 → 0.14 → 0.15。
- 不滑行的條件（方向是多留現金，照格點 0.15）各一個測試：今日姿態非 defensive、前次沒放寬、回撤達 offense_off_drawdown、回撤下限 defensive、
  帳本回撤歷史不可用；前次 run 的 policy 讀不到或雜湊不符時 anchor 建立拒絕（Agent 不被呼叫）。
- 沒有 cash_glide 欄位的對照組：隔日現金目標直接 0.15、anchor 沒有 ``prior_offense_loosened`` 鍵。
- 減碼：帳戶現金低於目標時 drawdown_derisk 賣到「當日」現金目標（0.13）而不是 0.15，賣的股數少於對照組。

沒有走真實 pipeline 的部分：前次 run 的「帳本已套用」只以 ``applied_decision_run_ids`` 參數表示（沒有真實帳本）；帳本 NAV／持股歷史是測試自建的列。
"""

import copy
import json
import shutil
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, _history_provider
from etf_agent.decision import DecisionFinalizer, DecisionRepository, DecisionToolError, decision_policy_sha256
from etf_agent.perception.market_fear_greed.service import _store

import test_daily_pipeline as daily
from test_daily_pipeline import FakeRunner, PipelineWorld
from test_market_fear_greed import approvals, captures
from test_offense_pipeline import FearGreedHarness, probe_outputs
from test_quality_dip_ruling_pipeline import live_like_template, team_outputs
from test_swap_nomination_pipeline import HOLDINGS, NAV, ROOT

# 逐日 cutoff：第 1 天是 fixture 原本的週日早上（目標時段週一），之後每天收盤後（目標時段為下一個交易日）。
C1, C2, C3, C4 = (
    "2026-09-20T00:55:00+00:00", "2026-09-21T10:00:00+00:00", "2026-09-22T10:00:00+00:00", "2026-09-23T10:00:00+00:00")
ARTIFACTS = ("decision_input", "policy", "momentum", "debate", "intent", "team_inputs", "proposal", "scenario",
             "guard", "risk_review", "revision_history", "decision")
RECONCILIATION = "防守格點照舊，但賣壓竭盡與廣度回升，小量試單，現金目標只放寬一格。"
LOW_CASH = {"cash": "1000000", "settled_cash": "1000000", "nav": "9200000", "shares_2330": 50000}  # 行情 2330=164：持股 820 萬


def live_template(glide=True):
    """正式設定原樣；只在記憶體內調 fixture 需要的 entry_reserve.max_entry_weight（glide=False 時另拿掉 cash_glide 當對照組）。"""
    value = live_like_template("v4")
    value["position_sizing"]["agent_swap"]["entry_reserve"]["max_entry_weight"] = "0.2"
    if not glide:
        value["position_sizing"]["offense"].pop("cash_glide")
    return value


def stance_outputs(level="defensive", grade="lower", view="keep_defensive", **fields):
    out = team_outputs("v4")
    out["cash_stance"] = [dict(out["cash_stance"][0], level=level, cash_grade=grade, exposure_view=view, **fields)]
    return out


def peak_nav(drawdown, current="1164000"):
    """讓目前 NAV 相對峰值的回撤剛好約 ``drawdown`` 的帳本歷史（峰值在第一列）。"""
    peak = int((Decimal(current) / (Decimal(1) - Decimal(drawdown))).to_integral_value())
    return [dict(NAV[0], nav=str(peak)), dict(NAV[1], nav="1161000")]


class Day:
    """一個交易日的 pipeline 世界、結果與封存 artifacts。"""

    def __init__(self, directory, repository, cutoff, run_id, outputs, applied=(), template=None, nav=NAV, fear_greed=None,
                 account=None, tolerate_failure=False):
        self.repository, self.run_id, self.nav = Path(repository), run_id, nav
        real = daily.builder_inputs

        def shifted():
            snapshot, rules, history, account_row = real()
            snapshot["decision_cutoff"] = cutoff
            account_row["valuation_at"] = cutoff
            return snapshot, rules, history, account_row

        with patch.object(daily, "builder_inputs", shifted):
            self.world = PipelineWorld(Path(directory), approved_status=True, template=template if template is not None else live_template())
        self.world.repository = self.repository
        row = json.loads(self.world.paths["account"].read_text(encoding="utf-8"))
        row.update(cash="1000000", settled_cash="1000000", nav="1164000")
        rules = json.loads(self.world.paths["rules"].read_text(encoding="utf-8"))
        rules["max_positions"] = 1  # 名額卡住，換股提名才會被呼叫（與 Harness3 相同）
        if account:
            row.update(cash=account["cash"], settled_cash=account["cash"], nav=account["nav"])
            row["positions"] = [dict(position, shares=account["shares_2330"]) for position in row["positions"]]
            rules.update(max_stock_weight="0.95", special_weight_limits={"2330.TW": "0.95"}, max_sector_weight="0.95")
        self.world.paths["account"].write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        self.world.paths["rules"].write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
        self.runner = runner = FakeRunner(outputs)
        pipeline = DailyDecisionPipeline(ROOT, self.world.run_dir, runner, self.repository, log=lambda _: None, sleep=lambda _: None)
        self.error = None
        kwargs = {"fear_greed_root": fear_greed[0], "fear_greed_approvals_path": fear_greed[1]} if fear_greed else {}
        try:
            self.result = pipeline.run(
                self.world.paths["snapshot"], self.world.paths["account"], self.world.paths["trading_status"], self.world.paths["rules"],
                self.world.paths["database"], self.world.paths["template"], self.world.paths["sectors"], run_id,
                nav_history=nav, holdings_history=HOLDINGS, applied_decision_run_ids=list(applied), **kwargs)
        except (DecisionToolError, DailyPipelineError) as error:
            if not tolerate_failure:
                raise
            self.result, self.error = None, error
        self.art = self.load() if self.result is not None and self.result.status == "completed" else {}

    def load(self, repository=None):
        run_dir = DecisionRepository(repository or self.repository).verify(self.run_id)
        return {name: json.loads((run_dir / ("%s.json" % name)).read_text(encoding="utf-8")) for name in ARTIFACTS}

    # -- 取值
    @property
    def sizing(self):
        return self.art["policy"]["position_sizing"]

    @property
    def offense(self):
        return self.sizing["offense"]

    @property
    def anchor(self):
        return self.sizing["cash_ratio_range"]["anchor"]

    @property
    def buffer(self):
        return Decimal(self.art["proposal"]["effective_policy"]["cash_buffer_rate"])

    def sell_shares(self):
        return sum(order["shares"] for order in self.art["decision"]["orders"] if order["side"] == "sell")

    # -- Finalizer 重建
    def rebuild_errors(self, repository=None, **replace):
        art = dict(self.art, **replace)
        provider = _history_provider(self.nav, HOLDINGS)
        try:
            result = DecisionFinalizer(
                art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
                repository or self.repository, provider,
            ).run(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"])
        except (DecisionToolError, DailyPipelineError) as error:
            return ["raised: %s" % error]
        return result["errors"] if result["status"] == "rejected" else []


def sealed(policy, mutate):
    value = copy.deepcopy(policy)
    mutate(value)
    value["content_sha256"] = decision_policy_sha256(value)
    return value


class MultiDayCase(unittest.TestCase):
    """每個測試類別一個暫存目錄；``day`` 逐日建立獨立 PipelineWorld。"""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.base = Path(cls.directory.name)
        cls.count = 0
        cls.fear_greed = (cls.base / "fg", cls.base / "fg.json")
        _store(cls.fear_greed[0]).save("fear-greed-20260919T120000Z-fixture", {"captures": {"items": captures()}})
        cls.fear_greed[1].write_text(json.dumps({"sources": approvals()}), encoding="utf-8")
        cls.evidence = cls.market_evidence()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @classmethod
    def market_evidence(cls):
        """恐懼貪婪資料包的證據編號（非新聞的市場層級證據；probe／start_rebuild 需要兩個市場層級證據）。"""
        harness = FearGreedHarness(cls.sub("evidence"), cls.fear_greed)
        outs = team_outputs("v4")
        outs["cash_stance"] = [dict(outs["cash_stance"][0], offense_basis=[], failure_condition="", defensive_reconciliation="")]
        harness.run(FakeRunner(outs))
        bundle = json.loads((harness.world.run_dir / "decision_input.json").read_text(encoding="utf-8"))
        return bundle["market_fear_greed_input"]["result"]["components"][0]["evidence_id"]

    @classmethod
    def sub(cls, name):
        cls.count += 1
        path = cls.base / ("%s-%d" % (name, cls.count))
        path.mkdir()
        return path

    @classmethod
    def day(cls, repository, cutoff, run_id, outputs, **kwargs):
        return Day(cls.sub(run_id), repository, cutoff, run_id, outputs, **kwargs)

    @classmethod
    def first_day(cls, repository, view, template=None, run_id="day1"):
        """第 1 天：defensive／lower＋probe 或 start_rebuild，使 offense 放寬現金。"""
        if view == "probe":
            outs = probe_outputs(cls.evidence, level="defensive", cash_grade="lower", defensive_reconciliation=RECONCILIATION)
        else:
            outs = probe_outputs(cls.evidence, level="defensive", cash_grade="lower", exposure_view=view, turn_assessment="turn_unconfirmed",
                                 offense_basis=[], failure_condition="", defensive_reconciliation=RECONCILIATION)
        return cls.day(repository, C1, run_id, outs, fear_greed=cls.fear_greed, template=template)

    @classmethod
    def copy_repository(cls, repository, name):
        target = cls.sub(name) / "decisions"
        shutil.copytree(repository, target)
        return target


class GlideChainTests(MultiDayCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # probe 鏈：0.15 → 0.10（第 1 天放寬）→ 0.13 → 0.15
        cls.repo = cls.base / "probe-repo"
        cls.d1 = cls.first_day(cls.repo, "probe")
        cls.d2 = cls.day(cls.repo, C2, "day2", stance_outputs(), applied=["day1"])
        cls.d3 = cls.day(cls.repo, C3, "day3", stance_outputs(), applied=["day2"])
        # start_rebuild 鏈：0.15 → 0.08 → 0.11 → 0.14 → 0.15
        cls.sr_repo = cls.base / "sr-repo"
        cls.s1 = cls.first_day(cls.sr_repo, "start_rebuild")
        cls.s2 = cls.day(cls.sr_repo, C2, "day2", stance_outputs(), applied=["day1"])
        cls.s3 = cls.day(cls.sr_repo, C3, "day3", stance_outputs(), applied=["day2"])
        cls.s4 = cls.day(cls.sr_repo, C4, "day4", stance_outputs(), applied=["day3"])

    def test_every_day_completes_and_is_rebuilt_clean_by_the_finalizer(self):
        for label, day in (("p1", self.d1), ("p2", self.d2), ("p3", self.d3), ("s1", self.s1), ("s2", self.s2), ("s3", self.s3), ("s4", self.s4)):
            self.assertEqual(day.result.status, "completed", (label, day.result.errors))
            self.assertEqual(day.rebuild_errors(), [], label)

    def test_day_1_loosens_the_cash_target_and_has_no_prior_run(self):
        self.assertEqual((self.d1.offense["effective_view"], self.d1.offense["cash_loosening"]), ("probe", {"from": "0.15", "to": "0.10"}))
        self.assertEqual(self.d1.buffer, Decimal("0.10"))
        self.assertEqual(self.d1.anchor["status"], "account_cash")
        self.assertIsNone(self.d1.offense["cash_raise_limited"])
        self.assertEqual((self.s1.offense["effective_view"], self.s1.offense["cash_loosening"]), ("start_rebuild", {"from": "0.15", "to": "0.08"}))
        self.assertEqual(self.s1.buffer, Decimal("0.08"))

    def test_probe_day_2_anchor_flag_and_cash_target_are_limited_to_anchor_plus_max_daily_raise(self):
        d2 = self.d2
        self.assertEqual((d2.anchor["status"], d2.anchor["ratio"], d2.anchor["source"]["run_id"], d2.anchor["source"]["selection"]),
                         ("prior_decision", "0.10000000", "day1", "ledger_applied"))
        self.assertIs(d2.anchor["prior_offense_loosened"], True)
        self.assertEqual((d2.offense["view"], d2.offense["effective_view"], d2.offense["cash_loosening"]), ("keep_defensive", "keep_defensive", None))
        self.assertEqual(d2.offense["cash_raise_limited"], {"from": "0.15", "to": "0.13"})
        self.assertEqual(d2.buffer, Decimal("0.13"))
        self.assertEqual(Decimal(d2.art["policy"]["cash_buffer_rate"]), Decimal("0.13"))
        self.assertEqual(d2.buffer, min(Decimal("0.15"), Decimal(d2.anchor["ratio"]) + Decimal("0.03")))
        self.assertGreater(d2.buffer, Decimal(self.d1.buffer))  # 放寬後比例 0.10 以下不會更低

    def test_probe_day_3_returns_to_the_grid_point(self):
        d3 = self.d3
        self.assertEqual((d3.anchor["ratio"], d3.anchor["source"]["run_id"]), ("0.13000000", "day2"))
        self.assertIs(d3.anchor["prior_offense_loosened"], True)  # 前次 run 有 cash_raise_limited 紀錄也算放寬過
        self.assertEqual(d3.buffer, Decimal("0.15"))
        self.assertIsNone(d3.offense["cash_raise_limited"])  # 0.15 ≤ 0.13 + 0.03：不需要限制

    def test_start_rebuild_chain_glides_0_08_0_11_0_14_0_15(self):
        self.assertEqual([day.buffer for day in (self.s1, self.s2, self.s3, self.s4)],
                         [Decimal("0.08"), Decimal("0.11"), Decimal("0.14"), Decimal("0.15")])
        self.assertEqual(self.s2.offense["cash_raise_limited"], {"from": "0.15", "to": "0.11"})
        self.assertEqual(self.s3.offense["cash_raise_limited"], {"from": "0.15", "to": "0.14"})
        self.assertIsNone(self.s4.offense["cash_raise_limited"])
        for day in (self.s2, self.s3, self.s4):
            self.assertIs(day.anchor["prior_offense_loosened"], True)
        self.assertEqual([self.s2.anchor["ratio"], self.s3.anchor["ratio"], self.s4.anchor["ratio"]], ["0.08000000", "0.11000000", "0.14000000"])

    def test_risk_agent_brief_only_mentions_the_ceiling_when_the_prior_run_loosened(self):
        brief = json.loads((self.d2.world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8"))
        text = json.dumps(brief, ensure_ascii=False)
        self.assertIn("offense_cash_glide", text)
        self.assertEqual(brief["offense_cash_glide"]["max_cash_target_today"], "0.13")
        self.assertFalse(brief["offense_cash_glide"]["citable"])
        day1_brief = (self.d1.world.run_dir / "brief_cash_stance.json").read_text(encoding="utf-8")
        self.assertNotIn("offense_cash_glide", day1_brief)

    # -- fail-closed：竄改被 Finalizer 拒絕
    def test_forged_cash_raise_limited_or_cash_buffer_is_rejected(self):
        d2 = self.d2
        mutations = {
            "拿掉現金回升限制": lambda p: p["position_sizing"]["offense"].update(cash_raise_limited=None),
            "回升限制的 to 改大": lambda p: p["position_sizing"]["offense"].update(cash_raise_limited={"from": "0.15", "to": "0.14"}),
            "回升限制的 from 偽造": lambda p: p["position_sizing"]["offense"].update(cash_raise_limited={"from": "0.18", "to": "0.13"}),
            "現金比例改回格點但保留紀錄": lambda p: p.update(cash_buffer_rate="0.15"),
            "現金比例改回格點且拿掉紀錄": lambda p: (p.update(cash_buffer_rate="0.15"), p["position_sizing"]["offense"].update(cash_raise_limited=None)),
            "anchor 偽造沒放寬過": lambda p: (p["position_sizing"]["cash_ratio_range"]["anchor"].update(prior_offense_loosened=False),
                                              p["position_sizing"]["offense"].update(cash_raise_limited=None), p.update(cash_buffer_rate="0.15")),
            "拿掉 cash_glide 欄位": lambda p: p["position_sizing"]["offense"].pop("cash_glide"),
        }
        for label, mutate in mutations.items():
            self.assertTrue(d2.rebuild_errors(policy=sealed(d2.art["policy"], mutate)), label)

    def test_forged_effective_policy_cash_buffer_in_the_proposal_is_rejected(self):
        proposal = copy.deepcopy(self.d2.art["proposal"])
        proposal["effective_policy"]["cash_buffer_rate"] = "0.15"
        self.assertTrue(self.d2.rebuild_errors(proposal=proposal))

    def test_a_prior_run_with_a_tampered_policy_is_rejected_on_rebuild(self):
        repository = self.copy_repository(self.repo, "tampered-prior")
        policy_path = DecisionRepository(repository).verify("day1") / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["position_sizing"]["offense"]["cash_loosening"] = None  # 前次 run 的放寬紀錄被拿掉（雜湊不再相符）
        policy_path.write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")
        self.assertTrue(self.d2.rebuild_errors(repository=repository))
        untouched = self.copy_repository(self.repo, "untouched-prior")
        self.assertEqual(self.d2.rebuild_errors(repository=untouched), [])  # 對照：未竄改的副本重建乾淨

    def test_a_prior_run_without_a_policy_file_is_rejected_on_rebuild(self):
        repository = self.copy_repository(self.repo, "missing-prior-policy")
        (DecisionRepository(repository).verify("day1") / "policy.json").unlink()
        self.assertTrue(self.d2.rebuild_errors(repository=repository))


class NoGlideConditionTests(MultiDayCase):
    """每個不滑行的條件各一個：方向是多留現金，照格點 0.15（或今日姿態自己的格點）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.base / "repo"
        cls.d1 = cls.first_day(cls.repo, "probe")
        cls.unloosened_repo = cls.base / "unloosened"
        cls.u1 = cls.day(cls.unloosened_repo, C1, "day1", stance_outputs())  # 前次 keep_defensive：沒放寬

    def day2(self, name, repository=None, outputs=None, **kwargs):
        return self.day(repository or self.repo, C2, name, outputs or stance_outputs(), applied=["day1"], **kwargs)

    def assert_grid_point(self, day, expected="0.15"):
        self.assertEqual(day.result.status, "completed", day.result.errors)
        self.assertEqual(day.anchor["status"], "prior_decision")
        self.assertIsNone(day.offense["cash_raise_limited"])
        self.assertEqual(day.buffer, Decimal(expected))
        self.assertEqual(day.rebuild_errors(), [])

    def test_today_stance_not_defensive_follows_the_grid(self):
        day = self.day2("neutral", outputs=stance_outputs("neutral", "middle"))
        self.assert_grid_point(day, "0.10")  # neutral／middle 格點：不被 anchor 0.10＋0.03 以外的規則動到
        self.assertIs(day.anchor["prior_offense_loosened"], True)

    def test_prior_run_without_loosening_follows_the_grid(self):
        self.assertEqual(self.u1.buffer, Decimal("0.15"))
        self.assertIsNone(self.u1.offense["cash_loosening"])
        day = self.day2("after-unloosened", repository=self.unloosened_repo)
        self.assert_grid_point(day)
        self.assertIs(day.anchor["prior_offense_loosened"], False)

    def test_drawdown_at_the_off_line_follows_the_grid(self):
        day = self.day2("dd-off", nav=peak_nav("0.13"))  # 0.12 ≤ 回撤 < 0.15：回撤下限 neutral、但達 offense_off_drawdown
        self.assertEqual(day.result.status, "completed", day.result.errors)
        control = day.art["policy"]["drawdown_control"]
        self.assertEqual(control["status"], "available")
        self.assertNotEqual(control["stance_floor"], "defensive")
        self.assertGreaterEqual(Decimal(control["drawdown"]), Decimal("0.12"))
        self.assert_grid_point(day)
        # 對照：回撤低於停用線時同一個前次 run 會滑行
        self.assertEqual(self.day2("dd-ok", nav=peak_nav("0.06")).buffer, Decimal("0.13"))

    def test_drawdown_floor_defensive_follows_the_grid(self):
        day = self.day2("dd-floor", nav=peak_nav("0.20"))
        self.assertEqual(day.result.status, "completed", day.result.errors)
        self.assertEqual(day.art["policy"]["drawdown_control"]["stance_floor"], "defensive")
        self.assert_grid_point(day)

    def test_unavailable_ledger_drawdown_is_refused_before_any_agent_so_glide_never_sees_it(self):
        # 正式樣板的 exposure_rotation／drawdown_derisk 需要 history_ref：沒有帳本 NAV 歷史時整個 pipeline 在 Agent 呼叫前拒絕，
        # 所以 cash_glide 的「帳本回撤不可用 → 不滑行」在 pipeline 層不會發生（fail-closed 更早）；該條件由 test_offense_extensions 單元測試覆蓋。
        day = self.day2("dd-unavailable", nav=None, tolerate_failure=True)
        self.assertTrue(day.result is None or day.result.status != "completed")
        self.assertEqual(day.runner.prompts, [])
        self.assertEqual(day.art, {})

    def test_prior_run_with_unreadable_or_mismatched_policy_stops_before_any_cash_agent(self):
        for label, tamper in {
            "policy 內容被改（雜湊不符）": lambda path: path.write_text(
                path.read_text(encoding="utf-8").replace('"cash_loosening"', '"cash_loosening_x"', 1), encoding="utf-8"),
            "policy 讀不到": lambda path: path.unlink(),
        }.items():
            repository = self.copy_repository(self.repo, "broken-prior")
            tamper(DecisionRepository(repository).verify("day1") / "policy.json")
            day = self.day2("broken-" + str(self.count), repository=repository, tolerate_failure=True)
            self.assertTrue(day.result is None or day.result.status != "completed", label)
            self.assertNotIn("cash_stance", [name for name, _ in day.runner.prompts], label)  # anchor 建立在風險 Agent 之前就拒絕
            self.assertEqual(day.art, {}, label)

    def test_a_policy_without_the_glide_field_is_bitwise_the_old_behaviour(self):
        control_repo = self.base / "control-repo"
        c1 = self.first_day(control_repo, "probe", template=live_template(glide=False))
        c2 = self.day(control_repo, C2, "day2", stance_outputs(), applied=["day1"], template=live_template(glide=False))
        self.assertEqual(c1.buffer, Decimal("0.10"))  # 第 1 天放寬照舊
        self.assertEqual(c2.buffer, Decimal("0.15"))  # 隔日直接回格點
        self.assertNotIn("cash_raise_limited", c2.offense)
        self.assertNotIn("prior_offense_loosened", c2.anchor)
        self.assertNotIn("prior_offense_loosened", c1.anchor)
        self.assertEqual(c2.rebuild_errors(), [])
        glide = self.day2("glide-compare")
        self.assertEqual(glide.buffer, Decimal("0.13"))
        self.assertNotEqual(glide.art["policy"]["content_sha256"], c2.art["policy"]["content_sha256"])
        # 沒有欄位的 Policy 不得夾帶綁定紀錄或 anchor 旗標
        forged_policy = sealed(c2.art["policy"], lambda p: p["position_sizing"]["offense"].update(cash_raise_limited=None))
        self.assertTrue(c2.rebuild_errors(policy=forged_policy))
        forged_anchor = sealed(c2.art["policy"], lambda p: p["position_sizing"]["cash_ratio_range"]["anchor"].update(prior_offense_loosened=True))
        self.assertTrue(c2.rebuild_errors(policy=forged_anchor))


class GlideDeriskTests(MultiDayCase):
    """帳戶現金低於目標：drawdown_derisk 賣到當日現金目標（0.13），不是 0.15；賣的股數少於沒有 cash_glide 的對照組。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.repo = cls.base / "glide-repo"
        cls.first_day(cls.repo, "probe")
        cls.control_repo = cls.base / "control-repo"
        cls.first_day(cls.control_repo, "probe", template=live_template(glide=False))
        cls.glide = cls.day(cls.repo, C2, "day2", stance_outputs(), applied=["day1"], account=LOW_CASH)
        cls.control = cls.day(cls.control_repo, C2, "day2", stance_outputs(), applied=["day1"], account=LOW_CASH, template=live_template(glide=False))

    def derisk_flags(self, day):
        return [flag for flag in day.art["proposal"]["constraint_flags"] if flag["code"] == "RISK_DRAWDOWN"]

    def test_both_days_complete_and_rebuild_clean(self):
        for day in (self.glide, self.control):
            self.assertEqual(day.result.status, "completed", day.result.errors)
            self.assertEqual(day.rebuild_errors(), [])

    def test_derisk_sells_to_the_glided_target_not_the_grid_point(self):
        self.assertEqual(self.glide.buffer, Decimal("0.13"))
        self.assertEqual(self.glide.offense["cash_raise_limited"], {"from": "0.15", "to": "0.13"})
        self.assertEqual(self.control.buffer, Decimal("0.15"))
        self.assertTrue(self.derisk_flags(self.glide), "滑行日仍須減碼（帳戶現金 0.109 低於 0.13）")
        self.assertTrue(self.derisk_flags(self.control))
        self.assertLess(self.glide.sell_shares(), self.control.sell_shares())
        self.assertGreater(self.glide.sell_shares(), 0)

    def test_post_sale_cash_ratio_is_not_below_the_loosened_ratio_and_not_above_the_grid_need(self):
        cash = Decimal(LOW_CASH["cash"])
        nav = Decimal(LOW_CASH["nav"])
        proceeds = sum(Decimal(order["net_cash_impact"]) for order in self.glide.art["decision"]["orders"])
        ratio = (cash + proceeds) / nav
        self.assertGreaterEqual(ratio, Decimal("0.13"))  # 賣到當日目標
        self.assertLess(ratio, Decimal("0.15"))  # 沒有一口氣賣到格點
        self.assertGreaterEqual(ratio, Decimal("0.10"))  # 不低於放寬後比例


if __name__ == "__main__":
    unittest.main()
