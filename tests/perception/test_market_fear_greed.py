import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from etf_agent.core import canonical_sha256
from etf_agent.decision.analysts import AnalystReportValidator, report_envelope, seal_event_market_report
from etf_agent.decision.contracts import DecisionInputValidator
from etf_agent.decision.event_sentiment_report import render_event_sentiment_report
from etf_agent.decision.market_scope import market_sentiment_channel
from etf_agent.perception.market_fear_greed import (
    FearGreedError, attach_fear_greed, build_fear_greed_input, capture_fear_greed, evaluate, validate_pair,
)
from etf_agent.perception.market_fear_greed.contracts import PACK_VERSION, SOURCES, seal
from etf_agent.perception.market_fear_greed.provider import margin_url, vix_url
from test_analysts import bundle_with_event, event_items

CUTOFF = "2026-09-20T00:55:00+00:00"
AVAILABLE = "2026-09-19T12:00:00+00:00"


def trading_days(end, count):
    days, day = [], end
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    return sorted(days)


def raw_vix(days):
    head = "交易日期\t時間(時/分/秒/毫秒)\t臺指選擇權波動率指數\t收盤前1分鐘平均指數\r\n--------\t---\t---\t---\r\n"
    return head + "".join("%s\t13450000\t\t\t%.2f\t\t%.2f\r\n" % (d.strftime("%Y%m%d"), 20 + i * 0.1, 20 + i * 0.1)
                          for i, d in enumerate(days))


def raw_pcr(days):
    head = "日期,賣權成交量,買權成交量,買賣權成交量比率%,賣權未平倉量,買權未平倉量,買賣權未平倉量比率%\n"
    return head + "".join("%s,100,100,%.2f,1,1,100\n" % (d.strftime("%Y%m%d"), 90 + i) for i, d in enumerate(reversed(days)))


def raw_margin(days):
    rows = []
    for i, d in enumerate(days):
        rows.append({"date": d.isoformat(), "name": "MarginPurchaseMoney", "TodayBalance": 600_000_000_000 + i * 1_000_000_000,
                     "YesBalance": 0, "buy": 0, "sell": 0, "Return": 0})
        rows.append({"date": d.isoformat(), "name": "MarginPurchase", "TodayBalance": 9_000_000, "YesBalance": 0,
                     "buy": 0, "sell": 0, "Return": 0})
    return json.dumps({"msg": "success", "status": 200, "data": rows})


def capture(source, url, raw, available=AVAILABLE, status="fetched"):
    return {"source": source, "url": url, "raw_text": raw, "raw_sha256": canonical_sha256(raw),
            "available_at": available, "status": status, "error": "" if status == "fetched" else "連線失敗"}


def captures(end=date(2026, 9, 18)):
    days = trading_days(end, 30)
    return [capture("TAIFEX_VIX", vix_url("202609"), raw_vix(days)),
            capture("TAIFEX_PCR", SOURCES["TAIFEX_PCR"]["url"], raw_pcr(days)),
            capture("FINMIND_MARGIN", margin_url(date(2026, 6, 1), min(end, date(2026, 9, 18))), raw_margin(days))]


def approvals(status="approved", reviewed_at="2026-09-19T00:00:00+00:00"):
    # 僅供契約驗證的人工核准，不是實際來源授權。
    return [{"source": s, "license_status": status, "terms_url": c["terms_url"], "usage_scope": "competition_research",
             "reviewed_at": reviewed_at, "approved_by": "測試", "evidence": "人工測試核准"} for s, c in SOURCES.items()]


def pack(snapshot_id="snapshot-decision-1", cutoff=CUTOFF, rows=None, source_approvals=None):
    return seal({"schema_version": PACK_VERSION, "snapshot_id": snapshot_id, "decision_cutoff": cutoff,
                 "captures": rows if rows is not None else captures(),
                 "source_approvals": source_approvals if source_approvals is not None else approvals()})


def event_report(bundle):
    rows = [dict(row, event_outlook=row["outlook"]) for row in event_items()]
    return seal_event_market_report(bundle, report_envelope(bundle, "event", "fear-greed-test"), rows)


class FakeResponse:
    def __init__(self, url, body):
        self.url, self.body = url, body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        return self.body[:limit]


class FearGreedScoreTests(unittest.TestCase):
    def setUp(self):
        self.pack = pack()

    def test_score_is_rebuilt_from_raw_values(self):
        result = evaluate(self.pack)
        self.assertEqual(result["status"], "available")
        # VIX 與 Put/Call 都在近 20 日最高點（恐懼 2.5 分），融資增幅也在最低端附近（2.5 分）。
        scores = {row["source"]: row["score"] for row in result["components"]}
        self.assertEqual(scores, {"TAIFEX_VIX": "2.5", "TAIFEX_PCR": "97.5", "FINMIND_MARGIN": "2.5"})
        self.assertEqual(result["score"], "34.2")
        self.assertEqual((result["band"], result["outlook"]), ("fear", "negative"))
        self.assertEqual(result["findings"][0]["evidence_ids"], [row["evidence_id"] for row in result["components"]])
        self.assertEqual(result["data_gaps"], [])

    def test_unapproved_source_is_diagnostic_only(self):
        rows = approvals()
        rows[0] = dict(rows[0], license_status="unverified")
        rows[1] = dict(rows[1], license_status="restricted")
        result = evaluate(pack(source_approvals=rows))
        self.assertEqual((result["status"], result["score"], result["outlook"]), ("unavailable", None, "unknown"))
        self.assertIsNotNone(result["diagnostic_score"])
        self.assertTrue(any("尚未核准" in gap for gap in result["data_gaps"]))

    def test_approval_after_cutoff_or_without_evidence_rejected(self):
        with self.assertRaises(FearGreedError):
            evaluate(pack(source_approvals=approvals(reviewed_at="2026-09-21T00:00:00+00:00")))
        rows = approvals()
        rows[0] = dict(rows[0], evidence=" ")
        with self.assertRaises(FearGreedError):
            evaluate(pack(source_approvals=rows))

    def test_capture_after_cutoff_or_naive_time_rejected(self):
        for available in ("2026-09-20T01:00:00+00:00", "2026-09-19T12:00:00"):
            rows = captures()
            rows[0] = capture(rows[0]["source"], rows[0]["url"], rows[0]["raw_text"], available=available)
            with self.assertRaises(FearGreedError):
                evaluate(pack(rows=rows))
        with self.assertRaises(FearGreedError):
            evaluate(pack(cutoff="2026-09-20T00:55:00"))

    def test_rows_after_cutoff_day_are_excluded(self):
        rows = captures(end=date(2026, 9, 25))
        result = evaluate(pack(rows=rows))
        self.assertTrue(all(row["as_of"] is None or row["as_of"] <= "2026-09-20" for row in result["components"]))

    def test_tampered_raw_url_or_rehashed_result_rejected(self):
        rows = captures()
        rows[1] = dict(rows[1], raw_text=rows[1]["raw_text"].replace("90.00", "10.00"))
        with self.assertRaises(FearGreedError):
            evaluate(pack(rows=rows))
        rows = captures()
        rows[1] = dict(rows[1], url="https://www.taifex.com.tw/cht/3/pcRatioDown")
        with self.assertRaises(FearGreedError):
            evaluate(pack(rows=rows))
        rows = captures()
        rows[0] = dict(rows[0], url=vix_url("202610"))
        with self.assertRaises(FearGreedError):
            evaluate(pack(rows=rows))
        tampered = copy.deepcopy(self.pack)
        tampered["captures"][0]["status"] = "failed"
        with self.assertRaises(FearGreedError):
            evaluate(tampered)
        result = dict(evaluate(self.pack), score="80")
        result["content_sha256"] = canonical_sha256({k: v for k, v in result.items() if k != "content_sha256"})
        with self.assertRaises(FearGreedError):
            validate_pair({"bundle": self.pack, "result": result}, "snapshot-decision-1", CUTOFF)

    def test_duplicate_capture_or_conflicting_day_rejected(self):
        rows = captures()
        with self.assertRaises(FearGreedError):
            evaluate(pack(rows=rows + [rows[1]]))
        days = trading_days(date(2026, 9, 18), 30)
        conflict = capture("TAIFEX_VIX", vix_url("202608"), raw_vix(days).replace("\t20.00\t", "\t99.00\t"))
        with self.assertRaises(FearGreedError):
            evaluate(pack(rows=rows + [conflict]))

    def test_short_sample_stale_and_failed_sources_degrade(self):
        days = trading_days(date(2026, 9, 18), 10)
        rows = captures()
        rows[0] = capture("TAIFEX_VIX", vix_url("202609"), raw_vix(days))
        rows[2] = capture("FINMIND_MARGIN", margin_url(date(2026, 6, 1), date(2026, 9, 18)), "", status="failed")
        result = evaluate(pack(rows=rows))
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("需要 20 個" in gap for gap in result["data_gaps"]))
        self.assertTrue(any("擷取失敗" in gap for gap in result["data_gaps"]))
        stale = captures(end=date(2026, 9, 8))
        result = evaluate(pack(rows=stale))
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(any("距截止日超過" in gap for gap in result["data_gaps"]))

    def test_changed_column_layout_degrades_source(self):
        rows = captures()
        rows[1] = capture("TAIFEX_PCR", SOURCES["TAIFEX_PCR"]["url"], "日期,比率\n20260918,90\n")
        result = evaluate(pack(rows=rows))
        self.assertTrue(any("格式無法解析" in gap for gap in result["data_gaps"]))
        self.assertEqual(result["status"], "available")
        self.assertEqual(len([row for row in result["components"] if row["status"] == "available"]), 2)


class FearGreedChannelTests(unittest.TestCase):
    def setUp(self):
        self.bundle = bundle_with_event()
        self.pair = {"bundle": pack(), "result": evaluate(pack())}

    def test_channel_without_input_is_unchanged(self):
        channel = market_sentiment_channel(self.bundle)
        self.assertNotIn("fear_greed", channel)
        self.assertEqual((channel["status"], channel["outlook"]), ("unavailable", "unknown"))

    def test_attached_index_becomes_market_channel_and_validates(self):
        attached = attach_fear_greed(self.bundle, self.pair)
        self.assertEqual(DecisionInputValidator(attached).validate(), [])
        channel = market_sentiment_channel(attached)
        self.assertEqual((channel["status"], channel["outlook"]), ("available", "negative"))
        self.assertEqual(channel["fear_greed"]["score"], "34.2")
        self.assertFalse(any(gap.startswith("缺少涵蓋整體台股") for gap in channel["data_gaps"]))
        output = event_report(attached)
        self.assertEqual(AnalystReportValidator(attached, "event").validate(output), [])
        text = render_event_sentiment_report(attached, output)
        self.assertIn("**結果：負面。**", text)
        self.assertIn("台股恐懼貪婪指標", text)

    def test_unavailable_index_keeps_unknown_and_explains_gap(self):
        rows = approvals("unverified")
        pair = {"bundle": pack(source_approvals=rows), "result": evaluate(pack(source_approvals=rows))}
        channel = market_sentiment_channel(attach_fear_greed(self.bundle, pair))
        self.assertEqual((channel["status"], channel["outlook"]), ("unavailable", "unknown"))
        self.assertTrue(any(gap.startswith("恐懼貪婪指標：") for gap in channel["data_gaps"]))

    def test_mismatched_snapshot_or_rewritten_report_rejected(self):
        wrong = {"bundle": pack(snapshot_id="other"), "result": evaluate(pack(snapshot_id="other"))}
        with self.assertRaises(FearGreedError):
            attach_fear_greed(self.bundle, wrong)
        attached = attach_fear_greed(self.bundle, self.pair)
        broken = dict(attached, market_fear_greed_input={"bundle": self.pair["bundle"], "result": dict(self.pair["result"], outlook="positive")})
        self.assertTrue(any("恐懼貪婪" in error for error in DecisionInputValidator(broken).validate()))
        output = event_report(attached)
        output["market_sentiment"]["outlook"] = "positive"
        output["content_sha256"] = canonical_sha256({k: v for k, v in output.items() if k != "content_sha256"})
        self.assertTrue(AnalystReportValidator(attached, "event").validate(output))


class FearGreedServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.approvals = self.root / "approvals.json"
        self.approvals.write_text(json.dumps({"sources": approvals()}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def provider(self, now):
        days = trading_days(date(2026, 9, 18), 30)
        bodies = {vix_url(m): raw_vix(days).encode("cp950") for m in ("202607", "202608", "202609")}
        bodies[SOURCES["TAIFEX_PCR"]["url"]] = raw_pcr(days).encode("utf-8")
        from etf_agent.perception.market_fear_greed import FearGreedProvider

        def opener(request, timeout):
            url = request.full_url
            if url.startswith("https://api.finmindtrade.com"):
                self.assertEqual(request.headers["Authorization"], "Bearer token")
                return FakeResponse(url, raw_margin(days).encode("utf-8"))
            return FakeResponse(url, bodies[url])
        provider = FearGreedProvider(opener=opener, token="token")
        provider.capture = (lambda original: lambda source, url, **kw: dict(original(source, url, **kw), available_at=now.isoformat()))(provider.capture)
        return provider

    def test_capture_then_build_uses_only_runs_before_cutoff(self):
        early = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
        late = datetime(2026, 9, 20, 3, tzinfo=timezone.utc)
        first = capture_fear_greed(self.root, provider=self.provider(early), now=early)
        self.assertEqual(first["failures"], [])
        capture_fear_greed(self.root, provider=self.provider(late), now=late)
        pair = build_fear_greed_input(self.root, self.approvals, "snapshot-decision-1", CUTOFF)
        self.assertTrue(all(row["available_at"] == early.isoformat() for row in pair["bundle"]["captures"]))
        self.assertEqual(pair["result"]["status"], "available")
        self.assertIsNone(build_fear_greed_input(self.root, self.approvals, "s", "2026-09-19T00:00:00+00:00"))

    def test_missing_token_or_redirect_recorded_as_failure(self):
        from etf_agent.perception.market_fear_greed import FearGreedProvider
        provider = FearGreedProvider(opener=lambda request, timeout: FakeResponse("https://evil.example/x", b"x"), token="")
        rows = provider.capture_all(date(2026, 9, 18))
        self.assertTrue(all(row["status"] == "failed" for row in rows))
        self.assertIn("FINMIND_TOKEN", rows[-1]["error"])

    def test_empty_current_month_vix_is_not_a_failure(self):
        from etf_agent.perception.market_fear_greed import FearGreedProvider
        provider = FearGreedProvider(opener=lambda request, timeout: FakeResponse(request.full_url, b""), token="token")
        rows = provider.capture_all(date(2026, 10, 1))
        status = {row["url"]: row["status"] for row in rows}
        self.assertEqual(status[vix_url("202610")], "fetched")
        self.assertEqual(status[vix_url("202609")], "failed")

    def test_tampered_saved_run_rejected(self):
        now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
        path = Path(capture_fear_greed(self.root, provider=self.provider(now), now=now)["run_path"])
        payload = json.loads((path / "captures.json").read_text(encoding="utf-8"))
        payload["items"][0]["raw_text"] = "x"
        (path / "captures.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(FearGreedError):
            build_fear_greed_input(self.root, self.approvals, "snapshot-decision-1", CUTOFF)
