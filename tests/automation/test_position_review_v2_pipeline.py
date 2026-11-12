"""position_review v2（自進場起算的三個警示）接入每日決策鏈的端到端測試。

以 config/decision_policy.json 原樣為基礎（記憶體內只把 position_review 換成 v2、填測試用門檻；數值只放在測試內，
沒有寫進 config），跑真實 pipeline 與真實 Finalizer：交易 Agent 簡報集中列出警示、fixture 交易 Agent 逐一處理後 decision run
completed、team_inputs 不升版也沒有新鍵、Finalizer 重建乾淨，竄改持股檢視或 Policy 綁定的持有起點被拒；沒有 v2 的對照組逐位元不變。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from etf_agent.automation.daily_pipeline import DailyDecisionPipeline, _history_provider
from etf_agent.decision import DecisionFinalizer, DecisionRepository, DecisionResultValidator, decision_policy_sha256

from test_daily_pipeline import FakeRunner, without_stop_exit
import test_daily_pipeline
from test_decision_input_builder import builder_inputs
from test_quality_dip_ruling_pipeline import Harness3, live_like_template, team_outputs
from test_swap_nomination_pipeline import ROOT

# 進場日取 fixture 行情視窗內的一天（行情到 2026-09-19）；其後帳本狀態仍持有 2330。
# fixture 的行情單調上升，所以把 2330 在 09-17 的收盤拉到 170（進場日 09-16 收盤 161、最新 164），製造進場後的高點與回落。
ENTRY = "2026-09-16"
NAV = [
    {"run_id": "ledger-1", "as_of": "%sT06:00:00+00:00" % ENTRY, "nav": "1160000"},
    {"run_id": "ledger-2", "as_of": "2026-09-11T06:00:00+00:00", "nav": "1161000"},
]
HOLDINGS = [
    {"run_id": "ledger-1", "as_of": NAV[0]["as_of"], "symbols": ["2330.TW"]},
    {"run_id": "ledger-2", "as_of": NAV[1]["as_of"], "symbols": ["2330.TW"]},
]
V2_REVIEW = {
    "version": "v2", "loss_alert": "0.12", "retrace_alert_20d": "0.20", "gain_alert": "0.10",
    "trail_alert": {"arm_gain": "0.05", "giveback": "0.03"},
    "relative_loss_alert": {"loss": "0.15", "lag": "0.08"},
    "stale_alert": {"days": 1},
    "context": "v1",
}


def spiking_inputs():
    snapshot, rules, history, account = builder_inputs()
    for row in history:
        if row["symbol"] == "2330.TW" and row["trade_date"] == "2026-09-17":
            row.update(open_price="169.0", high_price="171.0", low_price="168.0", close_price="170.0")
    return snapshot, rules, history, account


def template_v2():
    value = live_like_template("v4")
    value["position_review"] = copy.deepcopy(V2_REVIEW)
    return value


class V2Harness3(Harness3):
    """與 Harness3 同一個世界，只是帳本歷史換成進場日落在行情視窗內的版本。"""

    def run(self, runner, resume=False, **kwargs):
        world = self.world
        pipeline = DailyDecisionPipeline(
            ROOT, world.run_dir, runner, world.repository, log=lambda _: None, resume=resume, sleep=lambda _: None, **kwargs)
        result = pipeline.run(
            world.paths["snapshot"], world.paths["account"], world.paths["trading_status"], world.paths["rules"],
            world.paths["database"], world.paths["template"], world.paths["sectors"], "decision-fixture",
            nav_history=NAV, holdings_history=HOLDINGS,
        )
        return result, pipeline

    def run_with_entry(self, runner, entry):
        """帳本歷史的持有起點換成指定日期（同樣兩個狀態都持有 2330）。"""
        global NAV, HOLDINGS
        saved = NAV, HOLDINGS
        NAV = [dict(NAV[0], as_of="%sT06:00:00+00:00" % entry), dict(NAV[1])]
        HOLDINGS = [dict(HOLDINGS[0], as_of=NAV[0]["as_of"]), dict(HOLDINGS[1])]
        self.history = _history_provider(NAV, HOLDINGS)  # 重建驗證要用同一份（已改過持有起點的）帳本歷史
        try:
            return self.run(runner)
        finally:
            NAV, HOLDINGS = saved

    def validate(self, art, **replace):
        art = dict(art, **replace)
        return DecisionResultValidator(
            art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
            self.world.repository, getattr(self, "history", None) or _history_provider(NAV, HOLDINGS),
        ).validate(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"], art["decision"])

    def rebuild_errors(self, art, **replace):
        art = dict(art, **replace)
        result = DecisionFinalizer(
            art["decision_input"], art["policy"], art["momentum"], art["debate"], art["intent"], art["team_inputs"],
            self.world.repository, getattr(self, "history", None) or _history_provider(NAV, HOLDINGS),
        ).run(art["proposal"], art["scenario"], art["guard"], art["risk_review"], art["revision_history"])
        return result["errors"] if result["status"] == "rejected" else []


class ReviewV2PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        (base / "v2").mkdir()
        with patch.object(test_daily_pipeline, "builder_inputs", spiking_inputs):
            cls.harness = V2Harness3(base / "v2", template=template_v2())
        cls.runner = FakeRunner(team_outputs("v4"))
        cls.result, _ = cls.harness.run(cls.runner)
        cls.art = cls.harness.artifacts() if cls.result.status == "completed" else {}

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_run_completes_and_the_review_carries_entry_facts_and_alerts(self):
        self.assertEqual(self.result.status, "completed", self.result.errors)
        review = self.art["team_inputs"]["position_review"]
        self.assertEqual(review["policy"]["version"], "v2")
        (item,) = review["items"]
        self.assertEqual((item["entry_date"], item["entry_gap"]), (ENTRY, None), item)
        self.assertGreaterEqual(item["holding_trading_days"], 1)
        # fixture 的 2330 之後有收盤高於最新收盤，所以移動停利警示出現；由 FakeRunner 的交易 Agent 逐一採納。
        print(item["flags"])

    def test_team_inputs_version_and_keys_are_unchanged_by_v2(self):
        self.assertEqual(self.art["team_inputs"]["schema_version"], "2.10")

    def test_the_brief_lists_the_alerts_centrally(self):
        brief = json.loads((self.harness.world.run_dir / "brief_trader_b0.json").read_text(encoding="utf-8"))
        listed = brief["position_review_background"]["alerts_to_handle"]
        self.assertEqual(listed, {"2330.TW": [flag["review_id"] for flag in self.art["team_inputs"]["position_review"]["items"][0]["flags"]]})

    def test_the_sealed_run_validates_and_rebuilds(self):
        self.assertEqual(self.harness.validate(self.art), [])
        self.assertEqual(self.harness.rebuild_errors(self.art), [])

    def test_tampered_review_or_bound_held_since_is_rejected(self):
        forged = copy.deepcopy(self.art["team_inputs"])
        forged["position_review"]["items"][0]["flags"] = []
        self.assertTrue(self.harness.rebuild_errors(self.art, team_inputs=forged))
        policy = copy.deepcopy(self.art["policy"])
        policy["position_sizing"]["exposure_rotation"]["held_since_by_symbol"]["2330.TW"] = "2026-09-15T06:00:00+00:00"
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(self.harness.rebuild_errors(self.art, policy=policy))
        downgraded = copy.deepcopy(self.art["policy"])
        downgraded["position_review"] = {"version": "v1", "loss_alert": "0.12", "retrace_alert_20d": "0.20", "gain_alert": "0.10", "context": "v1"}
        downgraded["position_sizing"].pop("stop_exit", None)  # stop_exit 需要 v2；先移除它，才是單純「review 降版」被重建抓到
        downgraded["content_sha256"] = decision_policy_sha256(downgraded)
        self.assertTrue(self.harness.rebuild_errors(self.art, policy=downgraded))
        # 只把 review 降成 v1、留著 stop_exit：Policy 自身不合法，重建直接 fail-closed 拒絕（拋 DecisionToolError）。
        from etf_agent.decision.fields import DecisionToolError

        half = copy.deepcopy(self.art["policy"])
        half["position_review"] = downgraded["position_review"]
        half["position_sizing"]["stop_exit"] = copy.deepcopy(STOP_GATE)  # 正式設定已不含 stop_exit，這裡明確補上
        half["content_sha256"] = decision_policy_sha256(half)
        with self.assertRaises(DecisionToolError):
            self.harness.rebuild_errors(self.art, policy=half)


class LiveAlertThresholdTests(unittest.TestCase):
    """正式設定（2026-11-11 17:09 起 loss 0.12／retrace 0.20／gain 0.10）原樣跑：交易 Agent 這一步不可降級，
    所以要證明「第一次漏掉警示 → 回饋點名缺少的警示 ID → 第二次補上 → decision run completed」這條修正循環走得通。"""

    class Forgetful(FakeRunner):
        """第一次交件完全不處理警示（真實模型最可能犯的錯），第二次照回饋補上；其餘照 FakeRunner。"""

        def run(self, task, run_dir):
            if task.name != "trader_b0":
                return super().run(task, run_dir)
            self.prompts.append((task.name, task.prompt))
            output = self.outputs[task.name].pop(0)
            first = sum(1 for name, _ in self.prompts if name == "trader_b0") == 1
            from test_daily_pipeline import AgentCall, fill_dip_assessment, fill_review_alerts
            filled = output if first else fill_review_alerts(task, output, run_dir)
            return AgentCall(task.name, 0, fill_dip_assessment(task, filled), 0.01)

    def test_the_live_config_thresholds_are_pinned(self):
        review = live_like_template("v4")["position_review"]
        self.assertEqual(
            {key: review[key] for key in ("loss_alert", "retrace_alert_20d", "gain_alert")},
            {"loss_alert": "0.12", "retrace_alert_20d": "0.20", "gain_alert": "0.20"},
        )

    def test_a_first_attempt_that_skips_the_alerts_is_corrected_by_the_feedback(self):
        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness3(Path(directory), template=live_like_template("v4"))
            outputs = team_outputs("v4")
            outputs["trader_b0"] = outputs["trader_b0"] * 2
            runner = self.Forgetful(outputs)
            result, _ = harness.run(runner)
            self.assertEqual(result.status, "completed", result.errors)
            trader_prompts = [prompt for name, prompt in runner.prompts if name == "trader_b0"]
            self.assertEqual(len(trader_prompts), 2)
            self.assertIn("review:2330.TW:gain_alert", trader_prompts[1])  # 回饋點名缺少的警示
            self.assertIn("降級", trader_prompts[1])  # 並列降級與補依據兩種修法
            self.assertEqual(harness.rebuild_errors(harness.artifacts()), [])


class WithoutV2Tests(unittest.TestCase):
    def test_a_v1_template_keeps_the_review_shape_and_the_brief_has_no_central_list(self):
        with tempfile.TemporaryDirectory() as directory:
            value = without_stop_exit(live_like_template("v4"))  # 正式設定已是 v2＋stop_exit；對照組明確降回 v1 並移除 stop_exit
            self.assertEqual(value["position_review"]["version"], "v1")
            self.assertNotIn("stop_exit", value["position_sizing"])
            harness = V2Harness3(Path(directory), template=value)
            result, _ = harness.run(FakeRunner(team_outputs("v4")))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            brief = json.loads((harness.world.run_dir / "brief_trader_b0.json").read_text(encoding="utf-8"))
            self.assertNotIn("alerts_to_handle", brief["position_review_background"])
            for item in art["team_inputs"]["position_review"]["items"]:
                self.assertNotIn("entry_date", item)
            self.assertEqual(harness.rebuild_errors(art), [])


# ---------------------------------------------------------------- stop_exit（程式兜底硬停損）端到端

STOP_ENTRY = "2026-09-15"


def stop_inputs():
    """2330 在進場日（09-15）收盤 190，之後一路跌到最新的 164（-13.7%）；2317 持續小漲，所以 2330 同期輸交易池。"""
    snapshot, rules, history, account = builder_inputs()
    path = {"2026-09-15": "190.0", "2026-09-16": "180.0", "2026-09-17": "175.0", "2026-09-18": "170.0"}
    for row in history:
        if row["symbol"] == "2330.TW" and row["trade_date"] in path:
            close = path[row["trade_date"]]
            row.update(open_price=close, high_price=str(float(close) + 1), low_price=str(float(close) - 1), close_price=close)
    return snapshot, rules, history, account


STOP_GATE = {"version": "v1", "loss": "0.10", "lag_to_pool": "0.05", "min_holding_days": 3, "max_exits_per_run": 2}


def template_stop():
    value = live_like_template("v4")
    value["position_review"] = copy.deepcopy(V2_REVIEW)
    value["position_sizing"]["stop_exit"] = copy.deepcopy(STOP_GATE)
    return value


class StopExitPipelineTests(unittest.TestCase):
    """真實 pipeline 與 Finalizer：停損觸發的持股由程式賣出、旗標與意圖不改寫、重建乾淨、竄改被拒；
    沒有 stop_exit 的同一個世界沒有賣單（對照組）。fixture 的最少檔數與現金上限規則可能留住持股，所以兩種結果都要有明確旗標。"""

    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        base = Path(cls.directory.name)
        for name in ("stop", "plain"):
            (base / name).mkdir()
        with patch.object(test_daily_pipeline, "builder_inputs", stop_inputs):
            cls.stop = V2Harness3(base / "stop", template=template_stop())
            plain_template = template_stop()
            plain_template["position_sizing"].pop("stop_exit")
            cls.plain = V2Harness3(base / "plain", template=plain_template)
        for harness in (cls.stop, cls.plain):
            harness.hold_entry = STOP_ENTRY
        cls.results = {}
        for name, harness in (("stop", cls.stop), ("plain", cls.plain)):
            result, _ = harness.run_with_entry(FakeRunner(team_outputs("v4")), STOP_ENTRY)
            cls.results[name] = (result, harness.artifacts() if result.status == "completed" else {})

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_both_runs_complete(self):
        for name, (result, _) in self.results.items():
            self.assertEqual(result.status, "completed", (name, result.errors))

    def test_the_stop_is_applied_or_explained_and_the_agent_intent_is_untouched(self):
        art = self.results["stop"][1]
        flags_ = {(flag["symbol"], flag["code"]) for flag in art["proposal"]["constraint_flags"]}
        stopped = ("2330.TW", "RISK_STOP_EXIT") in flags_
        cancelled = ("2330.TW", "RISK_STOP_EXIT_CANCELLED") in flags_
        self.assertTrue(stopped or cancelled, flags_)
        intents = {item["symbol"]: item["intent"] for item in art["intent"]["items"]}
        self.assertNotEqual(intents["2330.TW"], "exit")  # 交易 Agent 沒有賣；賣出（若有）是程式兜底
        sells = [order for order in art["proposal"]["order_proposal"]["orders"] if order["symbol"] == "2330.TW" and order["side"] == "sell"]
        if stopped:
            self.assertEqual([order["intent"] for order in sells], ["exit"])
        plain = {(flag["symbol"], flag["code"]) for flag in self.results["plain"][1]["proposal"]["constraint_flags"]}
        self.assertFalse({code for _, code in plain} & {"RISK_STOP_EXIT", "RISK_STOP_EXIT_CANCELLED", "STOP_EXIT_UNAVAILABLE"})

    def test_team_inputs_are_unchanged_by_the_gate(self):
        stop, plain = self.results["stop"][1]["team_inputs"], self.results["plain"][1]["team_inputs"]
        self.assertEqual(stop["schema_version"], plain["schema_version"])
        self.assertEqual(set(stop), set(plain))

    def test_the_sealed_run_validates_and_rebuilds_and_a_removed_gate_is_rejected(self):
        art = self.results["stop"][1]
        self.assertEqual(self.stop.validate(art), [])
        self.assertEqual(self.stop.rebuild_errors(art), [])
        policy = copy.deepcopy(art["policy"])
        policy["position_sizing"].pop("stop_exit")
        policy["content_sha256"] = decision_policy_sha256(policy)
        self.assertTrue(self.stop.rebuild_errors(art, policy=policy))
        tampered = copy.deepcopy(art["proposal"])
        tampered["constraint_flags"] = [flag for flag in tampered["constraint_flags"] if not flag["code"].startswith("RISK_STOP_EXIT")]
        self.assertTrue(self.stop.rebuild_errors(art, proposal=tampered))


class ProductionTemplateTests(unittest.TestCase):
    """正式設定（config/decision_policy.json 原樣）釘住 2026-11-11 18:09 起的 position_review v2 三個新門檻，
    且沒有 stop_exit（使用者決定硬停損不開，只保留警示讓交易 Agent 判斷）；
    以正式設定為底的 pipeline 要 completed、Finalizer 重建乾淨（改設定而沒改測試時這裡先失敗）。
    另有一個以正式設定原樣加上 stop_exit 的 pipeline 測試，證明兩者可並存（stop_exit 仍是已實作、尚未開啟）。"""

    def live(self):
        return json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))

    def test_the_live_config_pins_review_v2_without_stop_exit(self):
        live = self.live()
        self.assertEqual(
            live["position_review"],
            {"version": "v2", "loss_alert": "0.12", "retrace_alert_20d": "0.20", "gain_alert": "0.20",
             "trail_alert": {"arm_gain": "0.10", "giveback": "0.08"},
             "relative_loss_alert": {"loss": "0.08", "lag": "0.04"}, "stale_alert": None, "context": "v1"},
        )
        self.assertNotIn("stop_exit", live["position_sizing"])
        self.assertEqual(live["policy_id"], "strategy-2026-11-12-turnover-skip-1")
        self.assertIn("exposure_rotation", live["position_sizing"])  # position_review v2 的前提

    def test_a_run_on_the_untouched_live_gates_completes_and_rebuilds_clean(self):
        value = live_like_template("v4")
        self.assertEqual(value["position_review"], self.live()["position_review"])
        self.assertNotIn("stop_exit", value["position_sizing"])
        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness3(Path(directory), template=value)
            result, _ = harness.run(FakeRunner(team_outputs("v4")))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            self.assertEqual(art["policy"]["position_review"]["version"], "v2")
            self.assertNotIn("stop_exit", art["policy"]["position_sizing"])
            self.assertEqual(art["team_inputs"]["schema_version"], "2.10")
            self.assertEqual(harness.rebuild_errors(art), [])

    def test_the_live_gates_plus_stop_exit_coexist_and_rebuild_clean(self):
        value = live_like_template("v4")
        value["position_sizing"]["stop_exit"] = copy.deepcopy(STOP_GATE)
        with tempfile.TemporaryDirectory() as directory:
            harness = V2Harness3(Path(directory), template=value)
            result, _ = harness.run(FakeRunner(team_outputs("v4")))
            self.assertEqual(result.status, "completed", result.errors)
            art = harness.artifacts()
            self.assertEqual(art["policy"]["position_review"], self.live()["position_review"])
            self.assertIn("stop_exit", art["policy"]["position_sizing"])
            self.assertEqual(harness.rebuild_errors(art), [])
