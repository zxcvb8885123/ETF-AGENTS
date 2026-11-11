"""持股檢視 v2（``position_review`` 自進場起算的三個警示）的確定性與 fail-closed 測試。

v2 是版本閘門（只由人設定）：沒有 v2 的 Policy 與舊 run 逐位元維持 v1 行為。事實取自 Policy 綁定的
``exposure_rotation.held_since_by_symbol``（持有起點）與 bundle 的行情，不依賴帳戶 average_cost。"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import TradeDecisionValidator, build_position_review, validate_position_review
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.position_review import (
    COST_BASIS_NOTE,
    ENTRY_ITEM_FIELDS,
    ITEM_FIELDS,
    position_review_brief,
    position_review_version,
    review_id,
    validate_position_review_policy,
)

from test_position_review import decide, losing_bundle, with_retrace
from test_trader import items, world

V2 = {
    "version": "v2", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None,
    "trail_alert": {"arm_gain": "0.10", "giveback": "0.05"},
    "relative_loss_alert": {"loss": "0.15", "lag": "0.08"},
    "stale_alert": {"days": 3},
}
SYMBOL = "2330.TW"
OTHER = "2317.TW"


def policy(since_date=None, symbol=SYMBOL, **overrides):
    """v2 Policy；``since_date`` 給了就綁定 held_since_by_symbol（台北日期當日收盤後的狀態）。"""
    value = {"position_review": {**V2, **overrides}}
    if since_date is not None:
        value["position_sizing"] = {"exposure_rotation": {"held_since_by_symbol": {symbol: "%sT13:30:00+08:00" % since_date}}}
    return value


def shaped(bundle, own, other=None):
    """把兩檔的最後 len(own) 根收盤改成指定值（第一根是進場日收盤），回傳 (bundle, 進場日)。其他欄位原樣。"""
    changed = copy.deepcopy(bundle)
    for row in changed["price_series"]:
        values = own if row["symbol"] == SYMBOL else (other if other is not None else [own[0]] * len(own))
        for bar, close in zip(row["bars"][-len(values):], values):
            bar["close"] = str(close)
            bar["high"] = str(max(Decimal(str(bar["high"])), Decimal(str(close))))
    series = next(row for row in changed["price_series"] if row["symbol"] == SYMBOL)
    return changed, series["bars"][-len(own)]["trade_date"]


def review_for(bundle, own, other=None, **overrides):
    changed, since = shaped(bundle, own, other)
    return build_position_review(changed, policy(since, **overrides))["items"][0]


def flags(item):
    return [flag["flag"] for flag in item["flags"]]


class PolicyTests(unittest.TestCase):
    def errors(self, settings):
        errors = []
        validate_position_review_policy(settings, errors)
        return errors

    def test_a_complete_v2_policy_is_valid_and_null_disables_each_alert(self):
        self.assertEqual(self.errors(V2), [])
        self.assertEqual(self.errors({**V2, "trail_alert": None, "relative_loss_alert": None, "stale_alert": None}), [])

    def test_missing_or_malformed_v2_fields_are_rejected(self):
        for field in ("trail_alert", "relative_loss_alert", "stale_alert"):
            self.assertTrue(self.errors({key: value for key, value in V2.items() if key != field}), field)
        bad = (
            {**V2, "trail_alert": {"arm_gain": "0.10"}},
            {**V2, "trail_alert": {"arm_gain": "0.10", "giveback": "0.05", "extra": "1"}},
            {**V2, "trail_alert": {"arm_gain": "0", "giveback": "0.05"}},
            {**V2, "trail_alert": {"arm_gain": "0.10", "giveback": "1"}},
            {**V2, "relative_loss_alert": {"loss": "-0.1", "lag": "0.08"}},
            {**V2, "relative_loss_alert": {"loss": "0.15"}},
            {**V2, "stale_alert": {"days": 0}},
            {**V2, "stale_alert": {"days": 2.5}},
            {**V2, "stale_alert": {"days": True}},
            {**V2, "stale_alert": 3},
            {**V2, "trail_alert": "0.1"},
        )
        for settings in bad:
            self.assertTrue(self.errors(settings), settings)

    def test_v1_policy_rejects_the_v2_fields(self):
        v1 = {"version": "v1", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None}
        self.assertEqual(self.errors(v1), [])
        self.assertTrue(self.errors({**v1, "trail_alert": None}))
        self.assertTrue(self.errors({**v1, "stale_alert": {"days": 3}}))
        self.assertTrue(self.errors({**V2, "version": "v3"}))

    def test_version_helper_defaults_to_v1(self):
        self.assertEqual(position_review_version({}), "v1")
        self.assertEqual(position_review_version({"position_review": {"version": "v1"}}), "v1")
        self.assertEqual(position_review_version(policy()), "v2")


class EntryFactsTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, _, _ = world()

    def test_facts_are_recomputable_from_the_bars_and_do_not_use_average_cost(self):
        item = review_for(self.bundle, [100, 110, 104, 102], [100, 101, 102, 103])
        self.assertEqual(set(item) - ENTRY_ITEM_FIELDS, ITEM_FIELDS)
        self.assertLessEqual(ENTRY_ITEM_FIELDS, set(item))
        self.assertEqual(item["holding_trading_days"], 3)
        self.assertEqual(item["return_since_entry"], "0.020000")
        self.assertEqual(item["peak_return_since_entry"], "0.100000")
        self.assertEqual(item["retrace_from_entry_high"], str((Decimal(102) / Decimal(110) - 1).quantize(Decimal("0.000001"))))
        self.assertEqual(item["pool_return_since_entry"], "0.025000")  # (0.02 + 0.03) / 2
        self.assertEqual(item["relative_to_pool_since_entry"], "-0.005000")
        self.assertIsNone(item["entry_gap"])
        # 帳戶成本（v1 的 unrealized_return 依據）改了，自進場事實完全不變。
        costly = losing_bundle(shaped(self.bundle, [100, 110, 104, 102], [100, 101, 102, 103])[0], average_cost="1")
        again = build_position_review(costly, policy(item["entry_date"]))["items"][0]
        self.assertEqual({key: again[key] for key in ENTRY_ITEM_FIELDS}, {key: item[key] for key in ENTRY_ITEM_FIELDS})

    def test_reconstruction_day_zero_cost_return_does_not_zero_the_entry_facts(self):
        """帳本重建日的 average_cost＝當日收盤，v1 的 unrealized_return 是 0；v2 事實從持有起點之後的行情算，不歸零。"""
        changed, since = shaped(self.bundle, [100, 90, 82])
        changed["account_snapshot"]["positions"][0]["average_cost"] = "82"
        item = build_position_review(changed, policy(since, relative_loss_alert={"loss": "0.15", "lag": "0.01"}))["items"][0]
        self.assertEqual(item["average_cost"], "82.000000")
        self.assertEqual(item["return_since_entry"], "-0.180000")

    def test_trail_alert_boundary(self):
        self.assertEqual(flags(review_for(self.bundle, [100, 110, 104.5])), ["trail_alert"])  # 剛好 -5%
        self.assertEqual(flags(review_for(self.bundle, [100, 110, 104.6])), [])  # 差一點
        self.assertEqual(flags(review_for(self.bundle, [100, 109.9, 104.4])), [])  # 沒賺到 arm_gain
        self.assertEqual(flags(review_for(self.bundle, [100, 110, 104.5], trail_alert=None)), [])

    def test_relative_loss_alert_needs_both_conditions(self):
        # 個股 -15%、對手 +1% → 交易池（含自己）-7%，相對 -8%：剛好觸發。
        self.assertEqual(flags(review_for(self.bundle, [100, 90, 85], [100, 100, 101])), ["relative_loss_alert"])
        self.assertEqual(flags(review_for(self.bundle, [100, 90, 85], [100, 100, 100.9])), [])  # 相對差一點
        self.assertEqual(flags(review_for(self.bundle, [100, 90, 85.1], [100, 100, 101])), [])  # 虧損差一點
        # 大盤一起跌：個股 -15%、交易池同跌，不觸發。
        self.assertEqual(flags(review_for(self.bundle, [100, 90, 85], [100, 90, 85])), [])

    def test_stale_alert_counts_trading_days_after_the_entry_day_and_needs_a_loss(self):
        self.assertEqual(flags(review_for(self.bundle, [100, 99, 98, 97])), ["stale_alert"])  # 3 個交易日、虧損
        self.assertEqual(flags(review_for(self.bundle, [100, 99, 98])), [])  # 只有 2 個交易日
        self.assertEqual(flags(review_for(self.bundle, [100, 99, 98, 100])), [])  # 持平不算虧損
        self.assertEqual(flags(review_for(self.bundle, [100, 99, 98, 101])), [])

    def test_flags_keep_the_v1_flags_first_and_use_the_review_id_format(self):
        item = review_for(self.bundle, [100, 99, 98, 97], loss_alert="0.01", stale_alert={"days": 1})
        # loss_alert 依帳戶成本（fixture 成本 120、現價 164，沒有虧損），所以不出現；stale 在列。
        self.assertEqual(item["flags"], [{"review_id": review_id(SYMBOL, "stale_alert"), "flag": "stale_alert"}])

    def test_missing_held_since_gives_null_facts_a_gap_and_no_alerts(self):
        item = build_position_review(self.bundle, policy())["items"][0]
        self.assertEqual(item["entry_gap"], "held_since_unavailable")
        for key in ENTRY_ITEM_FIELDS - {"entry_gap"}:
            self.assertIsNone(item[key], key)
        self.assertEqual(item["flags"], [])

    def test_entry_date_without_a_bar_or_with_a_bad_timestamp_is_a_gap(self):
        item = build_position_review(self.bundle, policy("2000-01-03"))["items"][0]
        self.assertEqual((item["entry_gap"], item["entry_date"], item["flags"]), ("entry_bar_missing", "2000-01-03", []))
        broken = policy()
        broken["position_sizing"] = {"exposure_rotation": {"held_since_by_symbol": {SYMBOL: "2026-09-18T13:30:00"}}}  # 缺時區
        bad = build_position_review(self.bundle, broken)["items"][0]
        self.assertEqual((bad["entry_gap"], bad["flags"]), ("held_since_invalid", []))

    def test_unavailable_pool_gives_null_relative_fact_and_no_relative_alert(self):
        changed, since = shaped(self.bundle, [100, 90, 85])
        changed["price_series"] = [row for row in changed["price_series"] if row["symbol"] == SYMBOL]
        for row in changed["price_series"]:
            row["bars"] = [dict(bar) for bar in row["bars"]]
        item = build_position_review(changed, policy(since))["items"][0]
        # 只有自己時交易池就是自己，相對報酬為 0，不會觸發。
        self.assertEqual(item["relative_to_pool_since_entry"], "0.000000")
        self.assertNotIn("relative_loss_alert", flags(item))

    def test_a_bar_after_the_cutoff_or_without_timezone_is_rejected(self):
        changed, since = shaped(self.bundle, [100, 99, 98])
        for row in changed["price_series"]:
            row["bars"][-1]["available_at"] = "2099-01-01T00:00:00+00:00"
        with self.assertRaises(DecisionToolError):
            build_position_review(changed, policy(since))
        for row in changed["price_series"]:
            row["bars"][-1]["available_at"] = "2026-09-19T00:00:00"
        with self.assertRaises(DecisionToolError):
            build_position_review(changed, policy(since))
        later, since = shaped(self.bundle, [100, 99, 98])
        later["snapshot"]["latest_trade_date"] = "2000-01-01"
        with self.assertRaises(DecisionToolError):
            build_position_review(later, policy(since))

    def test_v1_ignores_held_since_and_stays_bit_for_bit(self):
        v1 = {"version": "v1", "loss_alert": "0.10", "retrace_alert_20d": "0.12", "gain_alert": None}
        plain = build_position_review(self.bundle, {"position_review": v1})
        bound = build_position_review(self.bundle, {"position_review": v1, "position_sizing": {"exposure_rotation": {"held_since_by_symbol": {SYMBOL: "2026-09-18T13:30:00+08:00"}}}})
        self.assertEqual(plain, bound)
        self.assertEqual(set(plain["items"][0]), ITEM_FIELDS)
        self.assertEqual(plain["cost_basis_note"], COST_BASIS_NOTE)
        self.assertEqual(set(plain["policy"]), set(v1) | set())
        self.assertNotIn("alerts_to_handle", position_review_brief(plain))


class KnownLimitTests(unittest.TestCase):
    """已知限制（文件已註明）：帳本重建日 average_cost＝當日收盤，v1 的 loss／gain 警示因帳面報酬接近 0 而失靈；
    retrace_alert 看的是近 20 日高點、不依賴成本，不受影響；v2 的自進場事實也不依賴成本。"""

    def test_reconstruction_day_cost_silences_loss_and_gain_but_not_retrace(self):
        bundle, _, _, _ = world()
        bundle = with_retrace(bundle)
        price = next(row for row in bundle["snapshot"]["latest_prices"] if row["symbol"] == SYMBOL)["close_price"]
        bundle["account_snapshot"]["positions"][0]["average_cost"] = price  # 重建日：成本＝當日收盤
        settings = {"version": "v1", "loss_alert": "0.01", "retrace_alert_20d": "0.12", "gain_alert": "0.01"}
        item = build_position_review(bundle, {"position_review": settings})["items"][0]
        self.assertEqual(item["unrealized_return"], "0.000000")
        self.assertEqual(flags(item), ["retrace_alert"])


class ValidatorAndBriefTests(unittest.TestCase):
    def setUp(self):
        self.valid_bundle, self.momentum, _, self.debate = world()
        # 改過收盤的 bundle 只用來重算持股檢視；交易 Agent 的驗證只讀警示 ID，用原本通過驗證的 bundle。
        self.bundle, since = shaped(self.valid_bundle, [100, 99, 98, 97])
        self.policy = policy(since)
        self.review = build_position_review(self.bundle, self.policy)
        self.alert = review_id(SYMBOL, "stale_alert")

    def errors_for(self, review, settings=None):
        errors = []
        validate_position_review(self.bundle, settings or self.policy, review, errors)
        return errors

    def test_rebuilt_review_passes_and_every_tamper_is_rejected(self):
        self.assertEqual(self.errors_for(self.review), [])
        for mutate in (
            lambda item: item.update(flags=[]),
            lambda item: item.update(holding_trading_days=99),
            lambda item: item.update(return_since_entry="0.500000"),
            lambda item: item.update(entry_gap="entry_bar_missing"),
            lambda item: item.pop("peak_return_since_entry"),
        ):
            changed = copy.deepcopy(self.review)
            mutate(changed["items"][0])
            self.assertTrue(self.errors_for(changed))

    def test_a_review_rebuilt_under_another_threshold_or_version_is_rejected(self):
        self.assertTrue(self.errors_for(self.review, policy(self.review["items"][0]["entry_date"], stale_alert={"days": 5})))
        v1 = {"position_review": {"version": "v1", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None}}
        self.assertTrue(self.errors_for(self.review, v1))

    def test_a_forged_held_since_in_the_policy_changes_the_rebuild_and_is_rejected(self):
        self.assertTrue(self.errors_for(self.review, policy("2000-01-03")))

    def test_brief_lists_alerts_centrally_for_v2_only(self):
        brief = position_review_brief(self.review)
        self.assertEqual(brief["alerts_to_handle"], {SYMBOL: [self.alert]})
        self.assertLessEqual(ENTRY_ITEM_FIELDS, set(brief["by_symbol"][SYMBOL]))
        self.assertEqual(brief["by_symbol"][SYMBOL]["flags"], [{"review_id": self.alert, "flag": "stale_alert"}])
        quiet = build_position_review(world()[0], policy())
        self.assertEqual(position_review_brief(quiet)["alerts_to_handle"], {})

    def validate(self, values):
        decision = decide(self.valid_bundle, self.debate, values)
        return TradeDecisionValidator(self.valid_bundle, self.momentum, self.debate, position_review=self.review).validate(decision)

    def values(self, **changes):
        values = items()
        values[1].update(changes)
        return values

    def test_every_alert_must_be_adopted_exactly_once_and_the_feedback_lists_the_missing_id(self):
        errors = self.validate(items())
        self.assertTrue(any("未處理全部 claim" in error for error in errors), errors)
        self.assertTrue(any("尚未處理的持股檢視警示" in error and self.alert in error for error in errors), errors)
        ok = self.values(adopted_claim_ids=["bull-2330-1", self.alert], rejected_claim_ids=["bear-2330-1"])
        self.assertEqual(self.validate(ok), [])
        rejected = self.values(adopted_claim_ids=["bull-2330-1"], rejected_claim_ids=["bear-2330-1", self.alert])
        self.assertTrue(any("只能採納不可否決" in error for error in self.validate(rejected)))
        twice = self.values(adopted_claim_ids=["bull-2330-1", self.alert, self.alert], rejected_claim_ids=["bear-2330-1"])
        self.assertTrue(any("剛好處理一次" in error for error in self.validate(twice)))

    def test_v1_feedback_text_is_unchanged(self):
        v1_bundle = losing_bundle(world()[0])
        v1 = {"position_review": {"version": "v1", "loss_alert": "0.10", "retrace_alert_20d": None, "gain_alert": None}}
        review = build_position_review(v1_bundle, v1)
        decision = decide(v1_bundle, self.debate, items())
        errors = TradeDecisionValidator(v1_bundle, self.momentum, self.debate, position_review=review).validate(decision)
        self.assertTrue(any("未處理全部 claim" in error for error in errors))
        self.assertFalse(any("尚未處理的持股檢視警示" in error for error in errors))

    def test_stale_alert_alone_does_not_force_a_bull_claim_but_loss_alert_still_does(self):
        values = self.values(intent="hold", adopted_claim_ids=["bear-2330-1", self.alert], rejected_claim_ids=["bull-2330-1"])
        self.assertEqual(self.validate(values), [])


if __name__ == "__main__":
    unittest.main()
