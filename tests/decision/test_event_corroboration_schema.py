"""事件分析師 `corroborated_by` 的輸出 Schema 收窄測試（只改輸出格式，Validator 不變）。

覆蓋：沒給 id 清單時 Schema 逐位元不變；給了就限為本批重大訊息與新聞的 evidence_id（去重排序）；
本批沒有任何一則時回傳 None 以維持原 Schema（避免空列舉）；財報 id 不在列舉內；提示文字明寫不可填財報。
"""

import unittest

from etf_agent.automation.agent_schemas import _EVENT_RULES, event_analyst_schema
from etf_agent.automation.team_stages import _batch_corroboration_ids


def corroboration_property(schema):
    return schema["properties"]["items"]["items"]["properties"]["events"]["items"]["properties"]["corroborated_by"]


class CorroborationSchemaTests(unittest.TestCase):
    def test_default_schema_is_unchanged(self):
        self.assertEqual(event_analyst_schema(False), event_analyst_schema(False, False, None))
        self.assertNotIn("enum", corroboration_property(event_analyst_schema(False))["items"])

    def test_ids_limit_the_enum_sorted_and_deduplicated(self):
        prop = corroboration_property(event_analyst_schema(False, False, ["document:2", "document:1", "document:2"]))
        self.assertEqual(prop["items"]["enum"], ["document:1", "document:2"])

    def test_financial_statement_ids_are_not_in_the_enum(self):
        brief = {"symbols": [
            {"symbol": "3529.TWO", "events": [{"evidence_id": "document:15189"}], "news": [],
             "financial_context": [{"evidence_id": "document:25919"}, {"evidence_id": "document:25946"}]},
        ]}
        ids = _batch_corroboration_ids(brief)
        self.assertEqual(ids, ["document:15189"])
        enum = corroboration_property(event_analyst_schema(False, False, ids))["items"]["enum"]
        self.assertNotIn("document:25919", enum)

    def test_events_and_news_of_every_symbol_in_the_batch_are_collected(self):
        brief = {"symbols": [
            {"symbol": "A", "events": [{"evidence_id": "document:1"}], "news": [{"evidence_id": "news:finmind:9"}]},
            {"symbol": "B", "events": None, "news": [{"evidence_id": "news:finmind:10"}]},
        ]}
        self.assertEqual(_batch_corroboration_ids(brief), ["document:1", "news:finmind:9", "news:finmind:10"])

    def test_empty_batch_returns_none_to_avoid_an_empty_enum(self):
        brief = {"symbols": [{"symbol": "A", "events": [], "news": None}]}
        self.assertIsNone(_batch_corroboration_ids(brief))

    def test_rules_text_forbids_financial_statements(self):
        self.assertIn("不可填財報", _EVENT_RULES)


if __name__ == "__main__":
    unittest.main()
