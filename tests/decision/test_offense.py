"""position_sizing.offense v1（轉向後進攻，已實作、尚未開啟）：docs/agent_led_offense_plan.md 4.1～4.10。

覆蓋：Policy 閘門（形狀、值域、單調、前提、互斥、人專用）、有效等級壓回（缺資料永不升級）、現金目標只放寬、份量格只縮不放、
進攻事實（合成行情，缺資料為 unavailable）、現金姿態驗證（一致性、證據、probe 欄位、defensive_reconciliation）、舊 Policy 不受影響。
數值只放在測試內，沒有寫進 config。
"""

import copy
import json
import unittest
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from etf_agent.decision import DecisionToolError
from etf_agent.decision.offense import (
    OFFENSE_FACTS_KEY, build_offense_facts, capped_size, effective_view, normalize_offense_stance, offense_cash_target,
    offense_gate, validate_offense_stance,
)
from etf_agent.decision.sizing import GATE_FIELDS, POSITION_SIZING_FIELDS, validate_position_sizing
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

from test_market_index_facts import rising, with_index

ROOT = Path(__file__).resolve().parents[2]
OFFENSE = {
    "version": "v1",
    "by_view": {
        "probe": {"defensive_cash_buffer": "0.10", "max_exposure_pairs": 2, "max_size": "medium", "beta_stop": "0.40"},
        "start_rebuild": {"defensive_cash_buffer": "0.08", "max_exposure_pairs": 4, "max_size": "large", "beta_stop": "0.55"},
        "rebuild": {"defensive_cash_buffer": "0.08", "max_exposure_pairs": 5, "max_size": "large", "beta_stop": "0.70"},
    },
    "offense_off_drawdown": "0.12",
}


def live():
    return json.loads((ROOT / "config" / "decision_policy.json").read_text(encoding="utf-8"))


def with_offense(mutate=None):
    policy = live()
    sizing = policy["position_sizing"]
    for row in sizing["rebound_participation"]["by_state"].values():
        row.pop("defensive_cash_buffer", None)
    sizing["agent_swap"]["exposure"]["max_pairs_by_view"] = {"probe": 2, "start_rebuild": 4, "rebuild": 5}
    sizing["offense"] = copy.deepcopy(OFFENSE)
    if mutate:
        mutate(policy)
    return policy


def offense_errors(policy):
    errors = []
    validate_position_sizing(policy, ["2330.TW"], errors)
    return [error for error in errors if "offense" in error or "max_pairs_by_view" in error]


class GateTests(unittest.TestCase):
    def test_registration_and_forbidden_zone(self):
        self.assertIn("offense", GATE_FIELDS)
        self.assertIn("offense", POSITION_SIZING_FIELDS)
        self.assertIn("offense", FORBIDDEN_PREFIXES)

    def test_valid_shape_and_legacy_policy_unchanged(self):
        self.assertEqual(offense_errors(with_offense()), [])
        legacy = live()
        legacy["position_sizing"].pop("offense", None)  # 正式設定已開 offense：舊 Policy 以拿掉它的版本代表
        legacy["position_sizing"].pop("cash_stance_brief", None)  # 前提閘門被拿掉時，依賴它們的簡報閘門一併拿掉
        legacy["position_sizing"]["agent_swap"]["exposure"]["max_pairs_by_view"] = {"start_rebuild": 3, "rebuild": 5}
        self.assertIsNone(offense_gate(legacy["position_sizing"]))
        self.assertEqual(offense_errors(legacy), [])

    def test_intended_rebuild_buffer_below_neutral_floor_is_rejected(self):
        def lower(policy):
            policy["position_sizing"]["offense"]["by_view"]["rebuild"]["defensive_cash_buffer"] = "0.05"
        self.assertTrue(any("中性姿態最低現金比例" in e for e in offense_errors(with_offense(lower))))

    def test_rejection_branches(self):
        def edit(fn):
            return lambda policy: fn(policy["position_sizing"]["offense"], policy["position_sizing"])
        cases = {
            "缺欄位": edit(lambda o, s: o["by_view"]["probe"].pop("beta_stop")),
            "未知欄位": edit(lambda o, s: o.update(extra=1)),
            "缺等級": edit(lambda o, s: o["by_view"].pop("rebuild")),
            "對數超過成交上限": edit(lambda o, s: o["by_view"]["rebuild"].update(max_exposure_pairs=6)),
            "對數與 max_pairs_by_view 不同": edit(lambda o, s: o["by_view"]["probe"].update(max_exposure_pairs=1)),
            "beta 停止線高於 aggressive 目標": edit(lambda o, s: o["by_view"]["rebuild"].update(beta_stop="0.95")),
            "停止線不單調": edit(lambda o, s: o["by_view"]["probe"].update(beta_stop="0.60")),
            "份量格不單調": edit(lambda o, s: o["by_view"]["probe"].update(max_size="large") or o["by_view"]["start_rebuild"].update(max_size="medium")),
            "現金緩衝不單調": edit(lambda o, s: o["by_view"]["start_rebuild"].update(defensive_cash_buffer="0.11")),
            "停用回撤不低於 defensive_at": edit(lambda o, s: o.update(offense_off_drawdown="0.15")),
            "預填綁定欄位": edit(lambda o, s: o.update(effective_view="probe")),
            "缺 exposure_view": edit(lambda o, s: s.pop("exposure_view")),
            "缺 cash_ratio_range": edit(lambda o, s: s.pop("cash_ratio_range")),
        }
        for label, fn in cases.items():
            self.assertTrue(offense_errors(with_offense(fn)), label)

    def test_exclusive_with_rebound_defensive_cash_buffer(self):
        def add(policy):
            policy["position_sizing"]["rebound_participation"]["by_state"]["candidate"]["defensive_cash_buffer"] = "0.10"
        self.assertTrue(offense_errors(with_offense(add)))

    def test_probe_cap_not_above_start_rebuild(self):
        def bad(policy):
            policy["position_sizing"]["agent_swap"]["exposure"]["max_pairs_by_view"]["probe"] = 5
            policy["position_sizing"]["offense"]["by_view"]["probe"]["max_exposure_pairs"] = 5
        self.assertTrue(offense_errors(with_offense(bad)))


class BindingTests(unittest.TestCase):
    CONTROL = {"status": "available", "drawdown": "0.02", "stance_floor": "aggressive"}
    FACTS = {"data_status": "available"}

    def view(self, name, control=None, facts="default"):
        facts = self.FACTS if facts == "default" else facts
        return effective_view(OFFENSE, {"exposure_view": name}, self.CONTROL if control is None else control, facts)

    def test_keep_defensive_and_clean_levels_pass_through(self):
        self.assertEqual(self.view("keep_defensive"), ("keep_defensive", None))
        for name in ("probe", "start_rebuild", "rebuild"):
            self.assertEqual(self.view(name), (name, None))

    def test_missing_data_never_upgrades(self):
        self.assertEqual(self.view("probe", facts=None), ("keep_defensive", "facts_unavailable"))
        self.assertEqual(self.view("probe", facts={"data_status": "unavailable"}), ("keep_defensive", "facts_unavailable"))
        self.assertEqual(self.view("rebuild", control={"status": "unavailable", "drawdown": None}), ("keep_defensive", "drawdown_unavailable"))
        self.assertEqual(self.view("rebuild", control=dict(self.CONTROL, stance_floor="defensive")), ("keep_defensive", "stance_floor"))
        self.assertEqual(self.view("probe", control=dict(self.CONTROL, drawdown="0.12")), ("keep_defensive", "drawdown_limit"))
        self.assertEqual(self.view("probe", control=dict(self.CONTROL, drawdown="0.119")), ("probe", None))

    def test_invalid_view_is_rejected(self):
        with self.assertRaises(DecisionToolError):
            effective_view(OFFENSE, {"exposure_view": "all_in"}, self.CONTROL, self.FACTS)

    def test_cash_target_only_loosens_under_defensive(self):
        gate = dict(OFFENSE, effective_view="probe")
        self.assertEqual(offense_cash_target(gate, "defensive", "0.18"), ("0.10", {"from": "0.18", "to": "0.10"}))
        self.assertEqual(offense_cash_target(gate, "defensive", "0.08"), ("0.08", None))
        self.assertEqual(offense_cash_target(gate, "neutral", "0.13"), ("0.13", None))
        self.assertEqual(offense_cash_target(gate, "aggressive", "0.05"), ("0.05", None))
        self.assertEqual(offense_cash_target(dict(OFFENSE, effective_view="keep_defensive"), "defensive", "0.18"), ("0.18", None))

    def test_size_is_only_capped_never_raised(self):
        self.assertEqual(capped_size("large", "medium"), "medium")
        self.assertEqual(capped_size("small", "large"), "small")
        self.assertEqual(capped_size("medium", "medium"), "medium")


def bundle(days=30, with_index=False):
    from datetime import date, timedelta
    ds, day = [], date(2026, 11, 9)
    while len(ds) < days:
        if day.weekday() < 5:
            ds.append(day.isoformat())
        day -= timedelta(days=1)
    ds.sort()
    series, latest = [], []
    for symbol in ("A.TW", "B.TW"):
        bars, close = [], 100.0
        for index, d in enumerate(ds):
            close *= 0.99 if index < days - 3 else 1.02
            bars.append({"trade_date": d, "close": "%.4f" % close, "volume": "1000", "available_at": "%sT14:00:00+08:00" % d})
        series.append({"symbol": symbol, "evidence_id": "price:%s" % symbol, "bars": bars})
        latest.append({"symbol": symbol, "analysis_close_price": bars[-1]["close"], "source_evidence_id": "price:%s" % symbol})
    return {
        "decision_cutoff": "2026-11-09T23:00:00+00:00", "evidence_policy": "typed_evidence_v1",
        "snapshot": {"latest_trade_date": ds[-1], "latest_prices": latest, "documents": []},
        "price_series": series, "account_snapshot": {"cash": "1000", "positions": []},
    }


class FactsTests(unittest.TestCase):
    def setUp(self):
        self.policy = with_offense()

    def test_missing_index_and_sentiment_are_unavailable_not_filled(self):
        facts = build_offense_facts(bundle(), self.policy)
        self.assertFalse(facts["citable"])
        self.assertEqual(facts["categories"]["index_reclaim"]["status"], "unavailable")
        self.assertEqual(facts["categories"]["sentiment_extreme"]["status"], "unavailable")
        self.assertEqual(facts, build_offense_facts(bundle(), self.policy))  # 可重算
        self.assertTrue(facts["facts_id"].startswith("offense-facts:"))

    def test_no_gate_and_future_bars_are_rejected(self):
        with self.assertRaises(DecisionToolError):
            legacy = live()
            legacy["position_sizing"].pop("offense", None)
            legacy["position_sizing"].pop("cash_stance_brief", None)  # 前提閘門被拿掉時，依賴它們的簡報閘門一併拿掉
            build_offense_facts(bundle(), legacy)
        late = bundle()
        late["price_series"][0]["bars"][-1]["available_at"] = "2026-11-10T14:00:00+08:00"
        with self.assertRaises(DecisionToolError):
            build_offense_facts(late, self.policy)
        naive = bundle()
        naive["price_series"][0]["bars"][-1]["available_at"] = "2026-11-09T14:00:00"
        with self.assertRaises(DecisionToolError):
            build_offense_facts(naive, self.policy)

    def test_no_prices_means_unavailable(self):
        empty = bundle()
        empty["price_series"] = []
        facts = build_offense_facts(empty, self.policy)
        self.assertEqual(facts["data_status"], "unavailable")


def pct(numerator, denominator):
    return str(((Decimal(numerator) / Decimal(denominator) - 1) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class IndexReclaimTests(unittest.TestCase):
    """index_reclaim 類別：加權指數（market_fear_greed_input 的已核准 TWSE_TAIEX 月檔）可用與各種缺漏降級。"""

    def setUp(self):
        self.policy = with_offense()
        self.base = bundle()

    def index(self, bundle_value):
        return build_offense_facts(bundle_value, self.policy)["categories"]["index_reclaim"]

    def facts_gaps(self, bundle_value):
        return build_offense_facts(bundle_value, self.policy)["data_gaps"]

    def test_available_path_recomputes_every_number_from_the_series(self):
        closes = rising(70)
        facts = build_offense_facts(with_index(self.base, closes), self.policy)
        row = facts["categories"]["index_reclaim"]
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["latest_date"], self.base["snapshot"]["latest_trade_date"])
        self.assertEqual(row["close"], "169.00")
        ma5, ma20 = sum(closes[-5:]) / 5, sum(closes[-20:]) / 20
        self.assertEqual(row["close_vs_ma5_pct"], pct(169, ma5))
        self.assertEqual(row["close_vs_ma20_pct"], pct(169, ma20))
        self.assertIs(row["above_ma5"], True)
        self.assertIs(row["above_ma20"], True)
        self.assertEqual(row["drawdown_from_60d_high_pct"], "0.00")
        self.assertEqual(facts["data_status"], "available")
        self.assertFalse(any("index_reclaim" in gap for gap in facts["data_gaps"]))
        self.assertEqual(facts, build_offense_facts(with_index(self.base, closes), self.policy))  # 可重算、逐位一致

    def test_a_sharp_drop_is_below_both_averages_and_shows_the_drawdown(self):
        closes = rising(69) + [Decimal(50)]
        row = self.index(with_index(self.base, closes))
        self.assertEqual(row["status"], "available")
        self.assertIs(row["above_ma5"], False)
        self.assertIs(row["above_ma20"], False)
        self.assertEqual(row["close"], "50.00")
        self.assertEqual(row["drawdown_from_60d_high_pct"], pct(50, 168))

    def test_a_reclaim_flips_the_flags_without_the_facts_calling_it_a_signal(self):
        closes = [Decimal(100)] * 40 + [Decimal(80)] * 28 + [Decimal(90), Decimal(110)]  # 先破底、再站回 MA5 與 MA20
        row = self.index(with_index(self.base, closes))
        self.assertIs(row["above_ma5"], True)
        self.assertIs(row["above_ma20"], True)
        self.assertEqual(row["drawdown_from_60d_high_pct"], "0.00")
        self.assertNotIn("signal", json.dumps(row))

    def test_short_history_keeps_the_category_available_but_lists_the_missing_drawdown(self):
        facts = build_offense_facts(with_index(self.base, rising(30)), self.policy)
        row = facts["categories"]["index_reclaim"]
        self.assertEqual(row["status"], "available")
        self.assertIsNone(row["drawdown_from_60d_high_pct"])
        self.assertTrue(any("60 日" in gap for gap in facts["data_gaps"]))

    def test_fewer_than_twenty_closes_is_unavailable_with_a_gap_not_a_guess(self):
        bundle_value = with_index(self.base, rising(19))
        self.assertEqual(self.index(bundle_value), {"status": "unavailable"})
        self.assertTrue(any("MA20" in gap for gap in self.facts_gaps(bundle_value)))

    def test_missing_pack_unapproved_source_and_failed_capture_degrade_to_unavailable(self):
        self.assertEqual(self.index(self.base), {"status": "unavailable"})  # 決策輸入沒有恐懼貪婪資料包
        self.assertTrue(any("沒有恐懼貪婪資料包" in gap for gap in self.facts_gaps(self.base)))
        unapproved = with_index(self.base, rising(70), approved=False)
        self.assertEqual(self.index(unapproved), {"status": "unavailable"})
        self.assertTrue(any("未核准" in gap for gap in self.facts_gaps(unapproved)))
        failed = with_index(self.base, rising(70))
        for capture in failed["market_fear_greed_input"]["bundle"]["captures"]:
            capture["status"] = "failed"
        self.assertEqual(self.index(failed), {"status": "unavailable"})
        self.assertTrue(any("沒有成功抓取" in gap for gap in self.facts_gaps(failed)))

    def test_a_stale_last_row_and_a_future_row_are_both_unavailable_with_a_gap(self):
        trade_date = date.fromisoformat(self.base["snapshot"]["latest_trade_date"])
        earlier = (trade_date - timedelta(days=3)).isoformat()
        stale = with_index(self.base, rising(70), end=earlier)
        self.assertEqual(self.index(stale), {"status": "unavailable"})
        self.assertTrue(any("不是 snapshot.latest_trade_date" in gap for gap in self.facts_gaps(stale)))
        # 晚於最近交易日的列不能當截止日的事實，也不能中斷整個 run：整類 unavailable、記缺口，不補值。
        future = with_index(self.base, rising(70), end=(trade_date + timedelta(days=3)).isoformat())
        self.assertEqual(self.index(future), {"status": "unavailable"})
        self.assertTrue(any("晚於 snapshot.latest_trade_date" in gap for gap in self.facts_gaps(future)))
        self.assertEqual(build_offense_facts(future, self.policy), build_offense_facts(future, self.policy))

    def test_a_damaged_raw_response_is_rejected_not_degraded(self):
        damaged = with_index(self.base, rising(70))
        damaged["market_fear_greed_input"]["bundle"]["captures"][0]["raw_text"] = "{}"
        with self.assertRaises(DecisionToolError):
            build_offense_facts(damaged, self.policy)

    def test_the_index_never_leaks_into_the_other_categories(self):
        with_series = build_offense_facts(with_index(self.base, rising(70)), self.policy)["categories"]
        without = build_offense_facts(self.base, self.policy)["categories"]
        for name in ("washout", "breadth_thrust", "volume_confirmation", "sentiment_extreme"):
            self.assertEqual(with_series[name], without[name], name)


class StanceTests(unittest.TestCase):
    FACTS = {"facts_id": "offense-facts:abc", "categories": {
        "washout": {"status": "available"}, "breadth_thrust": {"status": "available"}, "volume_confirmation": {"status": "unavailable"},
        "sentiment_extreme": {"status": "unavailable"}, "index_reclaim": {"status": "unavailable"}}}

    def run_stance(self, **fields):
        stance = {"level": "neutral", "exposure_view": "probe", "turn_assessment": "no_turn_evidence", "rationale": "x",
                  "evidence_ids": ["mkt-a", "reg"], "offense_basis": ["washout", "breadth_thrust"],
                  "failure_condition": "三個交易日內指數再破前低即視為失敗"}
        stance.update(fields)
        errors = []
        validate_offense_stance(stance, stance["evidence_ids"], {"mkt-a", "reg"}, {"reg"}, self.FACTS, ["offense_facts"], "cash_stance", errors)
        return errors

    def test_valid_probe(self):
        self.assertEqual(self.run_stance(), [])

    def test_rejection_branches(self):
        cases = {
            "turn 不一致": {"turn_assessment": "turn_confirmed"},
            "類別不足": {"offense_basis": ["washout"]},
            "類別不可用": {"offense_basis": ["washout", "volume_confirmation"]},
            "缺失敗條件": {"failure_condition": ""},
            "失敗條件無時間": {"failure_condition": "指數表現不好時就算失敗了"},
            "只有新聞類證據": {"evidence_ids": ["mkt-a", "mkt-b"]},
            "證據不足": {"evidence_ids": ["reg"]},
            "文字含鍵名": {"rationale": "依 offense_facts 判斷"},
            "defensive 缺 reconciliation": {"level": "defensive"},
        }
        for label, fields in cases.items():
            self.assertTrue(self.run_stance(**fields), label)

    def test_keep_defensive_must_not_fill_extra_fields(self):
        self.assertTrue(self.run_stance(exposure_view="keep_defensive", evidence_ids=["reg"], level="defensive", defensive_reconciliation="說明"))

    def test_normalize_drops_empty_inapplicable_fields(self):
        out = normalize_offense_stance({"exposure_view": "keep_defensive", "offense_basis": [], "failure_condition": "", "defensive_reconciliation": ""})
        self.assertNotIn("offense_basis", out)
        self.assertNotIn("failure_condition", out)
        self.assertNotIn("defensive_reconciliation", out)
        self.assertEqual(OFFENSE_FACTS_KEY, "offense_facts")


if __name__ == "__main__":
    unittest.main()
