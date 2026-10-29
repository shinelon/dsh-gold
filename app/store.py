"""JSONL 存储层：一标的一文件，纯文本、原子写入、无数据库。

文件契约
--------
- 路径: data/bars/<symbol>.jsonl
- 每行一个紧凑 JSON 对象，字段见 config；按 date 升序，date 唯一。
- 写入: 先写 <file>.tmp 再 os.replace() 原子替换，崩溃不会破坏原文件。
- 读取: 逐行解析，坏行跳过并计数，不因单行损坏而中断。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

RECORD_FIELDS = (
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "fetched_at",
)


# ------------------------------------------------------------------ 纯函数


def _dumps(rec: dict) -> str:
    """只保留约定字段，保证文件行格式稳定。"""
    clean = {k: rec.get(k) for k in RECORD_FIELDS}
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


def parse_line(line: str) -> dict | None:
    """解析单行；非法返回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(rec, dict):
        return None
    if not rec.get("symbol") or not rec.get("date"):
        return None
    return rec


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    """按 date 合并，新记录覆盖旧的，返回按日期升序的唯一记录列表。"""
    by_date: dict[str, dict] = {}
    for rec in old:
        by_date[rec["date"]] = rec
    for rec in new:
        by_date[rec["date"]] = rec
    return [by_date[d] for d in sorted(by_date)]


def write_atomic(path: Path, rows: list[dict]) -> None:
    """原子写入 JSONL。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            for rec in rows:
                fh.write(_dumps(rec))
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # 同卷原子替换
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load(symbol: str) -> list[dict]:
    """读取单标的 JSONL；坏行跳过并告警。"""
    path = config.bar_path(symbol)
    if not path.exists():
        return []
    rows: list[dict] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            rec = parse_line(line)
            if rec is None:
                if line.strip():
                    bad += 1
                continue
            rows.append(rec)
    if bad:
        log.warning("%s 跳过 %d 行损坏记录", symbol, bad)
    rows.sort(key=lambda r: r["date"])
    return rows


def load_all() -> dict[str, list[dict]]:
    return {s: load(s) for s in config.ORDER}


def summarize(symbol: str, rows: list[dict]) -> dict:
    inst = config.INSTRUMENTS.get(symbol, {})
    if not rows:
        return {
            "symbol": symbol,
            "name": inst.get("name", symbol),
            "rows": 0,
            "date_min": None,
            "date_max": None,
            "last_close": None,
            "source": inst.get("source"),
        }
    return {
        "symbol": symbol,
        "name": inst.get("name", symbol),
        "rows": len(rows),
        "date_min": rows[0]["date"],
        "date_max": rows[-1]["date"],
        "last_close": rows[-1].get("close"),
        "source": rows[-1].get("source") or inst.get("source"),
    }


def read_meta() -> dict:
    path = config.META_PATH
    if not path.exists():
        return {"generated_at": None, "instruments": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("meta.json 读取失败: %s", exc)
        return {"generated_at": None, "instruments": {}}


def write_meta(payload: dict) -> None:
    path = config.META_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        # 必须显式 newline="\n"：默认的文本模式会在 Windows 下把 \n 翻成
        # \r\n，既与 write_atomic 的换行契约不一致，也让 meta.json 每次
        # 刷新都产生一行「整文件被改写」的无意义 diff。
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=TZ)
    return (datetime.now(TZ) - ts).total_seconds()


# ------------------------------------------------------------------ 记忆化存储


class Store:
    """内存中的 JSONL 快照 + 写入串行化，供常驻服务使用。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.bars: dict[str, list[dict]] = {}
        self.instruments_meta: dict[str, dict] = {}
        self.generation: int = 0  # 每次写入自增，供分析缓存失效

    # -- 载入

    def load_all(self) -> None:
        with self._lock:
            config.ensure_dirs()
            self.bars = load_all()
            meta = read_meta()
            self.instruments_meta = meta.get("instruments", {})
            self.generation += 1
            counts = {s: len(v) for s, v in self.bars.items()}
            log.info("载入完成: %s", counts)

    # -- 写入

    def upsert(self, symbol: str, rows: list[dict], status: str = "ok", error: str | None = None) -> int:
        """增量合并并原子重写单标的文件，返回合并后行数。"""
        with self._lock:
            path = config.bar_path(symbol)
            old = self.bars.get(symbol) or load(symbol)
            merged = merge(old, rows)
            write_atomic(path, merged)
            self.bars[symbol] = merged
            info = summarize(symbol, merged)
            info.update(
                {
                    "last_fetch": _now_iso(),
                    "status": status,
                    "error": error,
                }
            )
            self.instruments_meta[symbol] = info
            self.generation += 1
            return len(merged)

    def mark_failed(self, symbol: str, error: str) -> None:
        with self._lock:
            info = dict(self.instruments_meta.get(symbol) or summarize(symbol, self.bars.get(symbol, [])))
            info["last_fetch"] = _now_iso()
            info["status"] = "error"
            info["error"] = error[:300]
            self.instruments_meta[symbol] = info

    def persist_meta(self) -> None:
        with self._lock:
            write_meta(
                {
                    "generated_at": _now_iso(),
                    "instruments": self.instruments_meta,
                }
            )

    # -- 读取

    def get(self, symbol: str) -> list[dict]:
        return self.bars.get(symbol, [])

    def snapshot(self) -> tuple[dict[str, list[dict]], int]:
        with self._lock:
            return self.bars, self.generation

    # -- 元信息

    def meta_payload(self) -> dict:
        with self._lock:
            out = []
            any_stale = False
            for symbol in config.ORDER:
                inst = config.INSTRUMENTS[symbol]
                info = dict(self.instruments_meta.get(symbol) or summarize(symbol, self.bars.get(symbol, [])))
                info.setdefault("name", inst["name"])
                info["unit"] = inst["unit"]
                info["kind"] = inst["kind"]
                info["color"] = inst["color"]
                info["short"] = inst["short"]
                age = _age_seconds(info.get("last_fetch"))
                stale = (
                    info.get("status") != "ok"
                    or age is None
                    or age > config.STALE_AFTER_SEC
                )
                info["stale"] = stale
                info["age_seconds"] = None if age is None else round(age, 1)
                if stale:
                    any_stale = True
                out.append(info)
            return {
                "instruments": out,
                "generated_at": _now_iso(),
                "stale": any_stale,
                "refresh_interval_sec": config.REFRESH_INTERVAL_SEC,
                "windows": config.ROLLING_WINDOWS,
                "default_window": config.DEFAULT_WINDOW,
                "max_lag": config.MAX_LAG,
                "divergence_z": config.DIVERGENCE_Z,
                "state_labels": config.STATE_LABELS,
                "state_colors": config.STATE_COLORS,
            }
