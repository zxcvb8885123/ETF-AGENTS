import hashlib
import json
import tempfile
import unittest
from datetime import timezone
from decimal import Decimal
from pathlib import Path

from etf_agent.core import (
    canonical_json,
    canonical_sha256,
    content_sha256,
    decimal_string,
    parse_aware_time,
    parse_decimal,
)
from etf_agent.core.artifact_store import ImmutableRunStore


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


class ImmutableRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ImmutableRunStore(self.root, schema_version="1.0", error=SampleError)
        self.artifacts = {"result": {"status": "completed", "note": "台積電"}}

    def tearDown(self):
        self.temp.cleanup()

    def test_save_is_idempotent_and_verifiable(self):
        path = self.store.save("run-1", self.artifacts)
        self.assertEqual(self.store.save("run-1", self.artifacts), path)
        self.assertEqual(self.store.verify("run-1"), path)
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["artifacts"]["result"]["sha256"], canonical_sha256(self.artifacts["result"]))

    def test_rejects_conflicting_content_and_unsafe_names(self):
        self.store.save("run-1", self.artifacts)
        with self.assertRaisesRegex(SampleError, "不同內容"):
            self.store.save("run-1", {"result": {"status": "failed"}})
        with self.assertRaisesRegex(SampleError, "run_id"):
            self.store.save("../escape", self.artifacts)
        with self.assertRaisesRegex(SampleError, "artifact 名稱"):
            self.store.save("run-2", {"../x": {}})

    def test_detects_tampering_extra_files_and_schema_mismatch(self):
        path = self.store.save("run-1", self.artifacts)
        (path / "extra.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(SampleError, "檔案集合"):
            self.store.verify("run-1")
        (path / "extra.json").unlink()
        (path / "result.json").write_text('{"status":"tampered"}', encoding="utf-8")
        with self.assertRaisesRegex(SampleError, "manifest 不一致"):
            self.store.verify("run-1")
        other = ImmutableRunStore(self.root, schema_version="2.0", error=SampleError)
        with self.assertRaisesRegex(SampleError, "身分欄位"):
            other.verify("run-1")

    def test_rejects_symlinked_run_directory(self):
        target = self.store.save("run-1", self.artifacts)
        (self.root / "linked").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(SampleError, "符號連結"):
            self.store.verify("linked")


if __name__ == "__main__":
    unittest.main()
