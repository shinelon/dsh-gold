"""数据采集：上金所 Au99.99 与新浪 A 股指数日线。

设计要点（均为实测踩坑后固化）：
- 上金所返回列序是 [日期, 开, 收, 低, 高]，不是 OHLC，必须显式映射；
  且存在高低价无法包围开收价的脏行，统一用 max/min 归一化兜底。
- 新浪返回的数值是字符串，必须显式转 float。
- 两端都可能返回全 0 行（无成交日），需过滤。
- 每次成功抓取的原始文本按日期归档，便于源端改数后回溯比对。
"""
from __future__ import annotations

import gzip
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

TZ = ZoneInfo("Asia/Shanghai")

SGE_URL = "https://www.sge.com.cn/graph/Dailyhq?instid={instid}"
SGE_REFERER = "https://www.sge.com.cn/"
SINA_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
    "/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
)
SINA_REFERER = "https://finance.sina.com.cn/"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SourceError(RuntimeError):
    """上游数据源不可用或返回无法解析的内容。"""


# ------------------------------------------------------------------ 工具


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_key() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _http_get(url: str, referer: str) -> str:
    """带重试与 gzip 解码的 GET，返回 UTF-8 文本。"""
    headers = {
        "User-Agent": _UA,
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    last: Exception | None = None
    for attempt in range(config.HTTP_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                req, timeout=config.HTTP_TIMEOUT, context=ssl.create_default_context()
            ) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
            if not text.strip():
                raise SourceError("响应为空")
            return text
        except (urllib.error.URLError, OSError, gzip.BadGzipFile, SourceError) as exc:
            last = exc
            if attempt < config.HTTP_RETRIES - 1:
                backoff = 2**attempt
                log.warning("请求失败(%s)，%ss 后重试: %s", exc, backoff, url)
                time.sleep(backoff)
    raise SourceError(f"请求失败: {url} ({last})")


def _f(value) -> float | None:
    """宽松转 float：空串/None/'--' 一律返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s or s in {"-", "--", "null", "None"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_record(
    symbol: str,
    day: str,
    o: float | None,
    c: float | None,
    lo: float | None,
    hi: float | None,
    volume: float | None,
    source: str,
    fetched_at: str,
) -> dict | None:
    """校验并归一化单条日线；无效返回 None。"""
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    vals = [o, c, lo, hi]
    if any(v is None for v in vals):
        return None
    if all(abs(v) < 1e-12 for v in vals):  # 无成交日的全 0 行
        return None
    if min(vals) <= 0:
        return None
    # 兜底：保证 high >= max(open, close) 且 low <= min(open, close)
    high = max(vals)
    low = min(vals)
    return {
        "symbol": symbol,
        "date": day,
        "open": round(o, 4),
        "high": round(high, 4),
        "low": round(low, 4),
        "close": round(c, 4),
        "volume": None if volume is None else round(volume, 2),
        "source": source,
        "fetched_at": fetched_at,
    }


def archive_raw(symbol: str, text: str) -> Path:
    """把原始响应落到 data/raw/<YYYY-MM-DD>/<symbol>.json。"""
    day_dir = Path(config.RAW_DIR) / today_key()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{symbol}.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


# ------------------------------------------------------------------ 黄金


def parse_gold(text: str, fetched_at: str | None = None) -> list[dict]:
    """解析上金所 Dailyhq 响应。

    结构: {"time": [[日期, 开, 收, 低, 高], ...]}
    注意列序是 开/收/低/高 —— 与日历常见的 OHLC 不同。
    """
    fetched_at = fetched_at or now_iso()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"上金所响应非 JSON: {exc}") from exc
    rows = payload.get("time")
    if not isinstance(rows, list):
        raise SourceError("上金所响应缺少 time 字段")

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        day = str(row[0]).strip()
        o, c, lo, hi = (_f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]))
        rec = _norm_record(
            config.GOLD, day, o, c, lo, hi, None, "sge", fetched_at
        )
        if rec:
            out.append(rec)
    if not out:
        raise SourceError("上金所响应解析后无有效记录")
    return out


def fetch_gold(archive: bool = True) -> list[dict]:
    url = SGE_URL.format(instid=config.INSTRUMENTS[config.GOLD]["source_id"])
    text = _http_get(url, SGE_REFERER)
    if archive:
        try:
            archive_raw(config.GOLD, text)
        except OSError as exc:  # 归档失败不应影响取数
            log.warning("归档失败 %s: %s", config.GOLD, exc)
    return parse_gold(text)


# ------------------------------------------------------------------ 指数


def parse_index(symbol: str, text: str, fetched_at: str | None = None) -> list[dict]:
    """解析新浪 getKLineData 响应。

    结构: [{"day":"2026-10-29","open":"...","high":"...","low":"...",
            "close":"...","volume":"..."}, ...]  —— 数值均为字符串。
    """
    fetched_at = fetched_at or now_iso()
    if text.lstrip().startswith("null"):
        raise SourceError(f"新浪返回 null: {symbol}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"新浪响应非 JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise SourceError(f"新浪响应结构异常: {symbol}")

    out: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        day = str(item.get("day", "")).strip()[:10]
        rec = _norm_record(
            symbol,
            day,
            _f(item.get("open")),
            _f(item.get("close")),
            _f(item.get("low")),
            _f(item.get("high")),
            _f(item.get("volume")),
            "sina",
            fetched_at,
        )
        if rec:
            out.append(rec)
    if not out:
        raise SourceError(f"新浪响应解析后无有效记录: {symbol}")
    return out


def fetch_index(symbol: str, archive: bool = True) -> list[dict]:
    inst = config.INSTRUMENTS[symbol]
    url = SINA_URL.format(symbol=inst["source_id"], datalen=config.SINA_DATALEN)
    text = _http_get(url, SINA_REFERER)
    if archive:
        try:
            archive_raw(symbol, text)
        except OSError as exc:
            log.warning("归档失败 %s: %s", symbol, exc)
    return parse_index(symbol, text)


def fetch_symbol(symbol: str, archive: bool = True) -> list[dict]:
    if symbol == config.GOLD:
        return fetch_gold(archive=archive)
    return fetch_index(symbol, archive=archive)


def fetch_all(archive: bool = True) -> dict[str, list[dict]]:
    """抓取全部标的；单个失败不影响其余，失败项不出现在结果中。"""
    result: dict[str, list[dict]] = {}
    for symbol in config.ORDER:
        try:
            result[symbol] = fetch_symbol(symbol, archive=archive)
            log.info("%s 抓取成功 %d 行", symbol, len(result[symbol]))
        except Exception as exc:  # noqa: BLE001 - 需容错继续
            log.error("%s 抓取失败: %s", symbol, exc)
    return result
