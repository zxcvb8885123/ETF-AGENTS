"""offense_facts 的 sentiment_extreme：5 日變化由資料包原始回應重算（只用內容日不晚於該日的列），缺資料維持 null 並記 data_gap。"""

import copy
import unittest
from datetime import date

from etf_agent.decision.offense import build_offense_facts
from etf_agent.perception.market_fear_greed import evaluate
from etf_agent.perception.market_fear_greed.contracts import FearGreedError, seal
from etf_agent.perception.market_fear_greed.tools import score_as_of
from test_market_fear_greed import approvals, capture, captures, pack, raw_margin, raw_pcr, raw_vix, trading_days
from test_offense import with_offense

END = date(2026, 9, 18)


def bundle_with_pair(the_pack=None):
    the_pack = the_pack or pack()
    days = [d.isoformat() for d in trading_days(END, 30)]
    series, latest = [], []
    for symbol in ("A.TW", "B.TW"):
        bars = [{"trade_date": d, "close": "%.4f" % (100 + i), "volume": "1000", "available_at": "%sT14:00:00+08:00" % d}
                for i, d in enumerate(days)]
        series.append({"symbol": symbol, "evidence_id": "price:%s" % symbol, "bars": bars})
        latest.append({"symbol": symbol, "analysis_close_price": bars[-1]["close"], "source_evidence_id": "price:%s" % symbol})
    return {
        "decision_cutoff": the_pack["decision_cutoff"], "evidence_policy": "typed_evidence_v1",
        "snapshot": {"latest_trade_date": days[-1], "latest_prices": latest, "documents": []},
        "price_series": series, "account_snapshot": {"cash": "1000", "positions": []},
        "market_fear_greed_input": {"bundle": the_pack, "result": evaluate(the_pack)},
    }


class SentimentChangeTests(unittest.TestCase):
    def setUp(self):
        self.policy = with_offense()

    def category(self, bundle):
        facts = build_offense_facts(bundle, self.policy)
        return facts["categories"]["sentiment_extreme"], facts["data_gaps"]

    def test_five_day_change_is_recomputed_from_history(self):
        bundle = bundle_with_pair()
        category, gaps = self.category(bundle)
        self.assertEqual(category["status"], "available")
        the_pack = bundle["market_fear_greed_input"]["bundle"]
        days = [d.isoformat() for d in trading_days(END, 30)]
        expected = str((__import__("decimal").Decimal(category["score"]) - __import__("decimal").Decimal(score_as_of(the_pack, days[-6]))).quantize(__import__("decimal").Decimal("0.1")))
        self.assertEqual(category["score_change_5d"], expected)
        self.assertFalse([g for g in gaps if "sentiment_extreme" in g])
        self.assertEqual(category["score"], score_as_of(the_pack, days[-1]))  # 當日分數與結果一致

    def test_history_does_not_use_rows_after_reference_day(self):
        the_pack = pack()
        days = [d.isoformat() for d in trading_days(END, 30)]
        base = score_as_of(the_pack, days[-6])
        # 把參考日之後的列改成極端值並重新封存：參考日分數不得變動
        tampered = copy.deepcopy(the_pack)
        rows = list(trading_days(END, 30))
        tampered["captures"][0] = capture("TAIFEX_VIX", tampered["captures"][0]["url"], raw_vix(rows[:-5]) + raw_vix(rows[-5:]).split("\r\n", 2)[2].replace("2", "9"))
        self.assertEqual(score_as_of(seal(tampered), days[-6]), base)

    def test_unapproved_sources_give_no_history(self):
        the_pack = pack(source_approvals=approvals(status="unverified"))
        self.assertIsNone(score_as_of(the_pack, "2026-09-11"))

    def test_future_day_and_tampered_pack_fail_closed(self):
        the_pack = pack()
        with self.assertRaises(FearGreedError):
            score_as_of(the_pack, "2026-09-21")
        bad = dict(the_pack, snapshot_id="other")
        with self.assertRaises(FearGreedError):
            score_as_of(bad, "2026-09-11")

    def test_insufficient_history_stays_null_with_gap(self):
        short = [d for d in trading_days(END, 30)][-22:]  # 參考日只剩 17 個交易日，不足 20 日窗口
        rows = [capture("TAIFEX_VIX", captures()[0]["url"], raw_vix(short)),
                capture("TAIFEX_PCR", captures()[1]["url"], raw_pcr(short)),
                capture("FINMIND_MARGIN", captures()[2]["url"], raw_margin(short))]
        category, gaps = self.category(bundle_with_pair(pack(rows=rows)))
        self.assertEqual(category["status"], "available")
        self.assertIsNone(category["score_change_5d"])
        self.assertTrue(any("sentiment_extreme" in g and "5 日變化" in g for g in gaps))

    def test_missing_pack_or_short_calendar_is_a_gap_not_a_guess(self):
        bundle = bundle_with_pair()
        bundle["market_fear_greed_input"].pop("bundle")
        category, gaps = self.category(bundle)
        self.assertIsNone(category["score_change_5d"])
        self.assertTrue(any("資料包" in g for g in gaps))
        short = bundle_with_pair()
        for row in short["price_series"]:
            row["bars"] = row["bars"][-4:]
        category, gaps = self.category(short)
        self.assertIsNone(category["score_change_5d"])

    def test_result_score_mismatch_is_not_trusted(self):
        bundle = bundle_with_pair()
        bundle["market_fear_greed_input"]["result"] = dict(bundle["market_fear_greed_input"]["result"], score="1.0")
        category, gaps = self.category(bundle)
        self.assertIsNone(category["score_change_5d"])
        self.assertTrue(any("不同" in g for g in gaps))

    def test_component_dated_after_the_trade_date_is_unavailable_with_a_gap_not_an_abort(self):
        bundle = bundle_with_pair()
        result = copy.deepcopy(bundle["market_fear_greed_input"]["result"])
        self.assertTrue(result["components"])
        result["components"][0]["as_of"] = "2026-09-21"  # 晚於最近交易日 2026-09-18
        bundle["market_fear_greed_input"]["result"] = result
        category, gaps = self.category(bundle)
        self.assertEqual(category, {"status": "unavailable"})
        self.assertTrue(any("sentiment_extreme" in g and "晚於台股最近交易日" in g for g in gaps))


if __name__ == "__main__":
    unittest.main()
