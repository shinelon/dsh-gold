"""数据工作台入口。

用法:
    python run.py serve [--port 8770] [--no-refresh] [--open]
    python run.py fetch
    python run.py test [-v]
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
import unittest
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app import config, server, sources, store  # noqa: E402


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_serve(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)

    def on_ready(url: str) -> None:
        """端口可能被顺延，必须用 serve() 回传的真实 URL 打开浏览器。"""
        if not args.open:
            return
        import threading

        def _open() -> None:
            time.sleep(1.2)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    server.serve(
        host=args.host,
        port=args.port,
        refresh=not args.no_refresh,
        on_ready=on_ready,
    )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    _setup_logging(args.log_level)
    config.ensure_dirs()
    st = store.Store()
    st.load_all()
    print("\n抓取中...\n")
    failures = 0
    t0 = time.time()
    for symbol in config.ORDER:
        inst = config.INSTRUMENTS[symbol]
        try:
            rows = sources.fetch_symbol(symbol, archive=not args.no_archive)
            total = st.upsert(symbol, rows)
            info = st.instruments_meta[symbol]
            print(
                f"  {inst['name']:<14} 抓取 {len(rows):>5} 行 -> 合计 {total:>5} 行"
                f"  ({info['date_min']} ~ {info['date_max']})"
            )
        except Exception as exc:  # noqa: BLE001
            failures += 1
            st.mark_failed(symbol, str(exc))
            print(f"  {inst['name']:<14} 失败: {exc}")
    st.persist_meta()
    print(f"\n完成，用时 {time.time() - t0:.1f}s，失败 {failures} 个\n")
    return 1 if failures else 0


def cmd_test(args: argparse.Namespace) -> int:
    _setup_logging("WARNING")
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(BASE_DIR / "tests"), top_level_dir=str(BASE_DIR)
    )
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py", description="上海黄金 × A股指数 数据工作台"
    )
    parser.add_argument("--log-level", default="INFO", help="日志级别 (DEBUG/INFO/WARNING)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动常驻本地服务")
    p_serve.add_argument("--host", default=config.HOST)
    p_serve.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    p_serve.add_argument("--no-refresh", action="store_true", help="只提供已有数据，不自动抓取")
    p_serve.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    p_serve.set_defaults(func=cmd_serve)

    p_fetch = sub.add_parser("fetch", help="抓取一次数据并落盘")
    p_fetch.add_argument("--no-archive", action="store_true", help="不保存原始响应快照")
    p_fetch.set_defaults(func=cmd_fetch)

    p_test = sub.add_parser("test", help="运行测试")
    p_test.add_argument("-v", "--verbose", action="store_true")
    p_test.set_defaults(func=cmd_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
