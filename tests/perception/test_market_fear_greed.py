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
from etf_agent.perception.market_fear_greed.contracts import (
    EXTRA_SOURCES, LEVEL_WINDOW, PACK_VERSION, PACK_VERSION_V11, PACK_VERSION_V12, SOURCES, seal,
)
from etf_agent.perception.market_fear_greed.provider import margin_url, recent_months, twse_url, vix_url
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


def raw_twse(days, base=40000):
    rows = [["%d/%02d/%02d" % (d.year - 1911, d.month, d.day), "1", "1", "1", "{:,.2f}".format(base + i * 10), "0"]
            for i, d in enumerate(days)]
    return json.dumps({"stat": "OK", "fields": ["日期", "成交股數", "成交金額", "成交筆數", "發行量加權股價指數", "漲跌點數"],
                       "data": rows})


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
        for month in recent_months(now.astimezone(timezone(timedelta(hours=8))).date(), 14):
            bodies[twse_url(month)] = raw_twse(days).encode("utf-8")
        from etf_agent.perception.market_fear_greed import FearGreedProvider

        def opener(request, timeout):
            url = request.full_url
            if url.startswith("https://api.finmindtrade.com"):
                self.assertEqual(request.headers["Authorization"], "Bearer token")
                return FakeResponse(url, raw_margin(days).encode("utf-8"))
            return FakeResponse(url, bodies[url])
        provider = FearGreedProvider(opener=opener, token="token", sleep=lambda seconds: None)
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


def long_margin(days, step=1_000_000_000):
    rows = [{"date": d.isoformat(), "name": "MarginPurchaseMoney", "TodayBalance": 600_000_000_000 + i * step,
             "YesBalance": 0, "buy": 0, "sell": 0, "Return": 0} for i, d in enumerate(days)]
    return json.dumps({"msg": "success", "status": 200, "data": rows})


def v11_approvals(taiex=True):
    rows = approvals()
    if taiex:
        spec = EXTRA_SOURCES["TWSE_TAIEX"]
        rows.append({"source": "TWSE_TAIEX", "license_status": "approved", "terms_url": spec["terms_url"],
                     "usage_scope": "competition_research", "reviewed_at": "2026-09-19T00:00:00+00:00",
                     "approved_by": "測試", "evidence": "人工測試核准"})
    return rows


def v11_pack(count=LEVEL_WINDOW + 5, step=1_000_000_000, source_approvals=None, drop_index=False):
    days = trading_days(date(2026, 9, 18), count)
    rows = [capture("TAIFEX_VIX", vix_url("202609"), raw_vix(days[-30:])),
            capture("TAIFEX_PCR", SOURCES["TAIFEX_PCR"]["url"], raw_pcr(days[-30:])),
            capture("FINMIND_MARGIN", margin_url(date(2025, 8, 1), date(2026, 9, 18)), long_margin(days, step))]
    months = sorted({d.strftime("%Y%m") for d in days})
    for month in ([] if drop_index else months):
        rows.append(capture("TWSE_TAIEX", twse_url(month), raw_twse([d for d in days if d.strftime("%Y%m") == month],
                                                                   base=40000 + 10 * days.index(next(d for d in days if d.strftime("%Y%m") == month)))))
    return seal({"schema_version": PACK_VERSION_V11, "snapshot_id": "snapshot-decision-1", "decision_cutoff": CUTOFF,
                 "captures": rows, "source_approvals": source_approvals if source_approvals is not None else v11_approvals()})


class FearGreedLevelTests(unittest.TestCase):
    def test_level_component_uses_margin_over_index_percentile(self):
        # 融資每天加 10 億、指數每天加 10 點：比值逐日上升，最新值在窗口最高點。
        result = evaluate(v11_pack())
        self.assertEqual((result["schema_version"], result["score_version"]), ("market-fear-greed-result-1.1", "fear-greed-v2"))
        level = {row["source"]: row for row in result["components"]}["FINMIND_MARGIN_LEVEL"]
        self.assertEqual((level["status"], level["approved"], level["sample_size"]), ("available", True, LEVEL_WINDOW))
        self.assertEqual(level["score"], "99.8")
        self.assertEqual(len(result["components"]), 4)
        self.assertEqual(result["findings"][0]["evidence_ids"], [row["evidence_id"] for row in result["components"]])

    def test_falling_ratio_scores_low(self):
        level = {r["source"]: r for r in evaluate(v11_pack(step=-100_000_000))["components"]}["FINMIND_MARGIN_LEVEL"]
        self.assertEqual(level["score"], "0.2")

    def test_unapproved_index_keeps_level_diagnostic_only(self):
        result = evaluate(v11_pack(source_approvals=v11_approvals(taiex=False)))
        level = {r["source"]: r for r in result["components"]}["FINMIND_MARGIN_LEVEL"]
        self.assertEqual((level["status"], level["approved"]), ("available", False))
        self.assertEqual(len([r for r in result["components"] if r["approved"]]), 3)
        self.assertTrue(any("尚未核准" in gap and "百分位" in gap for gap in result["data_gaps"]))

    def test_short_history_or_missing_index_degrades_without_filling(self):
        short = {r["source"]: r for r in evaluate(v11_pack(count=LEVEL_WINDOW - 1))["components"]}["FINMIND_MARGIN_LEVEL"]
        self.assertEqual(short["status"], "unavailable")
        missing = evaluate(v11_pack(drop_index=True))
        self.assertTrue(any("缺少融資金額或加權指數" in gap for gap in missing["data_gaps"]))
        self.assertEqual(missing["status"], "available")

    def test_index_month_after_cutoff_or_tampered_rejected(self):
        base = v11_pack()
        rows = [dict(c) for c in base["captures"]]
        rows[3] = capture("TWSE_TAIEX", twse_url("202610"), rows[3]["raw_text"])
        with self.assertRaises(FearGreedError):
            evaluate(seal(dict(base, captures=rows)))
        rows = [dict(c) for c in base["captures"]]
        rows[3] = dict(rows[3], raw_text="{}")
        with self.assertRaises(FearGreedError):
            evaluate(seal(dict(base, captures=rows)))

    def test_changed_index_layout_degrades(self):
        base = v11_pack()
        rows = [dict(c) for c in base["captures"]]
        for i, row in enumerate(rows):
            if row["source"] == "TWSE_TAIEX":
                bad = json.dumps({"stat": "OK", "fields": ["x"], "data": []})
                rows[i] = capture("TWSE_TAIEX", row["url"], bad)
        result = evaluate(seal(dict(base, captures=rows)))
        self.assertTrue(any("加權指數" in gap and "無法解析" in gap for gap in result["data_gaps"]))

    def test_v10_pack_ignores_taiex_approval_and_stays_v1(self):
        result = evaluate(pack())
        self.assertEqual((result["schema_version"], result["score_version"]), ("market-fear-greed-result-1.0", "fear-greed-v1"))
        with self.assertRaises(FearGreedError):
            evaluate(pack(source_approvals=v11_approvals()))


class FearGreedBackfillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.approvals = self.root / "approvals.json"
        self.approvals.write_text(json.dumps({"sources": v11_approvals()}), encoding="utf-8")
        self.fetched = []

    def tearDown(self):
        self.tmp.cleanup()

    def provider(self, now):
        from etf_agent.perception.market_fear_greed import FearGreedProvider
        days = trading_days(date(2026, 9, 18), 30)

        def opener(request, timeout):
            url = request.full_url
            if "twse.com.tw" in url:
                self.fetched.append(url)
                return FakeResponse(url, raw_twse(days).encode("utf-8"))
            if url.startswith("https://api.finmindtrade.com"):
                return FakeResponse(url, raw_margin(days).encode("utf-8"))
            if "PutCallRatio" in url:
                return FakeResponse(url, raw_pcr(days).encode("utf-8"))
            return FakeResponse(url, raw_vix(days).encode("cp950"))
        provider = FearGreedProvider(opener=opener, token="token", sleep=lambda seconds: None)
        provider.capture = (lambda original: lambda source, url, **kw: dict(original(source, url, **kw), available_at=now.isoformat()))(provider.capture)
        return provider

    def test_first_capture_backfills_then_later_runs_fetch_only_recent_months(self):
        first_time = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
        capture_fear_greed(self.root, provider=self.provider(first_time), now=first_time)
        self.assertEqual(len(self.fetched), 14)
        self.fetched.clear()
        second_time = datetime(2026, 9, 20, 3, tzinfo=timezone.utc)
        capture_fear_greed(self.root, provider=self.provider(second_time), now=second_time)
        # 已收盤且收盤後才抓到的月份沿用；當月（2026-09）與上個月（08，第一次抓時已收盤）以外都不重抓。
        self.assertEqual(sorted(self.fetched), [twse_url("202609")])
        pair = build_fear_greed_input(self.root, self.approvals, "snapshot-decision-1", "2026-09-21T00:00:00+00:00")
        self.assertEqual(pair["bundle"]["schema_version"], PACK_VERSION_V12)
        dates = {c["available_at"] for c in pair["bundle"]["captures"] if c["source"] == "TWSE_TAIEX"}
        self.assertEqual(dates, {first_time.isoformat(), second_time.isoformat()})

    def test_month_fetched_before_it_closed_is_refetched(self):
        early = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        capture_fear_greed(self.root, provider=self.provider(early), now=early)
        self.fetched.clear()
        later = datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
        capture_fear_greed(self.root, provider=self.provider(later), now=later)
        self.assertEqual(sorted(self.fetched), [twse_url("202609"), twse_url("202610")])

    def test_old_captures_without_index_still_build_v10(self):
        from etf_agent.perception.market_fear_greed.service import _store
        rows = captures()
        _store(self.root).save("fear-greed-20260919T120000Z-legacy00000", {"captures": {"items": [
            dict(c, available_at=AVAILABLE) for c in rows]}})
        pair = build_fear_greed_input(self.root, self.approvals, "snapshot-decision-1", CUTOFF)
        self.assertEqual(pair["bundle"]["schema_version"], PACK_VERSION)
        self.assertEqual(pair["result"]["score_version"], "fear-greed-v1")
        self.assertEqual([r["source"] for r in pair["bundle"]["source_approvals"]], list(SOURCES))


class FearGreedNoPutCallTests(unittest.TestCase):
    def v12_pack(self):
        base = v11_pack()
        rows = [c for c in base["captures"] if c["source"] != "TAIFEX_PCR"]
        approvals_ = [r for r in base["source_approvals"] if r["source"] != "TAIFEX_PCR"]
        return seal(dict(base, schema_version=PACK_VERSION_V12, captures=rows, source_approvals=approvals_))

    def test_v12_has_three_components_without_put_call(self):
        result = evaluate(self.v12_pack())
        self.assertEqual((result["schema_version"], result["score_version"]), ("market-fear-greed-result-1.2", "fear-greed-v3"))
        self.assertEqual([r["source"] for r in result["components"]], ["TAIFEX_VIX", "FINMIND_MARGIN", "FINMIND_MARGIN_LEVEL"])
        self.assertEqual(result["status"], "available")
        self.assertFalse(any("買賣權" in gap for gap in result["data_gaps"]))

    def test_v12_rejects_put_call_capture_or_approval(self):
        base = self.v12_pack()
        with_pcr = seal(dict(base, captures=base["captures"] + [capture("TAIFEX_PCR", SOURCES["TAIFEX_PCR"]["url"], raw_pcr(trading_days(date(2026, 9, 18), 30)))]))
        with self.assertRaises(FearGreedError):
            evaluate(with_pcr)
        with self.assertRaises(FearGreedError):
            evaluate(seal(dict(base, source_approvals=v11_approvals())))

    def test_new_captures_skip_put_call_and_build_v12(self):
        provider = FearGreedBackfillTests.provider
        tests = FearGreedBackfillTests("test_first_capture_backfills_then_later_runs_fetch_only_recent_months")
        tests.setUp()
        try:
            now = datetime(2026, 9, 19, 12, tzinfo=timezone.utc)
            capture_fear_greed(tests.root, provider=provider(tests, now), now=now)
            pair = build_fear_greed_input(tests.root, tests.approvals, "snapshot-decision-1", CUTOFF)
            self.assertEqual(pair["bundle"]["schema_version"], PACK_VERSION_V12)
            self.assertNotIn("TAIFEX_PCR", {c["source"] for c in pair["bundle"]["captures"]})
            self.assertNotIn("TAIFEX_PCR", {r["source"] for r in pair["bundle"]["source_approvals"]})
        finally:
            tests.tearDown()
