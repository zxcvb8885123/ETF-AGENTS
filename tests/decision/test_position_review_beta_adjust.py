"""持股檢視 ``beta_adjust``（扣除 beta 後的殘差報酬）的測試。

版本閘門（只由人設定）：沒有 beta_adjust 的 Policy 與舊 run 逐位元維持原行為；開啟時 relative_loss_alert 改看
residual_since_entry（beta 缺資料時退回原始相對報酬），缺 exposure_rotation 閘門即 fail-closed。"""

import copy
import unittest
from decimal import Decimal

from etf_agent.decision import build_position_review, validate_position_review
from etf_agent.decision.fields import DecisionToolError
from etf_agent.decision.position_review import (
    BETA_ITEM_FIELDS,
    COST_BASIS_NOTE_V2,
    position_review_brief,
    validate_position_review_policy,
)

from test_trader import world

SYMBOL = "2330.TW"
V2 = {
    "version": "v2", "context": "v1", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None,
    "trail_alert": None, "relative_loss_alert": {"loss": "0.05", "lag": "0.03"}, "stale_alert": None,
}
SMALL = {"+": Decimal("0.01"), "-": Decimal("-0.01")}


def compound(returns):
    closes = [Decimal(100)]
    for value in returns:
        closes.append(closes[-1] * (1 + value))
    return [str(value.quantize(Decimal("0.000001"))) for value in closes]


def noise(count):
    return [SMALL["+"] if index % 2 == 0 else SMALL["-"] for index in range(count)]


def shaped(bundle, own_returns, other_returns):
    """最後 21 根收盤改成指定報酬路徑（共 20 個日報酬）；回傳 (bundle, 進場日＝倒數第 6 根)。"""
    changed = copy.deepcopy(bundle)
    paths = {SYMBOL: compound(own_returns), "2317.TW": compound(other_returns)}
    for row in changed["price_series"]:
        for bar, close in zip(row["bars"][-21:], paths[row["symbol"]]):
            bar["close"] = bar["high"] = bar["low"] = close
    series = next(row for row in changed["price_series"] if row["symbol"] == SYMBOL)
    return changed, series["bars"][-6]["trade_date"]


def policy(since_date, beta_adjust=True, window=20, gate=True, **overrides):
    settings = {**V2, **overrides}
    if beta_adjust:
        settings["beta_adjust"] = "v1"
    value = {"position_review": settings}
    if gate:
        value["position_sizing"] = {"exposure_rotation": {
            "beta_window_days": window, "held_since_by_symbol": {SYMBOL: "%sT13:30:00+08:00" % since_date}}}
    return value


def explained_by_beta():
    """前 15 天個股漲跌、交易池另一檔不動（beta＝2）；最後 5 天個股每天 -2%，其餘不動：跌幅完全由 beta 解釋。"""
    return noise(15) + [Decimal("-0.02")] * 5, [Decimal(0)] * 20


def idiosyncratic():
    """最後 5 天個股每天 -2%、另一檔每天 +2%：交易池持平，殘差仍大幅落後。"""
    return noise(15) + [Decimal("-0.02")] * 5, noise(15) + [Decimal("0.02")] * 5


def flags(item):
    return [flag["flag"] for flag in item["flags"]]


class BetaAdjustTests(unittest.TestCase):
    def setUp(self):
        self.bundle, _, _, _ = world()

    def review(self, paths, **overrides):
        bundle, since = shaped(self.bundle, *paths)
        return bundle, build_position_review(bundle, policy(since, **overrides))

    def test_gate_off_has_no_new_keys_and_old_note(self):
        bundle, since = shaped(self.bundle, *explained_by_beta())
        review = build_position_review(bundle, policy(since, beta_adjust=False))
        item = review["items"][0]
        self.assertFalse(BETA_ITEM_FIELDS & set(item))
        self.assertNotIn("beta_adjust", review["policy"])
        self.assertEqual(review["cost_basis_note"], COST_BASIS_NOTE_V2)
        self.assertEqual(flags(item), ["relative_loss_alert"])

    def test_beta_explained_drop_does_not_alert(self):
        _, review = self.review(explained_by_beta())
        item = review["items"][0]
        self.assertEqual(review["policy"]["beta_adjust"], "v1")
        self.assertEqual(item["beta"], "2.000000")
        self.assertLessEqual(Decimal(item["relative_to_pool_since_entry"]), Decimal("-0.03"))
        self.assertLess(abs(Decimal(item["residual_since_entry"])), Decimal("0.001"))
        self.assertEqual(flags(item), [])

    def test_residual_values_are_recomputable(self):
        _, review = self.review(explained_by_beta())
        item = review["items"][0]
        for tag in ("5d", "20d"):
            expected = Decimal(item["return_" + tag]) - Decimal(item["beta"]) * Decimal(item["pool_return_" + tag])
            self.assertEqual(item["residual_" + tag], str(expected.quantize(Decimal("0.000001"))))
        expected = Decimal(item["return_since_entry"]) - Decimal(item["beta"]) * Decimal(item["pool_return_since_entry"])
        self.assertEqual(item["residual_since_entry"], str(expected.quantize(Decimal("0.000001"))))

    def test_idiosyncratic_lag_still_alerts(self):
        _, review = self.review(idiosyncratic())
        item = review["items"][0]
        self.assertLessEqual(Decimal(item["residual_since_entry"]), Decimal("-0.03"))
        self.assertEqual(flags(item), ["relative_loss_alert"])

    def test_unavailable_beta_falls_back_to_raw_relative(self):
        _, review = self.review(explained_by_beta(), window=100)
        item = review["items"][0]
        for key in BETA_ITEM_FIELDS:
            self.assertIsNone(item[key], key)
        self.assertEqual(flags(item), ["relative_loss_alert"])

    def test_brief_carries_the_new_facts(self):
        _, review = self.review(explained_by_beta())
        row = position_review_brief(review)["by_symbol"][SYMBOL]
        self.assertTrue(BETA_ITEM_FIELDS <= set(row))

    def test_invalid_policy_values_are_rejected(self):
        def errors(settings):
            found = []
            validate_position_review_policy(settings, found)
            return found

        self.assertEqual(errors({**V2, "beta_adjust": "v1"}), [])
        self.assertTrue(errors({**V2, "beta_adjust": "v2"}))
        self.assertTrue(errors({**V2, "beta_adjust": None}))
        self.assertTrue(errors({**V2, "beta_adjust": True}))
        without_context = {key: value for key, value in V2.items() if key != "context"}
        self.assertTrue(errors({**without_context, "beta_adjust": "v1"}))
        v1 = {"version": "v1", "context": "v1", "loss_alert": None, "retrace_alert_20d": None, "gain_alert": None}
        self.assertEqual(errors(v1), [])
        self.assertTrue(errors({**v1, "beta_adjust": "v1"}))

    def test_missing_exposure_rotation_gate_is_rejected(self):
        bundle, since = shaped(self.bundle, *explained_by_beta())
        with self.assertRaises(DecisionToolError):
            build_position_review(bundle, policy(since, gate=False))
        broken = policy(since)
        del broken["position_sizing"]["exposure_rotation"]["beta_window_days"]
        with self.assertRaises(DecisionToolError):
            build_position_review(bundle, broken)

    def test_tampered_residual_is_rejected_and_untouched_is_accepted(self):
        bundle, since = shaped(self.bundle, *explained_by_beta())
        value = policy(since)
        review = build_position_review(bundle, value)
        errors = []
        validate_position_review(bundle, value, review, errors)
        self.assertEqual(errors, [])
        for key in ("residual_since_entry", "residual_5d", "beta"):
            tampered = copy.deepcopy(review)
            tampered["items"][0][key] = "0.500000"
            errors = []
            validate_position_review(bundle, value, tampered, errors)
            self.assertTrue(errors, key)


if __name__ == "__main__":
    unittest.main()
