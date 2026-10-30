"""分析层测试：相关系数、领先滞后、状态判定，以及最关键的无未来函数。"""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from app import analytics, config


def dates(n: int, start: str = "2020-01-01") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


def make_bars(symbol: str, closes, idx: pd.DatetimeIndex, volume=None, source="t") -> list[dict]:
    rows = []
    for i, (d, c) in enumerate(zip(idx, closes)):
        c = float(c)
        rows.append(
            {
                "symbol": symbol,
                "date": d.strftime("%Y-%m-%d"),
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "volume": None if volume is None else float(volume[i]),
                "source": source,
                "fetched_at": "2026-01-01T00:00:00+08:00",
            }
        )
    return rows


def price_from_returns(r: np.ndarray, base: float = 100.0) -> np.ndarray:
    return base * np.exp(np.cumsum(r))


class TestPanel(unittest.TestCase):
    def test_shape_and_sorting(self):
        idx = dates(5)
        bars = {
            "AU9999": make_bars("AU9999", [1, 2, 3, 4, 5], idx),
            "sh000300": make_bars("sh000300", [9, 8, 7, 6, 5], idx),
        }
        px = analytics.panel(bars, "close")
        self.assertEqual(list(px.columns), ["AU9999", "sh000300"])
        self.assertTrue(px.index.is_monotonic_increasing)
        self.assertEqual(float(px["AU9999"].iloc[-1]), 5.0)

    def test_duplicate_dates_keep_last(self):
        idx = dates(2)
        rows = make_bars("X", [1, 2], idx)
        rows.append({**rows[0], "close": 99.0})
        px = analytics.panel({"X": rows}, "close")
        self.assertEqual(len(px), 2)
        self.assertEqual(float(px["X"].iloc[0]), 99.0)

    def test_empty(self):
        self.assertTrue(analytics.panel({}, "close").empty)


class TestCorrelation(unittest.TestCase):
    def test_perfect_positive(self):
        idx = dates(120)
        r = np.full(119, 0.01)
        gold = pd.Series(price_from_returns(r), index=idx[1:])
        other = pd.Series(price_from_returns(r, 200.0), index=idx[1:])
        rc = analytics.rolling_correlation(gold, other, 20).dropna()
        self.assertGreater(len(rc), 0)
        self.assertAlmostEqual(float(rc.iloc[-1]), 1.0, places=6)

    def test_perfect_negative(self):
        idx = dates(120)
        r = np.tile([0.01, -0.01], 60)[:119]
        gold = pd.Series(price_from_returns(r), index=idx[1:])
        other = pd.Series(price_from_returns(-r, 200.0), index=idx[1:])
        rc = analytics.rolling_correlation(gold, other, 20).dropna()
        self.assertAlmostEqual(float(rc.iloc[-1]), -1.0, places=6)

    def test_short_series_returns_empty(self):
        idx = dates(10)
        gold = pd.Series(np.arange(10) + 1.0, index=idx)
        rc = analytics.rolling_correlation(gold, gold * 2, 60)
        self.assertTrue(rc.empty)

    def test_matrix_diagonal_and_symmetry(self):
        rng = np.random.default_rng(3)
        idx = dates(300)
        cols = {}
        for name in ["AU9999", "sh000300", "sz399006"]:
            cols[name] = pd.Series(price_from_returns(rng.normal(0, 0.01, 299)), index=idx[1:])
        ret = pd.DataFrame(cols).pct_change(fill_method=None)
        out = analytics.corr_matrix(ret)
        self.assertEqual(out["labels"], ["AU9999", "sh000300", "sz399006"])
        mat = out["matrix"]
        for i in range(3):
            self.assertAlmostEqual(mat[i][i], 1.0, places=6)
            for j in range(3):
                self.assertAlmostEqual(mat[i][j], mat[j][i], places=6)


class TestLeadLag(unittest.TestCase):
    def test_detects_index_leading_by_3(self):
        """构造 idx_ret[t] = gold_ret[t+3]，即指数领先 3 天，峰值应落在 +3。"""
        rng = np.random.default_rng(7)
        n = 800
        r = rng.normal(0, 0.01, n)
        idx_ret = np.empty(n)
        idx_ret[: n - 3] = r[3:]
        idx_ret[n - 3 :] = 0.0

        d = dates(n)
        gold = pd.Series(price_from_returns(r), index=d)
        other = pd.Series(price_from_returns(idx_ret, 500.0), index=d)

        out = analytics.lead_lag(gold, other, max_lag=10)
        self.assertEqual(out["best_lag"], 3)
        self.assertEqual(out["verdict"], "index_leads")
        self.assertTrue(out["significant"])
        self.assertFalse(out["at_boundary"])
        self.assertGreater(out["best_corr"], 0.9)

    def test_short_series(self):
        d = dates(10)
        s = pd.Series(np.arange(10) + 1.0, index=d)
        out = analytics.lead_lag(s, s, max_lag=10)
        self.assertEqual(out["verdict"], "none")
        self.assertEqual(out["lags"], [])

    def test_lag_axis_is_symmetric(self):
        d = dates(200)
        rng = np.random.default_rng(1)
        a = pd.Series(price_from_returns(rng.normal(0, 0.01, 199)), index=d[1:])
        b = pd.Series(price_from_returns(rng.normal(0, 0.01, 199)), index=d[1:])
        out = analytics.lead_lag(a, b, max_lag=10)
        self.assertEqual(out["lags"], list(range(-10, 11)))
        self.assertEqual(len(out["corrs"]), 21)


class TestDivergence(unittest.TestCase):
    def test_gold_outperformance_gives_positive_z(self):
        d = dates(200)
        gold = pd.Series(100 * 1.004 ** np.arange(200), index=d)
        other = pd.Series(np.full(200, 100.0), index=d)
        z = analytics.divergence(gold, other, 60)
        self.assertGreater(float(z.iloc[-1]), 0)

    def test_short_series_empty(self):
        d = dates(30)
        s = pd.Series(np.arange(30) + 1.0, index=d)
        self.assertTrue(analytics.divergence(s, s, 60).empty)


class TestRegime(unittest.TestCase):
    def test_strong_uptrend_is_bull(self):
        d = dates(200)
        s = pd.Series(100 * 1.002 ** np.arange(200), index=d)
        self.assertEqual(analytics.regime_series(s).iloc[-1], "bull")

    def test_strong_downtrend_is_bear(self):
        d = dates(200)
        s = pd.Series(100 * 0.998 ** np.arange(200), index=d)
        self.assertEqual(analytics.regime_series(s).iloc[-1], "bear")

    def test_flat_is_range(self):
        """恒定价曾因浮点相等被判成空头，中性带修好后必须是震荡。"""
        d = dates(200)
        s = pd.Series(np.full(200, 100.0), index=d)
        self.assertEqual(analytics.regime_series(s).iloc[-1], "range")

    def test_short_series_is_na(self):
        d = dates(30)
        s = pd.Series(np.arange(30) + 1.0, index=d)
        self.assertTrue((analytics.regime_series(s) == "na").all())

    def test_stats_counts_cover_all(self):
        d = dates(300)
        s = pd.Series(price_from_returns(np.random.default_rng(2).normal(0, 0.01, 299)), index=d[1:])
        st = analytics.regime_series(s)
        ret = s.pct_change(fill_method=None)
        rows = analytics.regime_stats(ret, st)
        total = sum(r["count"] for r in rows)
        self.assertEqual(total, int(((st != "na") & ret.notna()).sum()))

    def test_transition_rows_sum_to_one(self):
        d = dates(400)
        s = pd.Series(price_from_returns(np.random.default_rng(5).normal(0, 0.01, 399)), index=d[1:])
        out = analytics.transition_matrix(analytics.regime_series(s))
        for row in out["matrix"]:
            vals = [v for v in row if v is not None]
            self.assertAlmostEqual(sum(vals), 1.0, places=3)


class TestNoLookahead(unittest.TestCase):
    """状态与滚动相关只能用当日及以前的数据，否则回测结论全错。"""

    def setUp(self):
        rng = np.random.default_rng(11)
        self.idx = dates(500)
        self.gold = pd.Series(
            price_from_returns(rng.normal(0, 0.01, 499)), index=self.idx[1:]
        )
        self.other = pd.Series(
            price_from_returns(rng.normal(0, 0.01, 499), 300.0), index=self.idx[1:]
        )

    def test_regime_stable_under_truncation(self):
        full = analytics.regime_series(self.gold)
        for cutoff in (250, 400, 499):
            trunc = analytics.regime_series(self.gold.iloc[:cutoff])
            self.assertEqual(
                full.iloc[cutoff - 1],
                trunc.iloc[-1],
                f"cutoff={cutoff} 状态依赖了未来数据",
            )

    def test_rolling_corr_stable_under_truncation(self):
        full = analytics.rolling_correlation(self.gold, self.other, 60)
        for cutoff in (250, 400, 499):
            trunc = analytics.rolling_correlation(
                self.gold.iloc[:cutoff], self.other.iloc[:cutoff], 60
            )
            a = full.loc[self.gold.index[cutoff - 1]]
            b = trunc.iloc[-1]
            self.assertAlmostEqual(float(a), float(b), places=9,
                                   msg=f"cutoff={cutoff} 滚动相关依赖了未来数据")


class TestJsonSafety(unittest.TestCase):
    def test_nan_and_inf_become_none(self):
        out = analytics._arr(pd.Series([1.0, np.nan, np.inf, -np.inf, 2.5]), 3)
        self.assertEqual(out, [1.0, None, None, None, 2.5])

    def test_dates_formatted(self):
        self.assertEqual(
            analytics._dates(pd.DatetimeIndex(["2020-01-02", "2021-12-31"])),
            ["2020-01-02", "2021-12-31"],
        )

    def test_empty_dates(self):
        self.assertEqual(analytics._dates(pd.DatetimeIndex([])), [])


class TestBuildPayload(unittest.TestCase):
    def _bars(self, n=300):
        d = dates(n)
        rng = np.random.default_rng(21)
        gold = price_from_returns(rng.normal(0, 0.008, n))
        out = {"AU9999": make_bars("AU9999", gold, d)}
        for i, sym in enumerate(config.INDICES):
            out[sym] = make_bars(
                sym,
                price_from_returns(rng.normal(0, 0.012, n), 1000 + i * 100),
                d,
                volume=rng.uniform(1e8, 5e9, n),
            )
        return out

    def test_ok_and_sections(self):
        p = analytics.build_payload(self._bars(), symbols=config.INDICES, window=60)
        self.assertTrue(p["ok"])
        for key in ("axis", "cards", "corr", "rolling", "leadlag", "divergence",
                    "volume", "regime", "alerts", "conditional"):
            self.assertIn(key, p)
        self.assertEqual(len(p["cards"]), len(config.ORDER))
        self.assertEqual(len(p["cards"]), 8)

    def test_volume_in_100m_units(self):
        p = analytics.build_payload(self._bars(), symbols=["sh000300"], window=60)
        v = p["series"]["sh000300"]["volume"][-1]
        self.assertIsNotNone(v)
        self.assertLess(v, 100)  # 5e9 股 = 50 亿股，不能是原始股数

    def test_window_validated(self):
        p = analytics.build_payload(self._bars(), symbols=["sh000300"], window=33)
        self.assertEqual(p["window"], config.DEFAULT_WINDOW)

    def test_missing_gold_is_error(self):
        bars = self._bars()
        del bars["AU9999"]
        p = analytics.build_payload(bars)
        self.assertFalse(p["ok"])
        self.assertIn("黄金", p["error"])

    def test_empty_range_is_error(self):
        p = analytics.build_payload(
            self._bars(), symbols=["sh000300"], start="2030-01-01", end="2030-12-31"
        )
        self.assertFalse(p["ok"])

    def test_json_serialisable(self):
        import json

        p = analytics.build_payload(self._bars(), symbols=config.INDICES, window=60)
        json.dumps(p, allow_nan=False)  # 有 NaN 会直接抛


if __name__ == "__main__":
    unittest.main()
