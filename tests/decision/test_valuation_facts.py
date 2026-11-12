"""估值 V2（position_sizing.valuation_facts）：docs/plans/quality_dip_plan.md 第 2.6 節與第 10.5 節。

覆蓋：

1. 閘門：形狀、只由人設定（策略反思禁區）、登記在 GATE_PREREQUISITES、沒有閘門的 team_inputs 逐鍵不變。
2. 事實：只讀 fetched_at 不晚於 cutoff 的最新一個 V1 run 並先整份重建；三個寫死的視窗並列與百分位公式；固定視窗資料列不足時不算數字；
   只算 PER／PBR（殖利率不進）；上櫃股、核對不符、PER 非正、授權範圍不允許（V1 的紀錄目前一律不允許）都不可用且不補值；
   沒有 run、run 在 cutoff 之後、資料日晚於 Snapshot、run 事後被改都降級為 unavailable 而不是拋錯。
3. 重算驗證：``validate_valuation_facts`` 以同一個資料夾與 run 整份重算；欄位被改、run 事後被改、讀不到資料夾、多餘欄位、沒有閘門一律拒絕。
4. 簡報與邊界：背景 citable=false、沒有 facts_id／run_id；交易 Agent 文字寫出背景識別碼即拒絕；沒有閘門時交易驗證不變。
"""

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from etf_agent.data.valuation import ValuationCollector, valuation_store
from etf_agent.data.valuation import reconcile
from etf_agent.decision.finalization import build_team_inputs
from etf_agent.decision.sizing import GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS
from etf_agent.decision.valuation_facts import (
    APPROVED_LICENSE_SCOPES, build_valuation_facts, validate_valuation_facts, validate_valuation_facts_gate,
    valuation_background_brief, valuation_facts_gate, valuation_reference_tokens, valuation_root_for,
)
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_valuation_collection import FakeFinMind, FakeOfficial, finmind, official

GATE = {"version": "v1"}
CUTOFF = "2026-11-10T09:00:00+08:00"
CLOCK = lambda: datetime(2026, 11, 10, 0, 30, tzinfo=timezone.utc)  # 08:30 台北，早於 CUTOFF


def bundle(latest="2026-11-09", cutoff=CUTOFF, symbols=("2308.TW", "1101.TW", "1102.TW", "1103.TW", "6488.TWO")):
    return {"decision_cutoff": cutoff, "snapshot": {"latest_trade_date": latest, "latest_prices": [{"symbol": s} for s in symbols]}}


def trading_days(end, count):
    days, current = [], date.fromisoformat(end)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current.isoformat())
        current -= timedelta(days=1)
    return sorted(days)


def approved_scope():
    """模擬日後紀錄帶有核准「背景進簡報」的用途範圍（實際要改 V1 契約，見計畫第 10.5 節）；patch 要涵蓋收集與重建。"""
    return patch.object(reconcile, "LICENSE_SCOPE", APPROVED_LICENSE_SCOPES[0])


class Store:
    """在暫存資料夾以 V1 收集器（假端點）建立一個估值 run。"""

    def __init__(self, test, scope_approved=True, clock=CLOCK, long_history=False):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "valuation_collection"
        raw = official([("2308", "34.83", "9.47", "1.00"), ("1101", "10.00", "1.00", "3.00"), ("1102", "", "1.50", "2.00"),
                        ("1103", "10.00", "1.00", "3.00")])
        dates = trading_days("2026-11-09", 260 if long_history else 4)
        per = ["40.00", "30.00", "50.00"] if not long_history else [str(20 + (index % 40)) + ".00" for index in range(len(dates) - 1)]
        rows_2308 = [(day, value, "10.0", "0.9") for day, value in zip(dates[:-1], per)] + [("2026-11-09", "34.83", "9.47", "1.0")]
        responses = {
            "2308": finmind("2308", rows_2308),
            "1101": finmind("1101", [("2026-11-06", "11.00", "1.10", "3.0"), ("2026-11-09", "10.01", "1.00", "3.00")]),  # 核對不符
            "1102": finmind("1102", [("2026-11-06", "", "1.40", "2.0"), ("2026-11-09", "", "1.50", "2.00")]),  # PER 未提供
            "1103": finmind("1103", [("2026-11-06", "12.00", "0.90", "3.0"), ("2026-11-09", "10.00", "1.00", "3.00")]),
        }
        if scope_approved:
            scope = approved_scope()
            scope.start()
            test.addCleanup(scope.stop)
        self.raw, self.responses, self.start = raw, responses, date.fromisoformat(dates[0])
        self.run_id = self.collect(clock)

    def collect(self, clock):
        summary = ValuationCollector(self.root, FakeOfficial(self.raw), FakeFinMind(self.responses), clock).collect(
            ["2308", "1101", "1102", "1103"], history_start=self.start)
        return summary["run_id"]


def items(facts):
    return {item["symbol"]: item for item in facts["items"]}


class GateTests(unittest.TestCase):
    def test_shape_and_registration(self):
        errors: list = []
        validate_valuation_facts_gate({"valuation_facts": {"version": "v1"}}, errors)
        self.assertEqual(errors, [])
        for bad in ({"version": "v2"}, {"version": "v1", "windows": [250]}, "v1"):
            errors = []
            validate_valuation_facts_gate({"valuation_facts": bad}, errors)
            self.assertTrue(errors, bad)
        self.assertIn("valuation_facts", GATE_FIELDS)
        self.assertIn("valuation_facts", POSITION_SIZING_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "valuation_facts")
        self.assertTrue(rule.human_only)
        self.assertIn("valuation_facts", FORBIDDEN_PREFIXES)
        self.assertIsNone(valuation_facts_gate({}))
        self.assertIsNone(valuation_facts_gate(None))

    def test_root_is_derived_from_the_decision_repository(self):
        self.assertIsNone(valuation_root_for(None))
        self.assertEqual(valuation_root_for(Path("/x/artifacts/decisions")), Path("/x/artifacts/valuation_collection"))

    def test_team_inputs_take_the_optional_key_only_when_given(self):
        import inspect
        self.assertIsNone(inspect.signature(build_team_inputs).parameters["valuation_facts"].default)  # 沒傳入時 team_inputs 逐鍵不變


class FactsTests(unittest.TestCase):
    def test_v1_records_are_never_used_because_their_license_scope_is_audit_only(self):
        store = Store(self, scope_approved=False)
        facts = build_valuation_facts(store.root, bundle(), GATE)
        self.assertEqual(facts["data_status"], "unavailable")
        self.assertEqual(facts["source"]["run_id"], store.run_id)
        by = items(facts)
        self.assertEqual(by["2308.TW"]["reason"], "LICENSE_SCOPE_NOT_APPROVED")
        self.assertIsNone(by["2308.TW"]["metrics"])
        self.assertTrue(any(gap.startswith("NO_USABLE_RECORD") for gap in facts["data_gaps"]))

    def test_positions_use_three_fixed_windows_and_the_documented_percentile(self):
        store = Store(self)
        facts = build_valuation_facts(store.root, bundle(), GATE)
        self.assertEqual(facts["data_status"], "available")
        self.assertEqual((facts["metrics"], facts["windows"]), (["per", "pbr"], ["rows_250", "rows_750", "all"]))
        per = items(facts)["2308.TW"]["metrics"]["per"]
        self.assertEqual(per["value"], "34.83")
        # 歷史 40、30、50 加當日 34.83：不高於 34.83 的有 30 與 34.83 兩筆 → 2/4。
        self.assertEqual(per["windows"]["all"], {
            "status": "available", "rows": 4, "sample_size": 4, "start": per["windows"]["all"]["start"], "end": "2026-11-09",
            "min": "30.00", "median": "37.42", "max": "50.00", "percentile": "0.5000",
        })
        self.assertEqual(per["windows"]["rows_250"], {"status": "insufficient_history", "required_rows": 250, "available_rows": 4})
        self.assertEqual(per["windows"]["rows_750"]["status"], "insufficient_history")
        self.assertNotIn("dividend_yield", items(facts)["2308.TW"]["metrics"])  # 殖利率基準未確認，不進簡報

    def test_a_long_history_fills_the_250_window_but_not_the_750_window(self):
        store = Store(self, long_history=True)
        per = items(build_valuation_facts(store.root, bundle(), GATE))["2308.TW"]["metrics"]["per"]["windows"]
        self.assertEqual((per["rows_250"]["status"], per["rows_250"]["rows"]), ("available", 250))
        self.assertEqual(per["rows_750"], {"status": "insufficient_history", "required_rows": 750, "available_rows": 260})
        self.assertEqual(per["all"]["rows"], 260)

    def test_unusable_symbols_are_reported_without_filling(self):
        by = items(build_valuation_facts(Store(self).root, bundle(), GATE))
        self.assertEqual(by["6488.TWO"]["reason"], "OTC_NOT_COLLECTED")
        self.assertEqual(by["1101.TW"]["reason"], "VALUE_MISMATCH")
        self.assertIsNone(by["1101.TW"]["metrics"])
        missing_per = by["1102.TW"]["metrics"]
        self.assertEqual((missing_per["per"]["value"], missing_per["per"]["null_reason"], missing_per["per"]["windows"]),
                         (None, "NOT_PROVIDED", None))
        self.assertEqual(missing_per["pbr"]["windows"]["all"]["status"], "available")  # PBR 可獨立使用

    def test_missing_late_or_future_data_degrades_to_unavailable(self):
        self.assertEqual(build_valuation_facts(None, bundle(), GATE)["data_gaps"], ["VALUATION_STORE_NOT_CONFIGURED"])
        with tempfile.TemporaryDirectory() as directory:
            empty = build_valuation_facts(Path(directory) / "none", bundle(), GATE)
        self.assertEqual((empty["data_status"], empty["source"], empty["data_gaps"]), ("unavailable", None, ["NO_VALUATION_RUN"]))
        store = Store(self)
        early = build_valuation_facts(store.root, bundle(cutoff="2026-11-10T08:00:00+08:00"), GATE)  # 抓取在 cutoff 之後
        self.assertEqual(early["data_gaps"], ["NO_VALUATION_RUN"])
        stale = build_valuation_facts(store.root, bundle(latest="2026-11-06"), GATE)  # 資料日晚於 Snapshot 最近交易日
        self.assertEqual(stale["data_status"], "unavailable")
        self.assertTrue(any(gap.startswith("VALUATION_DATA_DATE_AFTER_SNAPSHOT") for gap in stale["data_gaps"]))

    def test_a_run_changed_after_collection_is_not_used(self):
        store = Store(self)
        run_dir = valuation_store(store.root).verify(store.run_id)
        history = json.loads((run_dir / "history.json").read_text(encoding="utf-8"))
        history["history"][0]["values"]["per"] = "1.00"
        (run_dir / "history.json").write_text(json.dumps(history), encoding="utf-8")
        facts = build_valuation_facts(store.root, bundle(), GATE)
        self.assertEqual(facts["data_status"], "unavailable")
        self.assertTrue(any(gap.startswith("VALUATION_RUN_INVALID:%s" % store.run_id) for gap in facts["data_gaps"]))


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(self)
        self.bundle = bundle()
        self.policy = {"position_sizing": {"valuation_facts": dict(GATE)}}
        self.facts = build_valuation_facts(self.store.root, self.bundle, GATE)

    def check(self, facts, root=None, policy=None):
        errors: list = []
        validate_valuation_facts(self.store.root if root is None else root, self.bundle, policy or self.policy, {"valuation_facts": facts}, errors)
        return errors

    def test_the_same_inputs_rebuild_bit_for_bit(self):
        self.assertEqual(self.check(self.facts), [])

    def test_any_edit_or_extra_field_is_refused(self):
        edited = copy.deepcopy(self.facts)
        edited["items"][0]["status"] = "available" if edited["items"][0]["status"] != "available" else "unavailable"
        self.assertTrue(self.check(edited))
        self.assertTrue(self.check(dict(self.facts, cheap=True)))
        self.assertTrue(self.check(self.facts, policy={"position_sizing": {}}))

    def test_a_store_changed_after_the_run_or_missing_at_rebuild_time_is_refused(self):
        self.assertTrue(self.check(self.facts, root=Path(self.store.tmp.name) / "elsewhere"))
        run_dir = valuation_store(self.store.root).verify(self.store.run_id)
        records = json.loads((run_dir / "records.json").read_text(encoding="utf-8"))
        records["records"][0]["values"]["per"] = "1.00"
        (run_dir / "records.json").write_text(json.dumps(records), encoding="utf-8")
        self.assertTrue(self.check(self.facts))

    def test_rebuild_is_pinned_to_the_run_that_was_used(self):
        # 之後又有更晚但仍早於 cutoff 的 run：重算仍以封存的 run_id 為準，結果不變。
        later = self.store.collect(lambda: datetime(2026, 11, 10, 0, 45, tzinfo=timezone.utc))
        self.assertNotEqual(later, self.store.run_id)
        self.assertEqual(build_valuation_facts(self.store.root, self.bundle, GATE)["source"]["run_id"], later)  # 新決策會用較晚的 run
        self.assertEqual(self.check(self.facts), [])


class BriefAndBoundaryTests(unittest.TestCase):
    def test_background_has_no_identifiers_and_can_be_limited_to_a_batch(self):
        facts = build_valuation_facts(Store(self).root, bundle(), GATE)
        brief = valuation_background_brief(facts, ["2308.TW"])
        self.assertFalse(brief["citable"])
        self.assertEqual(list(brief["by_symbol"]), ["2308.TW"])
        text = json.dumps(brief, ensure_ascii=False)
        self.assertNotIn(facts["facts_id"], text)
        self.assertNotIn(facts["source"]["run_id"], text)
        self.assertNotIn("evidence_id", json.dumps(brief["by_symbol"], ensure_ascii=False))
        for word in ("便宜", "低估"):
            self.assertIn(word, brief["note"])  # 只出現在「不得寫」的說明裡
        self.assertEqual(valuation_reference_tokens(None), [])
        self.assertEqual(set(valuation_reference_tokens(facts)), {
            "valuation_background", "valuation_facts", facts["facts_id"], facts["source"]["run_id"]})

    def test_the_trader_text_may_not_cite_the_background(self):
        from test_gate_prerequisites import decision_item
        from test_swap_nomination import World
        from etf_agent.decision.trader import TradeDecisionValidator
        world = World()
        facts = build_valuation_facts(Store(self).root, bundle(), GATE)
        items_ = [dict(decision_item("1101.TW", "buy", "medium", adopt=["bull-1101-1"]), rationale="依 valuation_background 看起來位置低。"),
                  decision_item("2317.TW", "hold", "low", adopt=["bull-2317-1"]),
                  decision_item("2330.TW", "hold", "medium", adopt=["bear-2330-1"], reject=["bull-2330-1"])]
        from etf_agent.decision import seal_trade_decision, trade_decision_envelope
        from etf_agent.decision import MomentumEngine
        trade = seal_trade_decision(trade_decision_envelope(world.bundle, world.debate, "trade-v"), items_)
        momentum = MomentumEngine(world.bundle).run()
        plain = TradeDecisionValidator(world.bundle, momentum, world.debate).validate(trade)
        gated = TradeDecisionValidator(world.bundle, momentum, world.debate, valuation_facts=facts).validate(trade)
        self.assertFalse(any("valuation_background" in error for error in plain))  # 沒有閘門：驗證不變
        self.assertTrue(any("不得引用 valuation_background" in error for error in gated), gated)


if __name__ == "__main__":
    unittest.main()
