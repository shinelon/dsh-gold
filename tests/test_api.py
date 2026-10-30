"""HTTP 层测试：路由、参数校验、错误码、路径穿越防护。

用进程内服务 + 合成数据，不触网、不依赖已抓取的真实数据。
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

import numpy as np
import pandas as pd

from app import config, server


def synth_bars(n: int = 300) -> dict[str, list[dict]]:
    d = pd.bdate_range("2023-01-02", periods=n)
    rng = np.random.default_rng(42)
    out: dict[str, list[dict]] = {}
    for i, sym in enumerate(config.ORDER):
        r = rng.normal(0.0002, 0.01, n)
        px = 100 * np.exp(np.cumsum(r))
        rows = []
        for j, (day, c) in enumerate(zip(d, px)):
            c = float(c)
            rows.append(
                {
                    "symbol": sym,
                    "date": day.strftime("%Y-%m-%d"),
                    "open": round(c * 0.999, 4),
                    "high": round(c * 1.01, 4),
                    "low": round(c * 0.99, 4),
                    "close": round(c, 4),
                    "volume": None if sym == config.GOLD else float(rng.uniform(1e8, 5e9)),
                    "source": "synthetic",
                    "fetched_at": "2026-01-01T00:00:00+08:00",
                }
            )
        out[sym] = rows
    return out


class APITestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = server.AppState(refresh=False)
        cls.state.store.bars = synth_bars()
        cls.state.store.generation += 1
        cls.state.store.instruments_meta = {}
        cls.state._panels = None
        cls.state._panels_gen = -1
        cls.state._cache = {}
        cls.httpd = server.make_server("127.0.0.1", 0, cls.state)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path: str, method: str = "GET"):
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, self._decode(exc.read())

    @staticmethod
    def _decode(body: bytes):
        """只有 API 返回 JSON；`/` 与 `/static/*` 是 HTML/JS，解析失败就原样返回。"""
        if not body:
            return None
        try:
            return json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return body

    # -- 正常路径

    def test_health(self):
        status, body = self.req("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["refresh_enabled"])

    def test_meta(self):
        status, body = self.req("/api/meta")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["instruments"]), len(config.ORDER))
        symbols = {i["symbol"] for i in body["instruments"]}
        self.assertEqual(symbols, set(config.ORDER))

    def test_analysis_default(self):
        status, body = self.req("/api/analysis")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(len(body["cards"]), len(config.ORDER))
        self.assertTrue(body["axis"])
        self.assertEqual(body["window"], config.DEFAULT_WINDOW)

    def test_analysis_subset(self):
        status, body = self.req("/api/analysis?symbols=sh000300,sz399006&window=120")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body["symbols"]), ["sh000300", "sz399006"])
        self.assertEqual(body["window"], 120)
        self.assertEqual(len(body["rolling"]), 2)

    def test_analysis_full_range(self):
        status, body = self.req("/api/analysis?symbols=all&start=all&end=all&window=20")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["range"]["start"], "2023-01-02")

    def test_bars(self):
        status, body = self.req("/api/bars?symbols=sh000300&start=2023-06-01")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertIn("AU9999", body["bars"])
        self.assertIn("sh000300", body["bars"])
        self.assertTrue(all(r["date"] >= "2023-06-01" for r in body["bars"]["sh000300"]))

    def test_index_page(self):
        status, _ = self.req("/")
        self.assertEqual(status, 200)

    def test_static_asset(self):
        status, _ = self.req("/static/app.js")
        self.assertEqual(status, 200)

    # -- 参数校验

    def test_bad_window(self):
        status, body = self.req("/api/analysis?window=33")
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("window", body["error"])

    def test_bad_window_type(self):
        status, _ = self.req("/api/analysis?window=abc")
        self.assertEqual(status, 400)

    def test_bad_date(self):
        status, body = self.req("/api/analysis?start=2026-13-99")
        self.assertEqual(status, 400)
        self.assertIn("start", body["error"])

    def test_bad_symbol(self):
        status, body = self.req("/api/analysis?symbols=xx999")
        self.assertEqual(status, 400)
        self.assertIn("xx999", body["error"])

    # -- 错误码

    def test_unknown_route(self):
        status, _ = self.req("/api/nope")
        self.assertEqual(status, 404)

    def test_refresh_requires_post(self):
        status, _ = self.req("/api/refresh", "GET")
        self.assertEqual(status, 405)

    def test_refresh_disabled(self):
        status, body = self.req("/api/refresh", "POST")
        self.assertEqual(status, 409)
        self.assertFalse(body["ok"])

    def test_path_traversal_blocked(self):
        for path in ("/static/../run.py", "/static/..%2frun.py", "/static/../../app/config.py"):
            status, _ = self.req(path)
            self.assertIn(status, (403, 404), path)


if __name__ == "__main__":
    unittest.main()
