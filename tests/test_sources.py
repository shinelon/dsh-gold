"""数据解析层测试：列序、脏数据、类型转换、原始快照归档。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import config, sources

FIX = Path(__file__).resolve().parent / "fixtures"


class TestGoldParse(unittest.TestCase):
    """上金所的列序是 [日期, 开, 收, 低, 高]，不是 OHLC，必须显式验证。"""

    def setUp(self):
        self.rows = sources.parse_gold(
            (FIX / "sge_sample.json").read_text(encoding="utf-8"),
            fetched_at="2026-01-01T00:00:00+08:00",
        )

    def test_drops_all_zero_rows(self):
        self.assertNotIn("2027-01-04", [r["date"] for r in self.rows])

    def test_row_count(self):
        # 5 行输入：1 行全 0 被丢弃，剩 4 行
        self.assertEqual(len(self.rows), 4)

    def test_column_order_open_close_low_high(self):
        r = self.rows[0]
        self.assertAlmostEqual(r["open"], 262.45)
        self.assertAlmostEqual(r["close"], 262.76)
        self.assertAlmostEqual(r["low"], 262.02)
        self.assertAlmostEqual(r["high"], 263.5)

    def test_dirty_bracket_is_normalized(self):
        r = next(x for x in self.rows if x["date"] == "2027-01-05")
        # 原始行 [900, 950, 940, 910] 的高低价无法包围开收价，归一后必须自洽
        self.assertAlmostEqual(r["high"], 950.0)
        self.assertAlmostEqual(r["low"], 900.0)

    def test_every_row_brackets(self):
        for r in self.rows:
            self.assertGreaterEqual(r["high"], max(r["open"], r["close"]) - 1e-9)
            self.assertLessEqual(r["low"], min(r["open"], r["close"]) + 1e-9)

    def test_volume_is_none_for_spot(self):
        self.assertTrue(all(r["volume"] is None for r in self.rows))

    def test_source_and_symbol(self):
        self.assertTrue(all(r["source"] == "sge" for r in self.rows))
        self.assertTrue(all(r["symbol"] == config.GOLD for r in self.rows))

    def test_empty_payload_raises(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_gold((FIX / "sge_empty.json").read_text(encoding="utf-8"))

    def test_broken_json_raises(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_gold((FIX / "sge_broken.json").read_text(encoding="utf-8"))

    def test_output_is_date_sorted(self):
        dates = [r["date"] for r in self.rows]
        self.assertEqual(dates, sorted(dates))


class TestIndexParse(unittest.TestCase):
    def setUp(self):
        self.rows = sources.parse_index(
            "sh000001",
            (FIX / "sina_sample.json").read_text(encoding="utf-8"),
            fetched_at="2026-01-01T00:00:00+08:00",
        )

    def test_numeric_strings_become_floats(self):
        r = self.rows[0]
        for field in ("open", "high", "low", "close", "volume"):
            self.assertIsInstance(r[field], float, field)

    def test_values_correct(self):
        r = self.rows[0]
        self.assertAlmostEqual(r["close"], 1319.471)
        self.assertAlmostEqual(r["volume"], 3280466900.0)

    def test_source_sina(self):
        self.assertTrue(all(r["source"] == "sina" for r in self.rows))

    def test_null_response_raises(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_index("sh000001", "null")

    def test_non_list_raises(self):
        with self.assertRaises(sources.SourceError):
            sources.parse_index("sh000001", '{"unexpected": 1}')


class TestHelpers(unittest.TestCase):
    def test_float_coercion(self):
        self.assertEqual(sources._f("1,234.5"), 1234.5)
        self.assertEqual(sources._f(""), None)
        self.assertEqual(sources._f("--"), None)
        self.assertEqual(sources._f(None), None)
        self.assertEqual(sources._f(3), 3.0)

    def test_archive_raw_writes_dated_file(self):
        with tempfile.TemporaryDirectory() as td:
            original = config.RAW_DIR
            config.RAW_DIR = Path(td)
            try:
                path = sources.archive_raw("AU9999", '{"x":1}')
                self.assertTrue(path.is_file())
                self.assertEqual(path.read_text(encoding="utf-8"), '{"x":1}')
                self.assertEqual(path.parent.name, sources.today_key())
                self.assertEqual(path.name, "AU9999.json")
            finally:
                config.RAW_DIR = original


if __name__ == "__main__":
    unittest.main()
