"""JSONL 存储层测试：合并语义、原子写入、坏行容错、幂等。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app import config, store


def rec(symbol: str, date: str, close: float = 100.0) -> dict:
    return {
        "symbol": symbol,
        "date": date,
        "open": close - 1,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": 1000.0,
        "source": "test",
        "fetched_at": "2026-01-01T00:00:00+08:00",
    }


class TestMerge(unittest.TestCase):
    def test_dedupes_by_date(self):
        merged = store.merge([rec("X", "2020-01-01", 1)], [rec("X", "2020-01-01", 2)])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["close"], 2.0)

    def test_new_wins(self):
        merged = store.merge([rec("X", "2020-01-01", 1)], [rec("X", "2020-01-01", 9)])
        self.assertEqual(merged[0]["close"], 9.0)

    def test_sorted_ascending(self):
        merged = store.merge(
            [rec("X", "2020-01-03"), rec("X", "2020-01-01")],
            [rec("X", "2020-01-02")],
        )
        self.assertEqual([r["date"] for r in merged],
                         ["2020-01-01", "2020-01-02", "2020-01-03"])

    def test_union_keeps_both(self):
        merged = store.merge([rec("X", "2020-01-01")], [rec("X", "2020-01-02")])
        self.assertEqual(len(merged), 2)


class TestParseLine(unittest.TestCase):
    def test_valid(self):
        line = json.dumps(rec("X", "2020-01-01"))
        self.assertEqual(store.parse_line(line)["date"], "2020-01-01")

    def test_blank(self):
        self.assertIsNone(store.parse_line("   "))

    def test_broken_json(self):
        self.assertIsNone(store.parse_line("{not json"))

    def test_missing_keys(self):
        self.assertIsNone(store.parse_line('{"symbol":"X"}'))

    def test_non_dict(self):
        self.assertIsNone(store.parse_line("[1,2,3]"))


class TestFileStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._bars = config.BARS_DIR
        self._meta = config.META_PATH
        config.BARS_DIR = self.root / "bars"
        config.META_PATH = self.root / "meta.json"
        config.BARS_DIR.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        config.BARS_DIR = self._bars
        config.META_PATH = self._meta
        self.tmp.cleanup()

    def test_roundtrip(self):
        rows = [rec("X", "2020-01-01"), rec("X", "2020-01-02")]
        store.write_atomic(config.bar_path("X"), rows)
        loaded = store.load("X")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["close"], 100.0)
        self.assertEqual(loaded[1]["date"], "2020-01-02")

    def test_no_tmp_residue(self):
        store.write_atomic(config.bar_path("X"), [rec("X", "2020-01-01")])
        leftovers = list((self.root / "bars").glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_missing_file_is_empty(self):
        self.assertEqual(store.load("NOPE"), [])

    def test_corrupt_line_is_skipped(self):
        path = config.bar_path("X")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec("X", "2020-01-01")) + "\n")
            fh.write("{ broken line\n")
            fh.write(json.dumps(rec("X", "2020-01-02")) + "\n")
        self.assertEqual(len(store.load("X")), 2)

    def test_load_sorts(self):
        path = config.bar_path("X")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec("X", "2020-01-03")) + "\n")
            fh.write(json.dumps(rec("X", "2020-01-01")) + "\n")
        self.assertEqual([r["date"] for r in store.load("X")],
                         ["2020-01-01", "2020-01-03"])

    def test_only_contract_fields_written(self):
        row = rec("X", "2020-01-01")
        row["extra"] = "should vanish"
        store.write_atomic(config.bar_path("X"), [row])
        stored = store.load("X")[0]
        self.assertNotIn("extra", stored)
        self.assertEqual(set(stored), set(store.RECORD_FIELDS))


class TestStoreUpsert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._bars = config.BARS_DIR
        self._meta = config.META_PATH
        config.BARS_DIR = self.root / "bars"
        config.META_PATH = self.root / "meta.json"
        config.BARS_DIR.mkdir(parents=True, exist_ok=True)
        self.store = store.Store()

    def tearDown(self):
        config.BARS_DIR = self._bars
        config.META_PATH = self._meta
        self.tmp.cleanup()

    def test_idempotent_upsert(self):
        rows = [rec("X", "2020-01-01"), rec("X", "2020-01-02")]
        self.store.upsert("X", rows)
        self.store.upsert("X", rows)
        self.assertEqual(len(self.store.get("X")), 2)
        self.assertEqual(len(store.load("X")), 2)

    def test_incremental_overlap_overwrites(self):
        self.store.upsert("X", [rec("X", "2020-01-01", 1), rec("X", "2020-01-02", 1)])
        self.store.upsert("X", [rec("X", "2020-01-02", 5), rec("X", "2020-01-03", 5)])
        loaded = store.load("X")
        self.assertEqual([r["date"] for r in loaded],
                         ["2020-01-01", "2020-01-02", "2020-01-03"])
        self.assertEqual(loaded[1]["close"], 5.0)  # 覆盖
        self.assertEqual(loaded[0]["close"], 1.0)  # 保留

    def test_generation_advances(self):
        g0 = self.store.generation
        self.store.upsert("X", [rec("X", "2020-01-01")])
        self.assertGreater(self.store.generation, g0)

    def test_meta_persist_and_read(self):
        self.store.upsert("X", [rec("X", "2020-01-01")])
        self.store.persist_meta()
        meta = store.read_meta()
        self.assertIn("X", meta["instruments"])
        self.assertEqual(meta["instruments"]["X"]["rows"], 1)

    def test_mark_failed_records_error(self):
        self.store.mark_failed("X", "boom")
        self.assertEqual(self.store.instruments_meta["X"]["status"], "error")
        self.assertIn("boom", self.store.instruments_meta["X"]["error"])

    def test_meta_payload_stale_flag(self):
        # meta_payload 只输出 config.ORDER 里的标的，用 "X" 这种临时符号它不会出现
        self.store.upsert(config.GOLD, [rec(config.GOLD, "2020-01-01")])
        payload = self.store.meta_payload()
        gold = next(i for i in payload["instruments"] if i["symbol"] == config.GOLD)
        self.assertEqual(gold["rows"], 1)
        self.assertFalse(gold["stale"])  # 刚抓过，不该标过期
        others = [i for i in payload["instruments"] if i["symbol"] != config.GOLD]
        self.assertTrue(all(i["stale"] for i in others))  # 没抓过的必须标过期
        self.assertTrue(payload["stale"])


class TestSummarize(unittest.TestCase):
    def test_empty(self):
        info = store.summarize("AU9999", [])
        self.assertEqual(info["rows"], 0)
        self.assertIsNone(info["date_min"])

    def test_values(self):
        info = store.summarize("AU9999", [rec("AU9999", "2020-01-01", 7)])
        self.assertEqual(info["rows"], 1)
        self.assertEqual(info["date_min"], "2020-01-01")
        self.assertEqual(info["last_close"], 7.0)


if __name__ == "__main__":
    unittest.main()
