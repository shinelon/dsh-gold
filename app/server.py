"""常驻本地 HTTP 服务：静态页面 + JSON API + 后台刷新线程。

只用标准库，不引入任何依赖。默认仅监听 127.0.0.1。
"""
from __future__ import annotations

import gzip
import json
import logging
import mimetypes
import posixpath
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from . import analytics, config, sources, store

log = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

MAX_BODY = 1 << 20


# ------------------------------------------------------------------ 状态


class AppState:
    """服务共享状态：数据快照、面板缓存、分析缓存、刷新控制。"""

    def __init__(self, refresh: bool = True) -> None:
        self.store = store.Store()
        self.store.load_all()
        self.refresh_enabled = refresh
        self.started_at = time.time()

        self._lock = threading.RLock()
        self._panels: tuple[pd.DataFrame, pd.DataFrame] | None = None
        self._panels_gen: int = -1
        self._cache: dict[tuple, dict] = {}

        self.refreshing = False
        self.last_refresh: str | None = None
        self.last_error: str | None = None
        self.last_summary: dict = {}
        self._stop = threading.Event()

    # -- 缓存

    def panels(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        with self._lock:
            bars, gen = self.store.snapshot()
            if self._panels is None or self._panels_gen != gen:
                self._panels = (
                    analytics.panel(bars, "close"),
                    analytics.panel(bars, "volume"),
                )
                self._panels_gen = gen
                self._cache.clear()
            return self._panels

    def analysis(self, symbols, start, end, window) -> dict:
        # 先取面板，确保 _panels_gen 已是本次数据代次，否则缓存键会慢一拍、
        # 每个新键第一次必然落空。
        panels = self.panels()
        key = (tuple(symbols), start, end, int(window), self._panels_gen, self.store.generation)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        bars, _ = self.store.snapshot()
        payload = analytics.build_payload(
            bars, symbols=symbols, start=start, end=end, window=window, panels=panels
        )
        with self._lock:
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[key] = payload
        return payload

    # -- 刷新

    def refresh_once(self) -> dict:
        """抓取全部标的并落盘；单标的失败不影响其余。"""
        if not self.refreshing:
            pass
        summary: dict[str, str] = {}
        self.refreshing = True
        try:
            for symbol in config.ORDER:
                try:
                    rows = sources.fetch_symbol(symbol, archive=True)
                    merged = self.store.upsert(symbol, rows)
                    summary[symbol] = f"ok ({len(rows)} fetched -> {merged} total)"
                except Exception as exc:  # noqa: BLE001
                    log.error("%s 刷新失败: %s", symbol, exc)
                    self.store.mark_failed(symbol, str(exc))
                    summary[symbol] = f"error: {exc}"
            self.store.persist_meta()
            self.last_refresh = datetime.now(TZ).isoformat(timespec="seconds")
            errors = [k for k, v in summary.items() if v.startswith("error")]
            self.last_error = "; ".join(f"{k}: {summary[k]}" for k in errors) or None
            self.last_summary = summary
            with self._lock:
                self._cache.clear()
                self._panels = None
            return summary
        finally:
            self.refreshing = False

    def refresh_async(self) -> bool:
        if self.refreshing:
            return False

        def _run() -> None:
            try:
                self.refresh_once()
            except Exception:  # noqa: BLE001
                log.exception("后台刷新异常")

        threading.Thread(target=_run, name="refresh-once", daemon=True).start()
        return True

    def _in_market_window(self, now: datetime) -> bool:
        if now.weekday() >= 5:
            return False
        hm = (now.hour, now.minute)
        return config.MARKET_OPEN <= hm <= config.MARKET_CLOSE

    def start_refresh_loop(self) -> None:
        if not self.refresh_enabled:
            log.info("自动刷新已禁用")
            return

        def _loop() -> None:
            try:
                self.refresh_once()
            except Exception:  # noqa: BLE001
                log.exception("首次刷新异常")
            last = time.time()
            while not self._stop.wait(60):
                now = datetime.now(TZ)
                if not self._in_market_window(now):
                    continue
                if time.time() - last >= config.REFRESH_INTERVAL_SEC:
                    try:
                        self.refresh_once()
                    except Exception:  # noqa: BLE001
                        log.exception("定时刷新异常")
                    last = time.time()

        threading.Thread(target=_loop, name="refresh-loop", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------------ 请求处理


class Handler(BaseHTTPRequestHandler):
    server_version = "GoldWorkbench/1.0"
    protocol_version = "HTTP/1.1"

    state: AppState  # 由 make_server 注入

    # -- 日志

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        log.info("%s %s", self.address_string(), fmt % args)

    # -- 响应工具

    def _accepts_gzip(self) -> bool:
        return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

    def _send_bytes(self, body: bytes, ctype: str, status: int = 200, cache: str = "no-cache") -> None:
        headers = [
            ("Content-Type", ctype),
            ("Cache-Control", cache),
            ("X-Content-Type-Options", "nosniff"),
        ]
        if len(body) > 1024 and self._accepts_gzip():
            body = gzip.compress(body, 6)
            headers.append(("Content-Encoding", "gzip"))
            headers.append(("Vary", "Accept-Encoding"))
        headers.append(("Content-Length", str(len(body))))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        try:
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):
            log.exception("JSON 序列化失败")
            self._json({"ok": False, "error": "内部序列化错误"}, 500)
            return
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _error(self, status: int, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    # -- 路由

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route("GET")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("处理 GET 失败: %s", self.path)
            try:
                self._error(500, "服务内部错误")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route("POST")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("处理 POST 失败: %s", self.path)
            try:
                self._error(500, "服务内部错误")
            except Exception:  # noqa: BLE001
                pass

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = posixpath.normpath(unquote(parsed.path))
        query = parse_qs(parsed.query)

        if path == "/api/meta":
            return self._api_meta()
        if path == "/api/bars":
            return self._api_bars(query)
        if path == "/api/analysis":
            return self._api_analysis(query)
        if path == "/api/health":
            return self._api_health()
        if path == "/api/refresh":
            if method != "POST":
                return self._error(405, "请使用 POST")
            return self._api_refresh()

        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        return self._error(404, "未找到")

    # -- API

    def _api_meta(self) -> None:
        payload = self.state.store.meta_payload()
        payload["ok"] = True
        payload["refreshing"] = self.state.refreshing
        payload["last_refresh"] = self.state.last_refresh
        payload["last_error"] = self.state.last_error
        payload["server_time"] = datetime.now(TZ).isoformat(timespec="seconds")
        self._json(payload)

    def _api_health(self) -> None:
        self._json(
            {
                "ok": True,
                "uptime_sec": round(time.time() - self.state.started_at, 1),
                "refreshing": self.state.refreshing,
                "last_refresh": self.state.last_refresh,
                "last_error": self.state.last_error,
                "generation": self.state.store.generation,
                "refresh_enabled": self.state.refresh_enabled,
            }
        )

    def _api_refresh(self) -> None:
        if not self.state.refresh_enabled:
            return self._error(409, "自动刷新已被禁用（--no-refresh 启动）")
        started = self.state.refresh_async()
        self._json({"ok": True, "started": started, "refreshing": True}, 202 if started else 200)

    def _symbols_param(self, query: dict) -> list[str]:
        raw = (query.get("symbols") or [""])[0].strip()
        if not raw or raw == "all":
            return list(config.INDICES)
        requested = [s.strip() for s in raw.split(",") if s.strip()]
        bad = [s for s in requested if s not in config.INSTRUMENTS]
        if bad:
            raise ValueError(f"未知标的: {', '.join(bad)}")
        return [s for s in requested if s != config.GOLD] or list(config.INDICES)

    @staticmethod
    def _date_param(query: dict, key: str) -> str | None:
        raw = (query.get(key) or [""])[0].strip()
        if not raw or raw == "all":
            return None
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"{key} 需为 YYYY-MM-DD 格式") from exc
        return raw

    def _api_analysis(self, query: dict) -> None:
        try:
            symbols = self._symbols_param(query)
            start = self._date_param(query, "start")
            end = self._date_param(query, "end")
        except ValueError as exc:
            return self._error(400, str(exc))

        try:
            window = int((query.get("window") or [config.DEFAULT_WINDOW])[0])
        except ValueError:
            return self._error(400, "window 需为整数")
        if window not in config.ROLLING_WINDOWS:
            return self._error(400, f"window 仅支持 {config.ROLLING_WINDOWS}")

        # 未指定区间时默认最近 2 年
        if start is None and not (query.get("start") or [""])[0]:
            bars, _ = self.state.store.snapshot()
            last = None
            for rows in bars.values():
                if rows:
                    d = rows[-1]["date"]
                    last = d if last is None or d > last else last
            if last:
                try:
                    start = (
                        datetime.strptime(last, "%Y-%m-%d")
                        - timedelta(days=config.DEFAULT_RANGE_DAYS)
                    ).strftime("%Y-%m-%d")
                except ValueError:
                    start = None

        payload = self.state.analysis(symbols, start, end, window)
        payload["refreshing"] = self.state.refreshing
        payload["last_refresh"] = self.state.last_refresh
        self._json(payload)

    def _api_bars(self, query: dict) -> None:
        try:
            symbols = self._symbols_param(query)
            start = self._date_param(query, "start")
            end = self._date_param(query, "end")
        except ValueError as exc:
            return self._error(400, str(exc))

        out = {}
        for s in [config.GOLD] + symbols:
            rows = self.state.store.get(s)
            if start:
                rows = [r for r in rows if r["date"] >= start]
            if end:
                rows = [r for r in rows if r["date"] <= end]
            out[s] = rows
        self._json({"ok": True, "bars": out})

    # -- 静态文件

    def _static(self, rel: str) -> None:
        rel = rel.replace("\\", "/").lstrip("/")
        target = (config.WEB_DIR / rel).resolve()
        root = config.WEB_DIR.resolve()
        if root not in target.parents and target != root:
            return self._error(403, "禁止访问")
        if not target.is_file():
            return self._error(404, "未找到")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
            "application/javascript",
            "application/json",
        ):
            ctype += "; charset=utf-8"
        self._send_bytes(target.read_bytes(), ctype)


# ------------------------------------------------------------------ 启动


def _probe_port(host: str, port: int) -> int | None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return None
    return port


def pick_port(host: str, preferred: int, scan: int = config.PORT_SCAN) -> int:
    for port in range(preferred, preferred + scan):
        if _probe_port(host, port) is not None:
            return port
    raise RuntimeError(f"端口 {preferred}-{preferred + scan - 1} 均被占用")


def make_server(host: str, port: int, state: AppState) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd


def serve(host: str = config.HOST, port: int = config.DEFAULT_PORT, refresh: bool = True) -> None:
    config.ensure_dirs()
    state = AppState(refresh=refresh)

    port = pick_port(host, port)
    if port != config.DEFAULT_PORT:
        log.warning("端口 %d 被占用，改用 %d", config.DEFAULT_PORT, port)

    httpd = make_server(host, port, state)
    state.start_refresh_loop()

    url = f"http://{host}:{port}/"
    log.info("服务已启动: %s", url)
    print(f"\n  上海黄金 × A股指数 数据工作台")
    print(f"  {url}\n  按 Ctrl+C 停止\n", flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止...", flush=True)
    finally:
        state.stop()
        httpd.shutdown()
        httpd.server_close()
