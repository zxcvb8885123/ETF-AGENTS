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


if __name__ == "__main__":
    unittest.main()
