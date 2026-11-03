"""論點檢視結論給交易 Agent 的背景：封存、讀取與 fail-closed 測試。"""

import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision.thesis_verdicts import (
    VERDICTS_PATH, load_thesis_verdicts, seal_verdicts, validate_verdicts_file, verdicts_brief,
)

CREATED = "2026-11-06T11:30:00+00:00"
CUTOFF = "2026-11-09T12:00:00+00:00"
REVIEW = {
    "2330.TW": {"symbol": "2330.TW", "verdict": "weakened", "summary": "收盤跌破二十日均線，技術面轉弱。"},
    "2317.TW": {"symbol": "2317.TW", "verdict": "intact", "summary": "理由未動。"},
    "2454.TW": {"symbol": "2454.TW", "verdict": "unknown", "summary": "沒有提供 Agent，未檢視"},
    "2412.TW": {"symbol": "2412.TW", "verdict": "broken", "summary": "見 tech-2412-1 的發現。"},
}


class ThesisVerdictsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, data):
        path = self.root / VERDICTS_PATH
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_seal_keeps_only_usable_items_without_identifiers(self):
        sealed = seal_verdicts("v-1", CREATED, REVIEW)
        self.assertEqual([item["symbol"] for item in sealed["items"]], ["2317.TW", "2330.TW"])  # unknown 與含識別碼者丟棄
        self.assertEqual(validate_verdicts_file(sealed), [])

    def test_load_and_brief_only_for_held_symbols_in_batch(self):
        self.write(seal_verdicts("v-1", CREATED, REVIEW))
        loaded = load_thesis_verdicts(self.root, CUTOFF)
        brief = verdicts_brief(loaded, {"2330.TW", "9999.TW"})
        self.assertEqual([item["symbol"] for item in brief["items"]], ["2330.TW"])
        self.assertEqual(brief["as_of"], CREATED)
        self.assertIsNone(verdicts_brief(loaded, {"9999.TW"}))
        self.assertIsNone(verdicts_brief(None, {"2330.TW"}))

    def test_missing_future_stale_or_tampered_file_is_ignored(self):
        self.assertIsNone(load_thesis_verdicts(self.root, CUTOFF))  # 沒有檔
        sealed = seal_verdicts("v-1", CREATED, REVIEW)
        self.write(sealed)
        self.assertIsNone(load_thesis_verdicts(self.root, "2026-11-05T00:00:00+00:00"))  # 晚於決策截止時間
        self.assertIsNone(load_thesis_verdicts(self.root, "2026-11-20T00:00:00+00:00"))  # 超過 8 天
        sealed["items"][0]["summary"] = "被改寫"
        (self.root / VERDICTS_PATH).write_text(json.dumps(sealed, ensure_ascii=False), encoding="utf-8")
        self.assertIsNone(load_thesis_verdicts(self.root, CUTOFF))  # 雜湊不符


class BindForcedSwapTests(unittest.TestCase):
    def policy(self, forced=True):
        rotation = {"version": "v1"}
        if forced:
            rotation["forced_swap"] = {"version": "v1", "min_new_conviction": "high", "max_swaps_per_run": 2}
        return {"position_sizing": {"rotation": rotation}}

    def verdicts(self, *pairs):
        return {"verdicts_id": "v-1", "as_of": "2026-10-30T11:30:00Z", "sha256": "h",
                "items": [{"symbol": s, "verdict": v, "summary": "x"} for s, v in pairs]}

    def test_only_broken_symbols_are_bound_sorted(self):
        from etf_agent.decision.thesis_verdicts import bind_forced_swap
        bound = bind_forced_swap(self.policy(), self.verdicts(("2330.TW", "intact"), ("2454.TW", "broken"), ("2317.TW", "broken")))
        forced = bound["position_sizing"]["rotation"]["forced_swap"]
        self.assertEqual(forced["broken_symbols"], ["2317.TW", "2454.TW"])
        self.assertEqual(forced["verdicts_ref"], {"verdicts_id": "v-1", "as_of": "2026-10-30T11:30:00Z", "sha256": "h"})

    def test_nothing_bound_without_verdicts_broken_or_template_gate(self):
        from etf_agent.decision.thesis_verdicts import bind_forced_swap
        for policy, verdicts in ((self.policy(), None), (self.policy(), self.verdicts(("2330.TW", "weakened"))),
                                 (self.policy(False), self.verdicts(("2330.TW", "broken")))):
            bound = bind_forced_swap(policy, verdicts)
            self.assertEqual(bound, policy)
            self.assertIsNot(bound, policy)
