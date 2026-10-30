import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.core import canonical_sha256, decimal_string
from etf_agent.decision.contracts import DecisionContext, DecisionInputValidator, decision_bundle_sha256
from etf_agent.decision.market_scope import market_sentiment_channel, overnight_market_channel
from etf_agent.decision.sizing import validate_cash_stance
from etf_agent.perception.overnight_market import (
    OvernightMarketError, OvernightProvider, attach_overnight_market, build_overnight_market_input,
    capture_overnight_market, evaluate, validate_pair,
)
from etf_agent.perception.overnight_market.contracts import PACK_VERSION, SOURCES, SYMBOLS, seal
from etf_agent.perception.overnight_market.provider import chart_url, raw_text
from etf_agent.perception.overnight_market.tools import session_end, window_start
from test_analysts import bundle_with_event

SNAPSHOT_ID = "snapshot-decision-1"
LATEST_TRADE_DATE = "2026-09-18"  # 週五
# 週一 08:30 台北；台股週五收盤後僅週五的美股時段已完成（夏令，16:00 EDT = 20:00 UTC）。
MONDAY_CUTOFF = "2026-09-21T00:30:00+00:00"
FETCHED = "2026-09-21T00:00:00+00:00"
FRIDAY_NIGHT_CUTOFF = "2026-09-18T13:00:00+00:00"  # 週五 21:00 台北
FRIDAY_NIGHT_FETCHED = "2026-09-18T12:00:00+00:00"


def weekdays(end, count):
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def make_bars(end, final_returns, early=28):
    """前 early 個交易日小幅波動，最後以 100 為基準依 final_returns 連乘；收盤價為精確 Decimal。"""
    closes = [Decimal("90") + Decimal(i % 5) for i in range(early)] + [Decimal(100)]
    for r in final_returns:
        closes.append(closes[-1] * (1 + Decimal(r)))
    days = weekdays(end, len(closes))
    return [{"date": d.isoformat(), "close": decimal_string(c)} for d, c in zip(days, closes)]


def capture_row(symbol, bars, available=FETCHED, status="fetched", url=None):
    text = raw_text(symbol, bars) if status == "fetched" else ""
    return {"source": "YAHOO_FINANCE", "symbol": symbol, "url": url or chart_url(symbol), "raw_text": text,
            "raw_sha256": canonical_sha256(text), "available_at": available, "status": status,
            "error": "" if status == "fetched" else "連線失敗"}


DEFAULT_RETURNS = {"^GSPC": ["-0.014"], "^IXIC": ["-0.02"], "^SOX": ["-0.02"], "EWT": ["-0.003"], "TSM": ["-0.004"]}


def captures(end=date(2026, 9, 18), returns=None, available=FETCHED, failed=()):
    returns = dict(DEFAULT_RETURNS, **(returns or {}))
    rows = []
    for symbol in SYMBOLS:
        if symbol in failed:
            rows.append(capture_row(symbol, [], available, "failed"))
        else:
            rows.append(capture_row(symbol, make_bars(end, returns[symbol]), available))
    return rows


def approvals(status="approved", reviewed_at="2026-09-19T00:00:00+00:00"):
    # 僅供契約驗證的人工核准，不是實際來源授權。
    return [{"source": s, "license_status": status, "terms_url": c["terms_url"], "usage_scope": "competition_research",
             "reviewed_at": reviewed_at, "approved_by": "測試", "evidence": "人工測試核准"} for s, c in SOURCES.items()]


def pack(snapshot_id=SNAPSHOT_ID, cutoff=MONDAY_CUTOFF, trade_date=LATEST_TRADE_DATE, rows=None, source_approvals=None):
    return seal({"schema_version": PACK_VERSION, "snapshot_id": snapshot_id, "decision_cutoff": cutoff,
                 "latest_trade_date": trade_date, "captures": rows if rows is not None else captures(),
                 "source_approvals": source_approvals if source_approvals is not None else approvals()})


def pair_of(**kwargs):
    value = pack(**kwargs)
    return {"bundle": value, "result": evaluate(value)}


def by_symbol(result):
    return {row["symbol"]: row for row in result["components"]}


class SessionTimeTests(unittest.TestCase):
    def test_session_end_follows_new_york_dst(self):
        self.assertEqual(session_end("2026-07-15"), datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(session_end("2026-01-15"), datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc))
        # 夏令結束前後一週的切換。
        self.assertEqual(session_end("2026-10-30"), datetime(2026, 10, 30, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(session_end("2026-11-02"), datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc))

    def test_window_starts_at_taipei_close(self):
        self.assertEqual(window_start("2026-09-18"), datetime(2026, 9, 18, 5, 30, tzinfo=timezone.utc))
        with self.assertRaises(OvernightMarketError):
            window_start("20260918")


class EvaluateTests(unittest.TestCase):
    def test_monday_run_includes_friday_session_only(self):
        result = evaluate(pack())
        self.assertEqual(result["status"], "available")
        sox = by_symbol(result)["^SOX"]
        self.assertEqual(sox["sessions"], ["2026-09-18"])
        self.assertEqual(sox["cumulative_change_pct"], "-2")
        self.assertEqual(sox["change_pct"], "-2")
        self.assertEqual(result["signal_pct"], "-1.7")  # (-2 + -1.4) / 2
        self.assertEqual(result["outlook"], "negative")
        self.assertIsNone(result["night_session"])
        self.assertIn("台指期夜盤尚未接入", result["data_gaps"])
        self.assertEqual(len(result["limitations"]), 3)
        self.assertEqual(result["findings"][0]["evidence_ids"], [row["evidence_id"] for row in result["components"]])
        self.assertTrue(all(row["evidence_id"].startswith("overnight:YAHOO_FINANCE:%s:" % row["symbol"]) for row in result["components"]))
        self.assertIsNotNone(sox["percentile_20d"])

    def test_friday_night_run_has_no_completed_session(self):
        rows = captures(available=FRIDAY_NIGHT_FETCHED)
        result = evaluate(pack(cutoff=FRIDAY_NIGHT_CUTOFF, rows=rows, source_approvals=approvals(reviewed_at="2026-09-18T00:00:00+00:00")))
        self.assertEqual((result["status"], result["outlook"]), ("unavailable", "unknown"))
        self.assertEqual(result["findings"], [])
        self.assertTrue(all(row["status"] == "unavailable" for row in result["components"]))
        self.assertTrue(any("視窗內沒有已完成的美股交易時段" in gap for gap in result["data_gaps"]))

    def test_bar_finishing_after_fetch_is_dropped(self):
        # 週一 US 時段還沒收盤就抓：當日 bar 丟棄，不得拿盤中價當收盤。
        rows = captures(end=date(2026, 9, 21), available="2026-09-21T15:00:00+00:00")
        result = evaluate(pack(cutoff="2026-09-21T16:00:00+00:00", rows=rows))
        sox = by_symbol(result)["^SOX"]
        self.assertEqual(sox["sessions"], ["2026-09-18"])

    def test_holiday_cumulates_every_session(self):
        # 台股週三收盤後到週一，週三、四、五的美股時段都已完成，連乘 (1+r)。
        rows = captures(returns={s: ["0.01", "0.01", "0.01"] for s in SYMBOLS})
        result = evaluate(pack(trade_date="2026-09-16", rows=rows))
        sox = by_symbol(result)["^SOX"]
        self.assertEqual(sox["sessions"], ["2026-09-16", "2026-09-17", "2026-09-18"])
        self.assertEqual(sox["cumulative_change_pct"], "3.03")
        self.assertEqual(sox["change_pct"], "1")
        self.assertEqual(result["outlook"], "positive")

    def test_thresholds(self):
        for sox, gspc, expected in (("0.01", "0.01", "positive"), ("0.0099", "0.0099", "neutral"),
                                    ("-0.015", "-0.015", "negative"), ("-0.0149", "-0.0149", "neutral")):
            rows = captures(returns={"^SOX": [sox], "^GSPC": [gspc], "EWT": ["0"], "TSM": ["0"]})
            self.assertEqual(evaluate(pack(rows=rows))["outlook"], expected, (sox, gspc))

    def test_opposite_proxy_makes_mixed_only_when_both_exceed_floor(self):
        base = {"^SOX": ["0.02"], "^GSPC": ["0.01"]}
        rows = captures(returns=dict(base, EWT=["-0.01"], TSM=["-0.01"]))
        self.assertEqual(evaluate(pack(rows=rows))["outlook"], "mixed")
        rows = captures(returns=dict(base, EWT=["-0.0075"], TSM=["-0.0075"]))
        self.assertEqual(evaluate(pack(rows=rows))["outlook"], "positive")
        rows = captures(returns=dict(base, EWT=["0.01"], TSM=["0.01"]))
        self.assertEqual(evaluate(pack(rows=rows))["outlook"], "positive")

    def test_unverified_source_is_diagnostic_only(self):
        result = evaluate(pack(source_approvals=approvals("unverified")))
        self.assertEqual((result["status"], result["outlook"]), ("unavailable", "unknown"))
        self.assertEqual(result["findings"], [])
        self.assertTrue(all(row["status"] == "available" and row["evidence_id"] is None and not row["approved"]
                            for row in result["components"]))
        self.assertTrue(any("尚未核准使用" in gap for gap in result["data_gaps"]))
        self.assertEqual(evaluate(pack(source_approvals=[]))["status"], "unavailable")

    def test_proxy_failure_degrades_but_main_failure_blocks(self):
        result = evaluate(pack(rows=captures(failed=("TSM",))))
        self.assertEqual(result["status"], "available")
        self.assertEqual(by_symbol(result)["TSM"]["status"], "unavailable")
        self.assertTrue(any(gap.startswith("TSM 擷取失敗") for gap in result["data_gaps"]))
        self.assertEqual(len(result["findings"][0]["evidence_ids"]), 4)
        result = evaluate(pack(rows=captures(failed=("^SOX",))))
        self.assertEqual((result["status"], result["outlook"]), ("unavailable", "unknown"))
        self.assertTrue(any("主訊號標的不可用" in gap for gap in result["data_gaps"]))
        result = evaluate(pack(rows=captures()[:3]))
        self.assertTrue(any(gap.startswith("TSM 沒有原始回應") for gap in result["data_gaps"]))

    def test_missing_baseline_close_is_unavailable(self):
        rows = captures()
        rows[2] = capture_row("^SOX", make_bars(date(2026, 9, 18), ["-0.02"], early=0)[1:])
        result = evaluate(pack(rows=rows))
        self.assertEqual(by_symbol(result)["^SOX"]["status"], "unavailable")
        self.assertTrue(any("缺少視窗前一個時段的收盤價" in gap for gap in result["data_gaps"]))

    def test_short_history_has_no_percentile_but_stays_available(self):
        rows = captures()
        rows[0] = capture_row("^GSPC", make_bars(date(2026, 9, 18), ["-0.014"], early=3))
        result = evaluate(pack(rows=rows))
        self.assertEqual(result["status"], "available")
        self.assertIsNone(by_symbol(result)["^GSPC"]["percentile_20d"])

    def test_percentile_ranks_latest_session_in_twenty_returns(self):
        result = evaluate(pack())
        # 近 20 個單日報酬中有 4 個比 -2% 更低（週期性的 -4.26%），另有 1 個同值：(4 + 0.5) / 20。
        self.assertEqual(by_symbol(result)["^SOX"]["percentile_20d"], "22.5")


class RejectTests(unittest.TestCase):
    def test_capture_after_cutoff_or_naive_time_rejected(self):
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(rows=captures(available="2026-09-21T01:00:00+00:00")))
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(rows=captures(available="2026-09-21T00:00:00")))
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(cutoff="2026-09-21T00:30:00"))

    def test_approval_after_cutoff_or_without_evidence_rejected(self):
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(source_approvals=approvals(reviewed_at="2026-09-22T00:00:00+00:00")))
        # 未核准的紀錄時間較晚不影響結果。
        self.assertEqual(evaluate(pack(source_approvals=approvals("unverified", reviewed_at="2026-09-22T00:00:00+00:00")))["status"], "unavailable")
        rows = approvals()
        rows[0]["evidence"] = ""
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(source_approvals=rows))
        rows = approvals()
        rows[0]["terms_url"] = "https://example.com/terms"
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(source_approvals=rows))

    def test_tampered_raw_url_host_or_duplicates_rejected(self):
        rows = captures()
        rows[0]["raw_text"] = rows[0]["raw_text"].replace("100", "101")
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(rows=rows))
        for url in ("https://evil.example.com/v8/finance/chart/%5EGSPC",
                    "https://query1.finance.yahoo.com/v8/finance/chart/%5EIXIC"):
            with self.assertRaises(OvernightMarketError):
                evaluate(pack(rows=[capture_row("^GSPC", make_bars(date(2026, 9, 18), ["0"]), url=url)]))
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(rows=captures() + captures()[:1]))
        with self.assertRaises(OvernightMarketError):
            evaluate(dict(pack(), extra=1))
        with self.assertRaises(OvernightMarketError):
            evaluate(dict(pack(), snapshot_id="other"))  # 雜湊不符

    def test_unknown_symbol_or_source_rejected(self):
        row = capture_row("^GSPC", make_bars(date(2026, 9, 18), ["0"]))
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(rows=[dict(row, symbol="^DJI")]))
        with self.assertRaises(OvernightMarketError):
            evaluate(pack(rows=[dict(row, source="OTHER")]))

    def test_rewritten_result_or_mismatched_identity_rejected(self):
        value = pair_of()
        self.assertEqual(validate_pair(value, SNAPSHOT_ID, MONDAY_CUTOFF, LATEST_TRADE_DATE)["status"], "available")
        rewritten = copy.deepcopy(value)
        rewritten["result"]["outlook"] = "positive"
        with self.assertRaises(OvernightMarketError):
            validate_pair(rewritten, SNAPSHOT_ID, MONDAY_CUTOFF)
        rewritten = copy.deepcopy(value)
        rewritten["result"]["components"][0]["cumulative_change_pct"] = "5"
        rewritten["result"] = seal(rewritten["result"])  # 連同雜湊一起改寫仍與重建不一致
        with self.assertRaises(OvernightMarketError):
            validate_pair(rewritten, SNAPSHOT_ID, MONDAY_CUTOFF)
        for args in (("other", MONDAY_CUTOFF, None), (SNAPSHOT_ID, "2026-09-21T01:30:00+00:00", None),
                     (SNAPSHOT_ID, MONDAY_CUTOFF, "2026-09-17")):
            with self.assertRaises(OvernightMarketError):
                validate_pair(value, *args)

    def test_unparseable_raw_degrades_source(self):
        rows = captures()
        text = json.dumps({"symbol": "TSM", "bars": [{"date": "2026-09-18", "close": "abc"}]})
        rows[4] = dict(capture_row("TSM", []), raw_text=text, raw_sha256=canonical_sha256(text))
        result = evaluate(pack(rows=rows))
        self.assertEqual(by_symbol(result)["TSM"]["status"], "unavailable")
        self.assertTrue(any(gap.startswith("TSM 格式無法解析") for gap in result["data_gaps"]))


class ProviderServiceTests(unittest.TestCase):
    def test_provider_records_failure_without_raising(self):
        def fetcher(symbol, start, end):
            if symbol == "TSM":
                raise ValueError("Yahoo 無回應")
            return make_bars(date(2026, 9, 18), ["-0.01"])

        rows = OvernightProvider(fetcher).capture_all(datetime(2026, 9, 21, tzinfo=timezone.utc))
        self.assertEqual([r["symbol"] for r in rows], list(SYMBOLS))
        failed = [r for r in rows if r["status"] == "failed"]
        self.assertEqual([r["symbol"] for r in failed], ["TSM"])
        self.assertIn("Yahoo 無回應", failed[0]["error"])
        self.assertEqual(rows[0]["url"], "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC")
        self.assertEqual(rows[0]["raw_sha256"], canonical_sha256(rows[0]["raw_text"]))

    def test_capture_then_build_uses_only_runs_before_cutoff(self):
        provider = OvernightProvider(lambda symbol, start, end: make_bars(date(2026, 9, 18), ["-0.01"]))
        with tempfile.TemporaryDirectory() as directory:
            root, approval_path = Path(directory) / "overnight", Path(directory) / "sources.json"
            approval_path.write_text(json.dumps({"sources": approvals()}), encoding="utf-8")
            output = capture_overnight_market(root, provider, datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc))
            self.assertEqual((output["captured"], output["failures"]), (5, []))
            self.assertIsNone(build_overnight_market_input(
                root, approval_path, SNAPSHOT_ID, "2026-09-20T23:59:00+00:00", LATEST_TRADE_DATE))
            built = build_overnight_market_input(root, approval_path, SNAPSHOT_ID, MONDAY_CUTOFF, LATEST_TRADE_DATE)
            self.assertEqual(built["result"]["status"], "available")
            self.assertEqual(validate_pair(built, SNAPSHOT_ID, MONDAY_CUTOFF, LATEST_TRADE_DATE), built["result"])
            # 再抓一次相同內容仍可重入；損壞已保存的 run 會被拒絕。
            capture_overnight_market(root, provider, datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc))
            target = next(root.glob("*/overnight-*/captures.json"))
            target.write_text(target.read_text(encoding="utf-8").replace("TSM", "TSN", 1), encoding="utf-8")
            with self.assertRaises(OvernightMarketError):
                build_overnight_market_input(root, approval_path, SNAPSHOT_ID, MONDAY_CUTOFF, LATEST_TRADE_DATE)

    def test_missing_approval_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = OvernightProvider(lambda symbol, start, end: make_bars(date(2026, 9, 18), ["0"]))
            capture_overnight_market(Path(directory) / "o", provider, datetime(2026, 9, 21, tzinfo=timezone.utc))
            with self.assertRaises(OvernightMarketError):
                build_overnight_market_input(Path(directory) / "o", Path(directory) / "missing.json", SNAPSHOT_ID,
                                             MONDAY_CUTOFF, LATEST_TRADE_DATE)

    def test_shipped_config_is_approved_by_user(self):
        path = Path(__file__).resolve().parents[2] / "config" / "overnight_market_sources.json"
        rows = json.loads(path.read_text(encoding="utf-8"))["sources"]
        self.assertEqual([(r["source"], r["license_status"], r["approved_by"]) for r in rows],
                         [("YAHOO_FINANCE", "approved", "apollo")])


def with_trade_date(bundle, trade_date=LATEST_TRADE_DATE):
    bundle = copy.deepcopy(bundle)
    bundle["snapshot"]["latest_trade_date"] = trade_date
    bundle["snapshot_sha256"] = canonical_sha256(bundle["snapshot"])
    bundle["bundle_sha256"] = decision_bundle_sha256(bundle)
    return bundle


class DecisionIntegrationTests(unittest.TestCase):
    def setUp(self):
        base = with_trade_date(bundle_with_event())
        self.snapshot_id, self.cutoff = base["snapshot_id"], base["decision_cutoff"]
        self.base = base

    def pair(self, **kwargs):
        value = pack(snapshot_id=self.snapshot_id, cutoff=self.cutoff, **kwargs)
        return {"bundle": value, "result": evaluate(value)}

    def attached(self, **kwargs):
        # 決策截止時間由 fixture 決定；抓取時間必須不晚於它。
        return attach_overnight_market(self.base, self.pair(**kwargs))

    def rows(self):
        cutoff = datetime.fromisoformat(self.cutoff)
        return captures(available=(cutoff - timedelta(minutes=5)).isoformat())

    def test_bundle_without_input_is_unchanged(self):
        self.assertNotIn("overnight_market_input", self.base)
        self.assertIsNone(overnight_market_channel(self.base))
        self.assertEqual(DecisionInputValidator(self.base).validate(), [])
        self.assertNotIn("overnight", json.dumps(market_sentiment_channel(self.base)))

    def test_attached_input_validates_and_stays_out_of_market_sentiment(self):
        attached = self.attached(rows=self.rows())
        self.assertEqual(DecisionInputValidator(attached).validate(), [])
        self.assertEqual(market_sentiment_channel(attached), market_sentiment_channel(self.base))
        channel = overnight_market_channel(attached)
        self.assertEqual(channel["scope"], "US_OVERNIGHT_AND_TW_PROXY")
        self.assertEqual((channel["status"], channel["outlook"]), ("available", "negative"))
        self.assertEqual(len(channel["components"]), 5)
        self.assertIsNone(channel["night_session"])

    def test_unavailable_channel_hides_component_numbers(self):
        attached = self.attached(rows=self.rows(), source_approvals=approvals("unverified"))
        self.assertEqual(DecisionInputValidator(attached).validate(), [])
        channel = overnight_market_channel(attached)
        self.assertEqual((channel["status"], channel["outlook"], channel["components"]), ("unavailable", "unknown", []))

    def test_rewritten_or_stale_input_rejected_by_bundle_validator(self):
        attached = self.attached(rows=self.rows())
        rewritten = copy.deepcopy(attached)
        rewritten["overnight_market_input"]["result"]["outlook"] = "positive"
        rewritten["bundle_sha256"] = decision_bundle_sha256(rewritten)
        self.assertTrue(any(error.startswith("隔夜市場輸入：") for error in DecisionInputValidator(rewritten).validate()))
        stale = with_trade_date(attached, "2026-09-17")
        self.assertTrue(any(error.startswith("隔夜市場輸入：") for error in DecisionInputValidator(stale).validate()))
        extra = dict(attached, overnight_market_inputs=1)
        self.assertTrue(DecisionInputValidator(extra).validate())

    def test_cash_stance_may_cite_overnight_evidence_but_stocks_may_not(self):
        context = DecisionContext(self.attached(rows=self.rows()))
        ids = [row["evidence_id"] for row in context.bundle["overnight_market_input"]["result"]["components"]]
        stance = {"level": "neutral", "breadth": "balanced", "rationale": "隔夜美股偏弱。", "evidence_ids": ids[:2]}
        errors = []
        validate_cash_stance(context, stance, "cash_stance", errors)
        self.assertEqual(errors, [])
        errors = []
        validate_cash_stance(context, dict(stance, evidence_ids=["overnight:YAHOO_FINANCE:^GSPC:" + "0" * 20]), "cash_stance", errors)
        self.assertTrue(any("引用不存在" in error for error in errors))
        errors = []
        context.validate_evidence(ids[:1], "2330.TW", "claim", errors)
        self.assertTrue(any("只能用於現金姿態" in error for error in errors))

    def test_unverified_input_registers_no_evidence(self):
        context = DecisionContext(self.attached(rows=self.rows(), source_approvals=approvals("unverified")))
        self.assertEqual(context.overnight_evidence_ids, set())
        errors = []
        validate_cash_stance(context, {"level": "neutral", "rationale": "x", "evidence_ids": ["overnight:YAHOO_FINANCE:^GSPC:" + "0" * 20]},
                             "cash_stance", errors)
        self.assertTrue(errors)


class ReportLineTests(unittest.TestCase):
    def test_available_and_unavailable_lines(self):
        from etf_agent.decision_delivery.renderer import _overnight_line
        base = with_trade_date(bundle_with_event())
        cutoff = datetime.fromisoformat(base["decision_cutoff"])
        rows = captures(available=(cutoff - timedelta(minutes=5)).isoformat())
        value = pack(snapshot_id=base["snapshot_id"], cutoff=base["decision_cutoff"], rows=rows)
        attached = attach_overnight_market(base, {"bundle": value, "result": evaluate(value)})
        line = _overnight_line(overnight_market_channel(attached))
        self.assertIn("隔夜市場背景", line)
        self.assertIn("overnight-v1", line)
        self.assertIn("台指期夜盤尚未接入", line)
        value = pack(snapshot_id=base["snapshot_id"], cutoff=base["decision_cutoff"], rows=rows, source_approvals=approvals("unverified"))
        attached = attach_overnight_market(base, {"bundle": value, "result": evaluate(value)})
        self.assertIn("資料不足", _overnight_line(overnight_market_channel(attached)))
