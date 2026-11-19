"""事件分析結果重用（automation/event_reuse.py）：同一交易日前一次 run、簡報列逐檔完全相同才沿用。

覆蓋：前一次 run 的選法（只看名稱較早、有報告與簡報的同日 run）、相同列才沿用、任何一欄不同都不沿用、
報告未完成／毀損／缺項目一律不重用、沿用的項目是複本（不會改到來源檔）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.event_reuse import previous_run_dir, reusable_event_items, row_digest


def row(symbol, events=1, news=0, context="c"):
    return {"symbol": symbol, "events": [{"evidence_id": "document:%d" % i} for i in range(events)],
            "news": [{"evidence_id": "news:%d" % i} for i in range(news)], "financial_context": context,
            "data_availability": [{"status": "ok"}]}


def write_run(day_dir, name, rows, items=None, status="completed", with_brief=True):
    run = day_dir / name
    run.mkdir(parents=True)
    items = items if items is not None else [{"symbol": r["symbol"], "outlook": "unknown", "findings": [], "data_gaps": [], "events": []} for r in rows]
    (run / "analyst_event.json").write_text(json.dumps({"status": status, "items": items}), encoding="utf-8")
    if with_brief:
        (run / "brief_event_b0.json").write_text(json.dumps({"symbols": rows}), encoding="utf-8")
    return run


class EventReuseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.day = Path(self.tmp.name) / "2026-11-11"
        self.day.mkdir()

    def current(self, name="daily-20261111-070000"):
        return self.day / name

    def test_only_an_earlier_sibling_with_report_and_briefs_is_a_source(self):
        write_run(self.day, "daily-20261110-210000", [row("2330.TW")])
        write_run(self.day, "daily-20261110-233000", [row("2330.TW")], with_brief=False)
        write_run(self.day, "daily-20261111-090000", [row("2330.TW")])  # 名稱較晚：不是前一次
        self.assertEqual(previous_run_dir(self.current()).name, "daily-20261110-210000")

    def test_no_previous_run_means_no_reuse(self):
        self.assertIsNone(previous_run_dir(self.current()))
        self.assertEqual(reusable_event_items(self.current(), {"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))

    def test_identical_rows_are_reused_and_any_difference_is_not(self):
        old = [row("2330.TW"), row("2317.TW"), row("2412.TW", news=1)]
        write_run(self.day, "daily-20261110-210000", old)
        new = {"2330.TW": row("2330.TW"), "2317.TW": row("2317.TW", events=2), "2412.TW": row("2412.TW", news=1, context="changed")}
        source, items = reusable_event_items(self.current(), new, ["2330.TW", "2317.TW", "2412.TW"])
        self.assertEqual(source, "daily-20261110-210000")
        self.assertEqual([item["symbol"] for item in items], ["2330.TW"])

    def test_data_availability_difference_does_not_block_matching(self):
        """data_availability 只是儲存筆數與取得時間，不同仍沿用；事件或新聞欄位不同則不沿用。"""
        stored = {"symbol": "2330.TW", "events": [], "news": [], "data_availability": [{"name": "material_events", "stored_rows": 1}]}
        write_run(self.day, "daily-20261110-210000", [stored])
        more_rows = dict(stored, data_availability=[{"name": "material_events", "stored_rows": 2, "status": "已取得"}])
        _, items = reusable_event_items(self.current(), {"2330.TW": more_rows}, ["2330.TW"])
        self.assertEqual(len(items), 1)
        new_event = dict(stored, events=[{"title": "新重大訊息"}])
        self.assertEqual(reusable_event_items(self.current(), {"2330.TW": new_event}, ["2330.TW"])[1], [])
        new_news = dict(stored, news=[{"title": "新新聞"}])
        self.assertEqual(reusable_event_items(self.current(), {"2330.TW": new_news}, ["2330.TW"])[1], [])

    def test_only_candidates_are_considered(self):
        write_run(self.day, "daily-20261110-210000", [row("2330.TW"), row("2317.TW")])
        new = {"2330.TW": row("2330.TW"), "2317.TW": row("2317.TW")}
        _, items = reusable_event_items(self.current(), new, ["2330.TW"])
        self.assertEqual([item["symbol"] for item in items], ["2330.TW"])

    def test_an_unfinished_or_broken_report_is_not_reused(self):
        write_run(self.day, "daily-20261110-210000", [row("2330.TW")], status="failed")
        self.assertEqual(reusable_event_items(self.current(), {"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))
        (self.day / "daily-20261110-210000" / "analyst_event.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(reusable_event_items(self.current(), {"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))

    def test_a_symbol_without_a_previous_item_is_not_reused(self):
        write_run(self.day, "daily-20261110-210000", [row("2330.TW")], items=[])
        self.assertEqual(reusable_event_items(self.current(), {"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))

    def test_reused_items_are_copies(self):
        write_run(self.day, "daily-20261110-210000", [row("2330.TW")])
        _, items = reusable_event_items(self.current(), {"2330.TW": row("2330.TW")}, ["2330.TW"])
        items[0]["findings"].append("x")
        stored = json.loads((self.day / "daily-20261110-210000" / "analyst_event.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["items"][0]["findings"], [])

    def test_row_digest_ignores_key_order(self):
        self.assertEqual(row_digest({"a": 1, "b": 2}), row_digest({"b": 2, "a": 1}))
        self.assertNotEqual(row_digest({"a": 1}), row_digest({"a": 2}))


def mark_reused(run, symbols):
    (run / "event_reuse.json").write_text(json.dumps({"source_run": "x", "symbols": sorted(symbols)}), encoding="utf-8")


class CrossDayEventReuseTests(unittest.TestCase):
    """逐檔跨交易日沿用：沿鏈找該檔最後一次真正交給 Agent 的 run，輸入逐檔相同、且在回看日數內才沿用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def day(self, name):
        path = self.root / name
        path.mkdir(exist_ok=True)
        return path

    def current(self, day="2026-11-18", name="daily-20261117-210000"):
        return self.day(day) / name

    def reuse(self, rows, candidates, max_days=4, day="2026-11-18"):
        return reusable_event_items(self.current(day), rows, candidates, cross_day_max_days=max_days)

    def test_an_identical_row_from_an_earlier_trading_day_is_reused(self):
        write_run(self.day("2026-11-17"), "daily-20261116-210000", [row("2330.TW")])
        source, items = self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"])
        self.assertEqual(source, "2026-11-17/daily-20261116-210000")
        self.assertEqual([item["symbol"] for item in items], ["2330.TW"])

    def test_default_mode_still_only_looks_at_the_same_trading_day(self):
        write_run(self.day("2026-11-17"), "daily-20261116-210000", [row("2330.TW")])
        self.assertEqual(
            reusable_event_items(self.current(), {"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))

    def test_a_changed_row_is_not_reused_across_days(self):
        write_run(self.day("2026-11-17"), "daily-20261116-210000", [row("2330.TW"), row("2317.TW")])
        rows = {"2330.TW": row("2330.TW", events=2), "2317.TW": row("2317.TW", news=1)}
        self.assertEqual(self.reuse(rows, ["2330.TW", "2317.TW"]), (None, []))

    def test_runs_older_than_the_lookback_are_not_used(self):
        write_run(self.day("2026-11-12"), "daily-20261111-210000", [row("2330.TW")])
        self.assertEqual(self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"], max_days=4), (None, []))
        self.assertEqual(len(self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"], max_days=6)[1]), 1)

    def test_a_run_that_only_reused_the_symbol_points_back_to_the_original_judgement(self):
        write_run(self.day("2026-11-16"), "daily-20261115-210000", [row("2330.TW")])
        reused_run = write_run(self.day("2026-11-17"), "daily-20261116-210000", [], items=[
            {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": [], "events": []}], with_brief=False)
        mark_reused(reused_run, ["2330.TW"])
        source, items = self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"])
        self.assertEqual(source, "2026-11-16/daily-20261115-210000")
        self.assertEqual(len(items), 1)

    def test_the_age_is_counted_from_the_original_judgement_not_from_the_latest_reuse(self):
        write_run(self.day("2026-11-12"), "daily-20261111-210000", [row("2330.TW")])
        for day, name in (("2026-11-13", "daily-20261112-210000"), ("2026-11-16", "daily-20261113-210000"),
                          ("2026-11-17", "daily-20261116-210000")):
            run = write_run(self.day(day), name, [], items=[
                {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": [], "events": []}], with_brief=False)
            mark_reused(run, ["2330.TW"])
        self.assertEqual(self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"], max_days=5), (None, []))
        self.assertEqual(len(self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"], max_days=6)[1]), 1)

    def test_a_program_produced_run_in_between_stops_the_search(self):
        """中間某次 run 沒有該檔簡報列、也不是沿用（例如當時沒有事件由程式輸出 unknown），輸入已變過，不可越過它沿用更早的判斷。"""
        write_run(self.day("2026-11-16"), "daily-20261115-210000", [row("2330.TW")])
        write_run(self.day("2026-11-17"), "daily-20261116-210000", [], items=[
            {"symbol": "2330.TW", "outlook": "unknown", "findings": [], "data_gaps": [], "events": []}], with_brief=False)
        self.assertEqual(self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))

    def test_a_failed_or_missing_report_is_skipped_in_the_chain(self):
        write_run(self.day("2026-11-16"), "daily-20261115-210000", [row("2330.TW")])
        write_run(self.day("2026-11-17"), "daily-20261116-210000", [row("2330.TW", events=3)], status="failed")
        write_run(self.day("2026-11-17"), "daily-20261116-233000", [row("2330.TW")], with_brief=True)
        (self.day("2026-11-17") / "daily-20261116-233000" / "analyst_event.json").unlink()
        source, items = self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"])
        self.assertEqual(source, "2026-11-16/daily-20261115-210000")
        self.assertEqual(len(items), 1)

    def test_a_later_run_of_the_target_day_is_never_a_source_and_other_folders_are_ignored(self):
        write_run(self.day("2026-11-18"), "daily-20261118-070000", [row("2330.TW")])  # 名稱晚於本次，不是來源
        write_run(self.day("scratch"), "daily-20261116-210000", [row("2330.TW")])  # 非日期資料夾
        write_run(self.day("2026-11-19"), "daily-20261118-210000", [row("2330.TW")])  # 日期在本次之後
        self.assertEqual(self.reuse({"2330.TW": row("2330.TW")}, ["2330.TW"]), (None, []))

    def test_same_day_earlier_runs_are_part_of_the_chain_and_items_are_copies(self):
        write_run(self.day("2026-11-18"), "daily-20261117-210000", [row("2330.TW")])
        current = self.day("2026-11-18") / "daily-20261118-070000"
        _, items = reusable_event_items(current, {"2330.TW": row("2330.TW")}, ["2330.TW"], cross_day_max_days=4)
        items[0]["findings"].append("x")
        stored = json.loads((self.day("2026-11-18") / "daily-20261117-210000" / "analyst_event.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["items"][0]["findings"], [])

    def test_symbols_are_resolved_independently(self):
        write_run(self.day("2026-11-16"), "daily-20261115-210000", [row("2330.TW"), row("2317.TW")])
        write_run(self.day("2026-11-17"), "daily-20261116-210000", [row("2317.TW", events=2)])
        rows = {"2330.TW": row("2330.TW"), "2317.TW": row("2317.TW", events=2)}
        _, items = self.reuse(rows, ["2330.TW", "2317.TW"])
        self.assertEqual(sorted(item["symbol"] for item in items), ["2317.TW"])


if __name__ == "__main__":
    unittest.main()
