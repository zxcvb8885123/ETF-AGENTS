import json
import tempfile
import unittest
from pathlib import Path

from etf_agent.data import (
    Instrument,
    SectorClassificationBuilder,
    SectorClassificationError,
    capture_sector_sources,
)


UNIVERSE = [
    Instrument("2330.TW", "2330", "台積電", "TWSE", "2026-09-14"),
    Instrument("6488.TWO", "6488", "環球晶", "TPEX", "2026-09-14"),
]


def twse_rows(**overrides):
    row = {"出表日期": "1150924", "公司代號": "2330", "公司簡稱": "台積電", "產業別": "24"}
    row.update(overrides)
    return [row, {"出表日期": "1150924", "公司代號": "1101", "公司簡稱": "台泥", "產業別": "01"}]


def tpex_rows():
    return [{"Date": "1150925", "SecuritiesCompanyCode": "6488", "SecuritiesIndustryCode": "24"}]


def captures(twse=None, tpex=None):
    return {
        "TWSE_COMPANY_PROFILE": {
            "raw": json.dumps(twse if twse is not None else twse_rows(), ensure_ascii=False).encode("utf-8"),
            "fetched_at": "2026-09-25T01:00:00+00:00",
        },
        "TPEX_COMPANY_PROFILE": {
            "raw": json.dumps(tpex if tpex is not None else tpex_rows()).encode("utf-8"),
            "fetched_at": "2026-09-25T01:00:05+00:00",
        },
    }


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status = status

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class SectorClassificationTests(unittest.TestCase):
    def test_classifies_both_markets_with_shared_industry_codes(self):
        result = SectorClassificationBuilder(UNIVERSE, captures()).build()

        self.assertEqual(result["status"], "completed")
        self.assertEqual(
            result["sector_by_symbol"], {"2330.TW": "industry:24", "6488.TWO": "industry:24"}
        )
        self.assertEqual([item["content_date"] for item in result["sources"]], ["2026-09-24", "2026-09-25"])
        self.assertEqual(result["available_at"], "2026-09-25T01:00:05+00:00")
        self.assertTrue(result["version"].startswith("sector:"))

    def test_missing_symbol_is_listed_not_filled(self):
        result = SectorClassificationBuilder(UNIVERSE, captures(tpex=[{"Date": "1150925", "SecuritiesCompanyCode": "9999", "SecuritiesIndustryCode": "24"}])).build()

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["missing_symbols"], ["6488.TWO"])
        self.assertNotIn("6488.TWO", result["sector_by_symbol"])

    def test_conflicting_codes_and_mixed_dates_fail_closed(self):
        conflicting = twse_rows() + [{"出表日期": "1150924", "公司代號": "2330", "產業別": "31"}]
        with self.assertRaisesRegex(SectorClassificationError, "衝突"):
            SectorClassificationBuilder(UNIVERSE, captures(twse=conflicting)).build()
        mixed = twse_rows() + [{"出表日期": "1150923", "公司代號": "2303", "產業別": "24"}]
        with self.assertRaisesRegex(SectorClassificationError, "出表日期"):
            SectorClassificationBuilder(UNIVERSE, captures(twse=mixed)).build()

    def test_capture_saves_raw_before_rejecting_http_error(self):
        responses = iter([FakeResponse(b"[]"), FakeResponse(b"gateway", status=502)])
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            with self.assertRaisesRegex(SectorClassificationError, "502"):
                capture_sector_sources(run_dir, fetch=lambda request, timeout: next(responses))
            self.assertEqual((run_dir / "TPEX_COMPANY_PROFILE.json").read_bytes(), b"gateway")


if __name__ == "__main__":
    unittest.main()
