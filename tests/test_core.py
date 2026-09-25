import hashlib
import json
import unittest
from datetime import timezone
from decimal import Decimal

from etf_agent.core import (
    canonical_json,
    canonical_sha256,
    content_sha256,
    decimal_string,
    parse_aware_time,
    parse_decimal,
)


class SampleError(ValueError):
    pass


class CanonicalHashTests(unittest.TestCase):
    def test_hash_is_stable_across_key_order_and_unicode(self):
        payload = {"b": "台積電", "a": [1, {"d": 2, "c": 3}]}
        reordered = {"a": [1, {"c": 3, "d": 2}], "b": "台積電"}
        self.assertEqual(canonical_sha256(payload), canonical_sha256(reordered))
        self.assertEqual(canonical_json(payload), '{"a":[1,{"c":3,"d":2}],"b":"台積電"}')

    def test_hash_matches_frozen_encoding(self):
        # Archived artifacts were sealed with this exact encoding; changing it breaks replay.
        payload = {"symbol": "2330.TW", "weight": "0.1", "note": "買進"}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(canonical_sha256(payload), hashlib.sha256(encoded.encode("utf-8")).hexdigest())

    def test_content_hash_ignores_its_own_field(self):
        body = {"id": "x", "value": 1}
        sealed = dict(body, content_sha256="stale")
        self.assertEqual(content_sha256(sealed), canonical_sha256(body))
        self.assertEqual(content_sha256(dict(body, bundle_sha256="y"), "bundle_sha256"), canonical_sha256(body))


class AwareTimeTests(unittest.TestCase):
    def test_normalizes_to_utc(self):
        parsed = parse_aware_time("2026-09-25T08:00:00+08:00", "cutoff")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        self.assertEqual(parsed.hour, 0)
        self.assertEqual(parse_aware_time("2026-09-25T00:00:00Z", "cutoff"), parsed)

    def test_keeps_offset_when_requested(self):
        parsed = parse_aware_time("2026-09-25T08:00:00+08:00", "cutoff", to_utc=False)
        self.assertEqual(parsed.hour, 8)

    def test_rejects_naive_empty_and_garbage_with_caller_error(self):
        for value, message in (
            ("2026-09-25T08:00:00", "必須包含時區"),
            ("", "必須是包含時區的時間字串"),
            (None, "必須是包含時區的時間字串"),
            ("not-a-time", "無法解析"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SampleError, "cutoff " + message):
                    parse_aware_time(value, "cutoff", error=SampleError)


class DecimalTests(unittest.TestCase):
    def test_parses_finite_values(self):
        self.assertEqual(parse_decimal("1.50", "price"), Decimal("1.50"))
        self.assertEqual(parse_decimal(2, "price"), Decimal(2))

    def test_rejects_non_finite_and_garbage(self):
        for value in ("NaN", "Infinity", "-inf"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SampleError, "price 必須是有限數值"):
                    parse_decimal(value, "price", error=SampleError)
        with self.assertRaisesRegex(SampleError, "price 無法解析數值：abc"):
            parse_decimal("abc", "price", error=SampleError)

    def test_bool_and_none_use_parse_message(self):
        for value in (True, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(SampleError, "qty 必須是有限數值"):
                    parse_decimal(value, "qty", error=SampleError, reject_bool=True, parse_message="%(field)s 必須是有限數值")

    def test_decimal_string_is_plain_and_normalized(self):
        self.assertEqual(decimal_string(Decimal("1.2300")), "1.23")
        self.assertEqual(decimal_string(Decimal("1E+3")), "1000")
        self.assertEqual(decimal_string(Decimal("-0.00")), "0")


if __name__ == "__main__":
    unittest.main()
