"""分析层：金股联动 / 量能 / 市场状态识别。

三个刻意遵循的原则
------------------
1. **先在完整历史上计算，再按请求区间切片**。滚动均线、滚动相关、状态判定
   都需要 start 之前的历史，否则视图开头会凭空出现一段 NaN。
2. **配对分析取两序列的日期交集，收益绝不前向填充**，避免停牌/休市日被
   错误地当成 0 收益，污染相关系数。
3. 所有输出必须是 JSON 可序列化的：NaN / inf 统一转 None。
"""
from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from . import config

STATE_CODES = ("bull", "bear", "range")
TRADING_DAYS = 252


# ------------------------------------------------------------------ 基础工具


def _num(value: Any, nd: int = 6) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


def _arr(values: Iterable[Any], nd: int = 6) -> list[float | None]:
    """向量化取整 + NaN/inf 转 None。

    逐元素调用 _num 会在全量历史上产生 20 万次函数调用（实测占掉近 1 秒），
    这里走 numpy 一次取整，再只做一次轻量列表推导。
    """
    a = np.round(np.asarray(values, dtype="float64"), nd)
    return [v if math.isfinite(v) else None for v in a.tolist()]


def _dates(index: pd.Index) -> list[str]:
    """向量化日期格式化；逐元素 strftime 同样是大头开销。"""
    if len(index) == 0:
        return []
    return pd.DatetimeIndex(index).strftime("%Y-%m-%d").tolist()


def panel(bars: dict[str, list[dict]], field: str = "close") -> pd.DataFrame:
    """把 JSONL 记录转成 宽表（index=交易日，columns=symbol）。"""
    series = {}
    for symbol, rows in bars.items():
        if not rows:
            continue
        idx = pd.to_datetime([r["date"] for r in rows])
        vals = [r.get(field) for r in rows]
        s = pd.Series(vals, index=idx, dtype="float64", name=symbol)
        s = s[~s.index.duplicated(keep="last")].sort_index()
        series[symbol] = s
    if not series:
        return pd.DataFrame()
    return pd.DataFrame(series).sort_index()


def returns(px: pd.DataFrame) -> pd.DataFrame:
    return px.pct_change(fill_method=None)


def _slice(obj, start: str | None, end: str | None):
    if start:
        obj = obj[obj.index >= pd.Timestamp(start)]
    if end:
        obj = obj[obj.index <= pd.Timestamp(end)]
    return obj


def _pair(gold: pd.Series, other: pd.Series) -> pd.DataFrame:
    """两序列按日期交集对齐，去掉任一缺失的日期。"""
    df = pd.concat([gold.rename("gold"), other.rename("other")], axis=1, join="inner")
    return df.dropna()


# ------------------------------------------------------------------ 联动


def corr_matrix(ret: pd.DataFrame) -> dict:
    labels = list(ret.columns)
    if ret.shape[1] < 2:
        return {"labels": labels, "matrix": []}
    mat = ret.corr(method="pearson")
    return {
        "labels": labels,
        "matrix": [[_num(mat.loc[a, b], 4) for b in labels] for a in labels],
    }


def rolling_correlation(
    gold: pd.Series, other: pd.Series, window: int, ret: pd.DataFrame | None = None
) -> pd.Series:
    """gold 与 other 日收益的滚动相关；在交集上计算。

    ret 可传入已算好的 [gold, other] 收益两列，避免同一配对重复对齐与求收益。
    """
    if ret is None:
        df = _pair(gold, other)
        if len(df) < window + 2:
            return pd.Series(dtype="float64")
        ret = df.pct_change(fill_method=None).dropna()
    if ret.empty or len(ret) < window:
        return pd.Series(dtype="float64")
    return ret["gold"].rolling(window).corr(ret["other"])


def lead_lag(
    gold: pd.Series,
    other: pd.Series,
    max_lag: int = config.MAX_LAG,
    ret: pd.DataFrame | None = None,
) -> dict:
    """互相关：r(k) = corr(gold_t, other_{t-k})。

    k > 0 表示指数领先黄金（指数 t-k 与黄金 t 相关），
    k < 0 表示黄金领先指数。
    """
    if ret is None:
        df = _pair(gold, other)
        if len(df) < 30:
            return _empty_leadlag()
        ret = df.pct_change(fill_method=None).dropna()
    if ret.empty or len(ret) < 30:
        return _empty_leadlag()

    a = ret["gold"].to_numpy(dtype="float64")
    b = ret["other"].to_numpy(dtype="float64")
    n = len(a)

    lags, corrs, ns = [], [], []
    for k in range(-max_lag, max_lag + 1):
        if k >= 0:
            x, y = a[k:], b[: n - k]
        else:
            m = -k
            x, y = a[: n - m], b[m:]
        mask = np.isfinite(x) & np.isfinite(y)
        cnt = int(mask.sum())
        lags.append(k)
        ns.append(cnt)
        if cnt < 20:
            corrs.append(None)
            continue
        xs, ys = x[mask], y[mask]
        sx, sy = xs.std(), ys.std()
        if sx > 0 and sy > 0:
            c = float(np.mean((xs - xs.mean()) * (ys - ys.mean())) / (sx * sy))
        else:
            c = 0.0
        corrs.append(_num(c, 4))

    pairs = [(k, c) for k, c in zip(lags, corrs) if c is not None]
    if pairs:
        best_lag, best_corr = max(pairs, key=lambda kc: abs(kc[1]))
    else:
        best_lag, best_corr = 0, None
    # 显著性参考带: 1.96/sqrt(n)，取样本量最小者，给一个保守带宽
    n_min = min([x for x in ns if x > 0], default=0)
    band = _num(1.96 / math.sqrt(n_min), 4) if n_min > 30 else None

    # 判定逻辑的两处必要保守：
    # - 要检验 21 个滞后，最大值天然被多重比较抬高，故要求 1.5 倍带宽才算显著；
    # - 峰值落在搜索边界说明真实峰值可能在窗外，不能宣称"滞后 N 日"。
    significant = (
        best_corr is not None
        and band is not None
        and abs(best_corr) >= band * 1.5
    )
    at_boundary = abs(best_lag) >= max_lag
    if not significant or at_boundary:
        verdict = "none"
    elif best_lag > 0:
        verdict = "index_leads"
    elif best_lag < 0:
        verdict = "gold_leads"
    else:
        verdict = "contemporaneous"

    return {
        "lags": lags,
        "corrs": corrs,
        "best_lag": best_lag,
        "best_corr": best_corr,
        "band": band,
        "n": n_min,
        "significant": bool(significant),
        "at_boundary": bool(at_boundary),
        "verdict": verdict,
    }


def _empty_leadlag() -> dict:
    return {
        "lags": [],
        "corrs": [],
        "best_lag": 0,
        "best_corr": None,
        "band": None,
        "n": 0,
        "significant": False,
        "at_boundary": False,
        "verdict": "none",
    }


def divergence(
    gold: pd.Series, other: pd.Series, window: int = 60, pair: pd.DataFrame | None = None
) -> pd.Series:
    """相对强弱 z 分数：ln(金) − ln(股) 的滚动标准化。"""
    df = pair if pair is not None else _pair(gold, other)
    if len(df) < window + 2:
        return pd.Series(dtype="float64")
    spread = np.log(df["gold"]) - np.log(df["other"])
    mean = spread.rolling(window).mean()
    std = spread.rolling(window).std()
    z = (spread - mean) / std.replace(0, np.nan)
    return z.dropna()


def conditional_stats(
    gold: pd.Series, other: pd.Series, ret: pd.DataFrame | None = None
) -> dict:
    """指数上涨/下跌日，黄金的表现与同向概率（避险属性检验）。"""
    if ret is None:
        df = _pair(gold, other)
        ret = df.pct_change(fill_method=None).dropna() if len(df) >= 30 else pd.DataFrame()
    if len(ret) < 30:
        return {}
    g, o = ret["gold"], ret["other"]
    up, down = o > 0, o < 0
    n_up, n_down = int(up.sum()), int(down.sum())
    out: dict[str, Any] = {
        "n_up": n_up,
        "n_down": n_down,
        "gold_mean_up": _num(g[up].mean()) if n_up else None,
        "gold_mean_down": _num(g[down].mean()) if n_down else None,
        "p_gold_up_given_idx_down": _num((g[down] > 0).mean()) if n_down else None,
        "p_gold_down_given_idx_down": _num((g[down] < 0).mean()) if n_down else None,
    }
    out["corr_up"] = _num(g[up].corr(o[up]), 4) if n_up > 10 else None
    out["corr_down"] = _num(g[down].corr(o[down]), 4) if n_down > 10 else None
    # 指数最差 10% 交易日，黄金平均收益
    if len(o) >= 50:
        thr = o.quantile(0.10)
        worst = o <= thr
        out["worst_decile_threshold"] = _num(thr)
        out["gold_mean_worst_decile"] = _num(g[worst].mean())
        out["gold_up_rate_worst_decile"] = _num((g[worst] > 0).mean())
        out["n_worst"] = int(worst.sum())
    return out


# ------------------------------------------------------------------ 量能


def volume_metrics(volume: pd.Series, px: pd.Series) -> dict[str, pd.Series]:
    v = volume.dropna()
    if v.empty:
        return {}
    ma20 = v.rolling(20).mean()
    ma60 = v.rolling(60).mean()
    std20 = v.rolling(20).std()
    ratio = v / ma20.replace(0, np.nan)
    z = (v - ma20) / std20.replace(0, np.nan)

    # 量价配合度：成交量变化 与 涨跌幅绝对值 的 60 日滚动相关
    aligned = pd.concat([v, px], axis=1, join="inner").dropna()
    aligned.columns = ["v", "p"]
    if len(aligned) > 61:
        dlnv = np.log(aligned["v"].replace(0, np.nan)).diff()
        absret = aligned["p"].pct_change(fill_method=None).abs()
        pv = dlnv.rolling(60).corr(absret)
    else:
        pv = pd.Series(dtype="float64")

    return {"volume": v, "ma20": ma20, "ma60": ma60, "ratio": ratio, "z": z, "pv_corr": pv}


def volume_flags(volume: pd.Series, px: pd.Series, lookback: int = 20) -> list[dict]:
    """识别缩量新高 / 放量滞涨。"""
    flags: list[dict] = []
    v = volume.dropna()
    if len(v) < lookback + 2 or px.dropna().empty:
        return flags
    p = px.dropna()
    common = p.index.intersection(v.index)
    if len(common) < lookback + 2:
        return flags
    p, v = p.loc[common], v.loc[common]
    vol_z = (v - v.rolling(lookback).mean()) / v.rolling(lookback).std().replace(0, np.nan)
    last_p, last_v = p.iloc[-1], vol_z.iloc[-1]
    if math.isnan(last_v):
        return flags
    high_20 = p.iloc[-lookback:].max()
    ret_5 = p.iloc[-1] / p.iloc[-6] - 1 if len(p) > 6 else 0.0
    if last_p >= high_20 and last_v < 0:
        flags.append(
            {
                "type": "shrinking_high",
                "label": "缩量新高",
                "detail": f"价格创{lookback}日新高但量能 z={last_v:.2f}，上涨动能不足",
                "severity": "warn",
            }
        )
    if last_v > 2 and abs(ret_5) < 0.005:
        flags.append(
            {
                "type": "heavy_stall",
                "label": "放量滞涨",
                "detail": f"量能 z={last_v:.2f} 显著放大但5日涨跌仅 {ret_5 * 100:+.2f}%",
                "severity": "warn",
            }
        )
    if last_v >= 2.5:
        flags.append(
            {
                "type": "volume_spike",
                "label": "异常放量",
                "detail": f"成交量为{lookback}日均量的 {last_v:.1f} 个标准差",
                "severity": "info",
            }
        )
    return flags


# ------------------------------------------------------------------ 状态识别


def regime_series(px: pd.Series, fast: int = config.REGIME_FAST, slow: int = config.REGIME_SLOW) -> pd.Series:
    """打分制状态判定 → 多头 / 空头 / 震荡 / 数据不足。

    三个独立信号各投一票（+1 / -1 / 0）：
      1. 价格相对 MA20；
      2. MA20 相对 MA60；
      3. MA60 的 20 日斜率，带 0.5% 死区。

    死区是必要的：长均线走平时斜率可能是 +0.1 这种纯噪声，
    若按正负号硬判会把"价格跌破两条均线"误标成震荡。
    总分 >= +2 判多头，<= -2 判空头，其余震荡。
    """
    s = px.dropna()
    state = pd.Series("na", index=s.index, dtype=object)
    if len(s) < slow + 21:
        return state
    ma_f = s.rolling(fast, min_periods=fast).mean()
    ma_s = s.rolling(slow, min_periods=slow).mean()
    slope = ma_s - ma_s.shift(20)
    valid = ma_s.notna() & slope.notna() & ma_f.notna()

    deadband = ma_s.abs() * 0.005  # 长均线 20 日变动不足 0.5% 视为走平
    tol = 0.001  # 价格/均线相差不足 0.1% 视为持平，避免浮点相等被判成单边

    sig1 = pd.Series(0, index=s.index)
    sig1[s > ma_f * (1 + tol)] = 1
    sig1[s < ma_f * (1 - tol)] = -1

    sig2 = pd.Series(0, index=s.index)
    sig2[ma_f > ma_s * (1 + tol)] = 1
    sig2[ma_f < ma_s * (1 - tol)] = -1

    sig3 = pd.Series(0, index=s.index)
    sig3[slope > deadband] = 1
    sig3[slope < -deadband] = -1

    score = sig1 + sig2 + sig3

    state[valid] = "range"
    state[valid & (score >= 2)] = "bull"
    state[valid & (score <= -2)] = "bear"
    return state


def regime_stats(ret: pd.Series, state: pd.Series) -> list[dict]:
    """各状态下的表现统计。

    刻意不输出"累计收益"：多头日的收益跨非连续日期连乘没有经济含义
    （会把空头期间的间隔凭空跳过），只会给出误导性的天文数字。
    """
    df = pd.concat([ret.rename("ret"), state.rename("state")], axis=1, join="inner").dropna()
    rows = []
    for code in STATE_CODES:
        sub = df[df["state"] == code]["ret"]
        n = len(sub)
        rows.append(
            {
                "state": code,
                "label": config.STATE_LABELS[code],
                "count": n,
                "share": _num(n / len(df) * 100, 2) if len(df) else None,
                "mean_daily": _num(sub.mean() * 100, 4) if n else None,
                "ann_vol": _num(sub.std() * math.sqrt(TRADING_DAYS) * 100, 2) if n > 1 else None,
                "win_rate": _num((sub > 0).mean() * 100, 2) if n else None,
            }
        )
    return rows


def transition_matrix(state: pd.Series) -> dict:
    seq = [s for s in state.tolist() if s in STATE_CODES]
    if len(seq) < 2:
        return {"labels": [config.STATE_LABELS[c] for c in STATE_CODES], "matrix": []}
    counts = {a: {b: 0 for b in STATE_CODES} for a in STATE_CODES}
    for a, b in zip(seq[:-1], seq[1:]):
        counts[a][b] += 1
    mat = []
    for a in STATE_CODES:
        total = sum(counts[a].values())
        mat.append([_num(counts[a][b] / total, 4) if total else None for b in STATE_CODES])
    return {"labels": [config.STATE_LABELS[c] for c in STATE_CODES], "matrix": mat}


# ------------------------------------------------------------------ 汇总


def build_cards(px: pd.DataFrame, regime: dict[str, pd.Series]) -> list[dict]:
    cards = []
    gold = px[config.GOLD].dropna() if config.GOLD in px else None
    for symbol in config.ORDER:
        if symbol not in px:
            continue
        inst = config.INSTRUMENTS[symbol]
        s = px[symbol].dropna()
        if s.empty:
            continue
        close = float(s.iloc[-1])
        prev = float(s.iloc[-2]) if len(s) > 1 else None
        chg = (close / prev - 1) * 100 if prev else None
        st = regime.get(symbol)
        state = st.iloc[-1] if st is not None and not st.empty else "na"

        corr60 = None
        if symbol != config.GOLD and gold is not None:
            rc = rolling_correlation(gold, s, 60)
            if not rc.empty:
                corr60 = _num(rc.iloc[-1], 4)

        cards.append(
            {
                "symbol": symbol,
                "name": inst["name"],
                "short": inst["short"],
                "kind": inst["kind"],
                "unit": inst["unit"],
                "color": inst["color"],
                "date": s.index[-1].strftime("%Y-%m-%d"),
                "close": _num(close, 2),
                "chg_pct": _num(chg, 2),
                "state": state,
                "state_label": config.STATE_LABELS.get(state, "数据不足"),
                "corr60": corr60,
            }
        )
    return cards


def build_payload(
    bars: dict[str, list[dict]],
    symbols: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    window: int = config.DEFAULT_WINDOW,
    panels: tuple[pd.DataFrame, pd.DataFrame] | None = None,
) -> dict:
    """组装前端所需的完整分析载荷。

    panels 可传入已构建好的 (价格宽表, 成交量宽表) 以跳过重复解析，
    常驻服务会按数据代次缓存它。
    """
    if panels is not None:
        px, vol = panels
    else:
        px = panel(bars, "close")
        vol = panel(bars, "volume")

    if px.empty or config.GOLD not in px:
        return {"ok": False, "error": "缺少黄金数据，无法进行联动分析"}

    symbols = [s for s in (symbols or config.INDICES) if s in px and s != config.GOLD]
    if not symbols:
        symbols = [s for s in config.INDICES if s in px]
    window = int(window) if window in config.ROLLING_WINDOWS else config.DEFAULT_WINDOW

    gold_full = px[config.GOLD].dropna()
    ret_full = returns(px)

    # ---- 卡片（全部标的，用于顶部概览）
    regime_full = {s: regime_series(px[s]) for s in config.ORDER if s in px}
    cards = build_cards(px, regime_full)

    # ---- 主图：黄金 + 选中指数，按日期并集对齐，缺失处留 null
    axis_index = gold_full.index
    for s in symbols:
        axis_index = axis_index.union(px[s].dropna().index)
    axis_index = _slice(pd.Series(index=axis_index, dtype="float64"), start, end).index
    if len(axis_index) == 0:
        return {"ok": False, "error": "所选区间内没有数据"}

    series: dict[str, dict] = {}
    for symbol in [config.GOLD] + symbols:
        if symbol not in px:
            continue
        entry = {"close": _arr(px[symbol].reindex(axis_index), 3)}
        if symbol != config.GOLD and symbol in vol:
            v = vol[symbol].reindex(axis_index) / 1e8  # 股 -> 亿股
            entry["volume"] = _arr(v, 2)
        series[symbol] = entry

    # ---- 联动：区间内相关矩阵
    ret_win = _slice(ret_full[[config.GOLD] + symbols], start, end)
    ret_win = ret_win.loc[:, ret_win.notna().any()]
    corr = corr_matrix(ret_win.dropna(how="all"))

    rolling: dict[str, dict] = {}
    leadlag: dict[str, dict] = {}
    divergence_out: dict[str, dict] = {}
    conditional: dict[str, dict] = {}
    for s in symbols:
        other = px[s].dropna()
        pair = _pair(gold_full, other)
        pair_ret = pair.pct_change(fill_method=None).dropna() if len(pair) > 2 else pd.DataFrame()

        rc = _slice(rolling_correlation(gold_full, other, window, ret=pair_ret).dropna(), start, end)
        rolling[s] = {"dates": _dates(rc.index), "values": _arr(rc, 4)}

        leadlag[s] = lead_lag(gold_full, other, config.MAX_LAG, ret=pair_ret)

        dz = _slice(divergence(gold_full, other, 60, pair=pair), start, end)
        current = _num(dz.iloc[-1], 3) if not dz.empty else None
        flag = None
        if current is not None and abs(current) >= config.DIVERGENCE_Z:
            flag = "gold_strong" if current > 0 else "equity_strong"
        divergence_out[s] = {
            "dates": _dates(dz.index),
            "z": _arr(dz, 3),
            "current": current,
            "flag": flag,
        }
        conditional[s] = conditional_stats(gold_full, other, ret=pair_ret)

    # ---- 量能
    volume_out: dict[str, dict] = {}
    for s in symbols:
        vm = volume_metrics(vol[s].dropna(), px[s].dropna())
        if not vm:
            volume_out[s] = {}
            continue
        idx = _slice(vm["volume"], start, end).index
        pv = _slice(vm["pv_corr"], start, end)
        volume_out[s] = {
            "dates": _dates(idx),
            "volume": _arr(vm["volume"].reindex(idx) / 1e8, 2),
            "ma20": _arr(vm["ma20"].reindex(idx) / 1e8, 2),
            "ma60": _arr(vm["ma60"].reindex(idx) / 1e8, 2),
            "ratio": _arr(vm["ratio"].reindex(idx), 3),
            "z": _arr(vm["z"].reindex(idx), 3),
            "pv_corr": _arr(pv, 3),
            "pv_corr_dates": _dates(pv.index),
        }

    # ---- 状态
    regime_out: dict[str, dict] = {}
    for s in [config.GOLD] + symbols:
        if s not in regime_full:
            continue
        st_win = _slice(regime_full[s], start, end)
        ret_win_s = _slice(ret_full[s], start, end)
        regime_out[s] = {
            "dates": _dates(st_win.index),
            "states": st_win.tolist(),
            "current": st_win.iloc[-1] if not st_win.empty else "na",
            "stats": regime_stats(ret_win_s, st_win),
            "transitions": transition_matrix(st_win),
        }

    # ---- 预警
    alerts: list[dict] = []
    for s in symbols:
        name = config.INSTRUMENTS[s]["name"]
        d = divergence_out.get(s) or {}
        if d.get("flag"):
            direction = "金强股弱" if d["flag"] == "gold_strong" else "股强金弱"
            alerts.append(
                {
                    "symbol": s,
                    "name": name,
                    "type": "divergence",
                    "label": "相对强弱背离",
                    "detail": f"{direction}，z={d['current']:+.2f}（阈值 ±{config.DIVERGENCE_Z}）",
                    "severity": "warn",
                }
            )
        for f in volume_flags(vol[s].dropna(), px[s].dropna()):
            alerts.append({"symbol": s, "name": name, **f})

    return {
        "ok": True,
        "asof": axis_index[-1].strftime("%Y-%m-%d"),
        "range": {
            "start": axis_index[0].strftime("%Y-%m-%d"),
            "end": axis_index[-1].strftime("%Y-%m-%d"),
        },
        "window": window,
        "axis": _dates(axis_index),
        "symbols": symbols,
        "volume_unit": "亿股",
        "series": series,
        "cards": cards,
        "corr": corr,
        "rolling": rolling,
        "leadlag": leadlag,
        "divergence": divergence_out,
        "conditional": conditional,
        "volume": volume_out,
        "regime": regime_out,
        "alerts": alerts,
    }
