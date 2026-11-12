"""交易背景封存（position_sizing.trader_background_record）：docs/plans/audit_fix_plan.md 項目 C。

1. 閘門形狀與登記：只允許 {version: v1}、人專屬、策略反思禁區。
2. 建立：兩個來源檔的完整內容封存；由紀錄產生的簡報與現行讀檔路徑逐位元相同；兩者皆無時紀錄為 None 並仍通過驗證。
3. fail-closed：雜湊、時間、有效天數、verdicts_ref、未知欄位。
4. 沒有閘門時：build_team_inputs 的簽章預設為 None（逐鍵不變）。
"""

import copy
import inspect
import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.decision.finalization import build_team_inputs
from etf_agent.decision.sizing import GATE_FIELDS, GATE_PREREQUISITES, POSITION_SIZING_FIELDS
from etf_agent.decision.strategy_lessons import LESSONS_PATH, lessons_brief, load_strategy_lessons, seal_lessons
from etf_agent.decision.thesis_verdicts import VERDICTS_PATH, load_thesis_verdicts, seal_verdicts, verdicts_brief
from etf_agent.decision.trader_background import (
    build_trader_background, lessons_from_record, trader_background_gate, validate_trader_background,
    validate_trader_background_gate, verdicts_from_record,
)
from etf_agent.core import canonical_sha256
from etf_agent.strategy_reflection.contracts import FORBIDDEN_PREFIXES

CUTOFF = "2026-11-11T08:00:00+08:00"
BUNDLE = {"decision_cutoff": CUTOFF}


def write(root: Path, relative: Path, payload) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def reseal(record):
    """竄改後重算 content_sha256，讓錯誤只來自被改的那一處。"""
    record = copy.deepcopy(record)
    record["content_sha256"] = canonical_sha256({key: value for key, value in record.items() if key != "content_sha256"})
    return record


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lessons = seal_lessons("lessons-1", "2026-11-09T08:00:00+08:00", [{"text": "買進後連續下跌時先檢查買進理由是否仍成立", "evidence_ids": ["e1"]}])
        self.verdicts = seal_verdicts("verdicts-1", "2026-11-07T08:00:00+08:00", {"a": {"symbol": "2330.TW", "verdict": "broken", "summary": "營收連續衰退，原本的買進理由不再成立"}})
        write(self.root, LESSONS_PATH, self.lessons)
        write(self.root, VERDICTS_PATH, self.verdicts)
        self.policy = {"position_sizing": {"trader_background_record": {"version": "v1"}}}

    def errors(self, record, policy=None):
        return validate_trader_background(record, BUNDLE, policy if policy is not None else self.policy)

    def test_gate_shape_and_registration(self):
        errors: list = []
        validate_trader_background_gate({"trader_background_record": {"version": "v1"}}, errors)
        self.assertEqual(errors, [])
        for bad in ({"version": "v2"}, {"version": "v1", "x": 1}, "v1"):
            errors = []
            validate_trader_background_gate({"trader_background_record": bad}, errors)
            self.assertTrue(errors, bad)
        self.assertIn("trader_background_record", GATE_FIELDS)
        self.assertIn("trader_background_record", POSITION_SIZING_FIELDS)
        rule = next(rule for rule in GATE_PREREQUISITES if rule.field == "trader_background_record")
        self.assertTrue(rule.human_only)
        self.assertIn("trader_background_record", FORBIDDEN_PREFIXES)
        self.assertIsNone(trader_background_gate({}))
        self.assertIsNone(trader_background_gate(None))
        self.assertEqual(trader_background_gate(self.policy["position_sizing"]), {"version": "v1"})

    def test_record_validates_and_briefs_equal_the_live_read_path(self):
        record = build_trader_background(self.root, BUNDLE)
        self.assertEqual(self.errors(record), [])
        self.assertEqual(record["strategy_lessons"], self.lessons)
        self.assertEqual(record["thesis_verdicts"], self.verdicts)
        self.assertEqual(lessons_from_record(record), load_strategy_lessons(self.root, CUTOFF))
        self.assertEqual(verdicts_from_record(record), load_thesis_verdicts(self.root, CUTOFF))
        self.assertEqual(lessons_brief(lessons_from_record(record)), lessons_brief(load_strategy_lessons(self.root, CUTOFF)))
        held = {"2330.TW"}
        self.assertEqual(verdicts_brief(verdicts_from_record(record), held), verdicts_brief(load_thesis_verdicts(self.root, CUTOFF), held))
        self.assertEqual(build_trader_background(self.root, BUNDLE), record)  # 同輸入重建逐位元相同

    def test_no_sources_gives_none_fields_that_still_validate_and_no_brief_blocks(self):
        (self.root / LESSONS_PATH).unlink()
        (self.root / VERDICTS_PATH).unlink()
        record = build_trader_background(self.root, BUNDLE)
        self.assertEqual((record["strategy_lessons"], record["thesis_verdicts"]), (None, None))
        self.assertEqual(self.errors(record), [])
        self.assertIsNone(lessons_brief(lessons_from_record(record)))
        self.assertIsNone(verdicts_brief(verdicts_from_record(record), {"2330.TW"}))

    def test_unusable_sources_are_recorded_as_none_exactly_like_the_live_read_path(self):
        late = seal_lessons("lessons-late", "2026-11-12T08:00:00+08:00", [{"text": "買進後連續下跌時先檢查買進理由是否仍成立", "evidence_ids": ["e1"]}])
        old = seal_verdicts("verdicts-old", "2026-10-01T08:00:00+08:00", {"a": {"symbol": "2330.TW", "verdict": "broken", "summary": "營收連續衰退，原本的買進理由不再成立"}})
        write(self.root, LESSONS_PATH, late)
        write(self.root, VERDICTS_PATH, old)
        record = build_trader_background(self.root, BUNDLE)
        self.assertEqual((record["strategy_lessons"], record["thesis_verdicts"]), (None, None))
        self.assertIsNone(load_strategy_lessons(self.root, CUTOFF))
        self.assertIsNone(load_thesis_verdicts(self.root, CUTOFF))
        self.assertEqual(self.errors(record), [])

    def test_a_tampered_lesson_text_is_refused_even_when_the_record_hash_is_recomputed(self):
        record = build_trader_background(self.root, BUNDLE)
        edited = copy.deepcopy(record)
        edited["strategy_lessons"]["lessons"][0]["text"] = "改寫後的另一條教訓文字內容"
        self.assertTrue(any("content_sha256" in error for error in self.errors(edited)))  # 外層雜湊不符
        self.assertTrue(any("strategy_lessons" in error and "content_sha256" in error for error in self.errors(reseal(edited))))  # 內層雜湊不符

    def test_a_record_hash_that_does_not_match_is_refused(self):
        record = build_trader_background(self.root, BUNDLE)
        record["thesis_verdicts"] = None
        self.assertTrue(any("content_sha256" in error for error in self.errors(record)))

    def test_future_dated_lessons_and_verdicts_are_refused(self):
        record = build_trader_background(self.root, BUNDLE)
        late_lessons = seal_lessons("l2", "2026-11-12T08:00:00+08:00", [{"text": "買進後連續下跌時先檢查買進理由是否仍成立", "evidence_ids": ["e1"]}])
        self.assertTrue(any("晚於 decision_cutoff" in error for error in self.errors(reseal(dict(record, strategy_lessons=late_lessons)))))
        late_verdicts = seal_verdicts("v2", "2026-11-12T08:00:00+08:00", {"a": {"symbol": "2330.TW", "verdict": "broken", "summary": "營收連續衰退，原本的買進理由不再成立"}})
        self.assertTrue(any("晚於 decision_cutoff" in error for error in self.errors(reseal(dict(record, thesis_verdicts=late_verdicts)))))

    def test_expired_verdicts_are_refused(self):
        record = build_trader_background(self.root, BUNDLE)
        old = seal_verdicts("v3", "2026-10-01T08:00:00+08:00", {"a": {"symbol": "2330.TW", "verdict": "broken", "summary": "營收連續衰退，原本的買進理由不再成立"}})
        self.assertTrue(any("有效天數" in error for error in self.errors(reseal(dict(record, thesis_verdicts=old)))))

    def test_empty_lists_must_be_recorded_as_none(self):
        record = build_trader_background(self.root, BUNDLE)
        empty_lessons = seal_lessons("l3", "2026-11-09T08:00:00+08:00", [])
        self.assertTrue(any("必須記 null" in error for error in self.errors(reseal(dict(record, strategy_lessons=empty_lessons)))))
        empty_verdicts = seal_verdicts("v4", "2026-11-07T08:00:00+08:00", {})
        self.assertTrue(any("必須記 null" in error for error in self.errors(reseal(dict(record, thesis_verdicts=empty_verdicts)))))

    def test_verdicts_ref_must_match_the_recorded_verdicts(self):
        record = build_trader_background(self.root, BUNDLE)
        policy = {"position_sizing": {"trader_background_record": {"version": "v1"},
                                      "agent_swap": {"verdicts_ref": {"sha256": self.verdicts["content_sha256"]}}}}
        self.assertEqual(self.errors(record, policy), [])
        none_record = reseal(dict(record, thesis_verdicts=None))
        self.assertTrue(any("verdicts_ref" in error for error in self.errors(none_record, policy)))
        other = copy.deepcopy(policy)
        other["position_sizing"]["agent_swap"]["verdicts_ref"]["sha256"] = "0" * 64
        self.assertTrue(any("verdicts_ref" in error for error in self.errors(record, other)))

    def test_unknown_or_missing_fields_and_wrong_types_are_refused(self):
        record = build_trader_background(self.root, BUNDLE)
        self.assertTrue(self.errors(dict(record, extra=1)))
        missing = {key: value for key, value in record.items() if key != "thesis_verdicts"}
        self.assertTrue(self.errors(missing))
        self.assertTrue(self.errors(reseal(dict(record, version="v2"))))
        self.assertTrue(self.errors("record"))
        self.assertTrue(validate_trader_background(record, {}, self.policy))  # 沒有 decision_cutoff

    def test_team_inputs_take_the_optional_key_only_when_given(self):
        self.assertIsNone(inspect.signature(build_team_inputs).parameters["trader_background"].default)  # 沒傳入時 team_inputs 逐鍵不變


if __name__ == "__main__":
    unittest.main()
