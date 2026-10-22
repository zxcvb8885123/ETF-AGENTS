import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.automation.agent_runner import DailyPipelineError
from etf_agent.automation.market_news_stage import build_daily_market_news
from etf_agent.core.artifact_store import ImmutableRunStore
from test_market_news import pack


class FakePipeline:
    """只模擬 save_artifact／agent_step；agent_step 以 answer 函式回應並跑 validate。"""

    def __init__(self, root, answer):
        self.root, self.answer, self.saved, self.logs, self.tasks = root, answer, {}, [], []

    def log(self, message):
        self.logs.append(message)

    def save_artifact(self, name, payload):
        self.saved[name] = payload
        path = self.root / (name + ".json")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def agent_step(self, task, build, validate):
        self.tasks.append(task)
        brief = json.loads(Path(task.prompt.split("再讀 ")[1].split("。")[0]).read_text(encoding="utf-8"))
        output = build(self.answer(brief["items"]))
        errors = validate(output)
        if errors:
            raise DailyPipelineError("；".join(errors))
        return output


def label_all(items, relevance="market"):
    return {"labels": [{"item_id": i["item_id"], "canonical_content_id": i["canonical_content_id"], "relevance": relevance,
                        "stance": "positive", "reason": "測試判讀"} for i in items],
            "synthesis": {"outlook": "positive", "findings": [{"text": "測試市場看法", "evidence_ids": [items[0]["item_id"]]}]}}


class MarketNewsStageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.captures_root = self.root / "market_news"
        self.database = self.root / "news.db"
        self.captures = pack()["captures"]
        self.snapshot = {"snapshot_id": "snap", "decision_cutoff": "2026-09-29T10:00:00+00:00"}

    def save_captures(self, captures):
        store = ImmutableRunStore(self.captures_root, schema_version="market-news-capture-1.0")
        store.save("market-news-test", {"captures": {"items": captures}, "prepared": {"items": []}})

    def test_no_capture_before_cutoff_returns_none(self):
        pipeline = FakePipeline(self.root, label_all)
        self.assertIsNone(build_daily_market_news(pipeline, self.snapshot, self.captures_root, self.database))
        late = [dict(c, available_at="2026-09-29T11:00:00+00:00") for c in self.captures]
        self.save_captures(late)
        self.assertIsNone(build_daily_market_news(pipeline, self.snapshot, self.captures_root, self.database))

    def test_full_labels_build_pair_but_unapproved_sources_stay_unavailable(self):
        self.save_captures(self.captures)
        pipeline = FakePipeline(self.root, label_all)
        pair = build_daily_market_news(pipeline, self.snapshot, self.captures_root, self.database)
        self.assertEqual(pair["result"]["item_count"], 6)
        self.assertEqual(pair["result"]["status"], "unavailable")  # 資料庫沒有核准紀錄。
        self.assertEqual(pair["bundle"]["source_approvals"], [])
        self.assertIn("market_news_result", pipeline.saved)

    def test_approved_sources_make_result_available(self):
        from etf_agent.core import canonical_sha256
        from etf_agent.perception.market_news.repository import MarketNewsRepository
        reviews = [dict(row, reviewed_at="2026-09-29T08:00:00+00:00") for row in pack()["source_approvals"]]
        terms = [{"terms_url": r["terms_url"], "available_at": "2026-09-29T07:00:00+00:00", "raw_text": "人工測試規範",
                  "raw_sha256": canonical_sha256("人工測試規範")} for r in reviews]
        MarketNewsRepository(self.database).save_source_reviews(reviews, terms, "2026-09-29T08:00:00+00:00")
        self.save_captures(self.captures)
        pair = build_daily_market_news(FakePipeline(self.root, label_all), self.snapshot, self.captures_root, self.database)
        self.assertEqual((pair["result"]["status"], pair["result"]["outlook"]), ("available", "positive"))

    def test_incomplete_labels_are_rejected(self):
        self.save_captures(self.captures)
        pipeline = FakePipeline(self.root, lambda items: label_all(items[:-1]))
        with self.assertRaisesRegex(DailyPipelineError, "覆蓋"):
            build_daily_market_news(pipeline, self.snapshot, self.captures_root, self.database)
