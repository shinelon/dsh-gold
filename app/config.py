"""全局配置：标的表、端口、滚动窗口、刷新节奏。

所有路径均相对项目根目录（本文件所在 app/ 的上一级）。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
BARS_DIR = DATA_DIR / "bars"
RAW_DIR = DATA_DIR / "raw"
META_PATH = DATA_DIR / "meta.json"
WEB_DIR = BASE_DIR / "web"

# ---------------------------------------------------------------- 标的

GOLD = "AU9999"

# 展示顺序即此顺序。color 用于前端图例与卡片强调色。
INSTRUMENTS: dict[str, dict] = {
    "AU9999": {
        "name": "上金所 Au99.99",
        "short": "黄金",
        "kind": "gold",
        "source": "sge",
        "source_id": "Au99.99",
        "unit": "元/克",
        "color": "#E0A33E",
    },
    "sh000001": {
        "name": "上证指数",
        "short": "上证",
        "kind": "index",
        "source": "sina",
        "source_id": "sh000001",
        "unit": "点",
        "color": "#4C9AFF",
    },
    "sz399001": {
        "name": "深证成指",
        "short": "深成",
        "kind": "index",
        "source": "sina",
        "source_id": "sz399001",
        "unit": "点",
        "color": "#36C6B4",
    },
    "sh000300": {
        "name": "沪深300",
        "short": "沪深300",
        "kind": "index",
        "source": "sina",
        "source_id": "sh000300",
        "unit": "点",
        "color": "#8B7CF6",
    },
    "sh000905": {
        "name": "中证500",
        "short": "中证500",
        "kind": "index",
        "source": "sina",
        "source_id": "sh000905",
        "unit": "点",
        "color": "#F2637B",
    },
    "sz399006": {
        "name": "创业板指",
        "short": "创业板",
        "kind": "index",
        "source": "sina",
        "source_id": "sz399006",
        "unit": "点",
        "color": "#56C271",
    },
    "sh000688": {
        "name": "科创50",
        "short": "科创50",
        "kind": "index",
        "source": "sina",
        "source_id": "sh000688",
        "unit": "点",
        "color": "#D98CF0",
    },
    "sh000016": {
        "name": "上证50",
        "short": "上证50",
        "kind": "index",
        "source": "sina",
        "source_id": "sh000016",
        "unit": "点",
        "color": "#7FA8C9",
    },
}

ORDER: list[str] = list(INSTRUMENTS)
INDICES: list[str] = [s for s in ORDER if s != GOLD]

# ---------------------------------------------------------------- 服务

HOST = "127.0.0.1"
DEFAULT_PORT = 8770
PORT_SCAN = 10  # 被占用时顺延尝试的端口数

REFRESH_INTERVAL_SEC = 30 * 60  # 常驻刷新间隔
MARKET_OPEN = (9, 0)  # 北京时间，仅此区间内自动刷新
MARKET_CLOSE = (16, 30)
STALE_AFTER_SEC = 3 * 3600  # 超过此时长未成功刷新则标记 data 可能过期

SINA_DATALEN = 5000  # 新浪单次返回上限，实测 5000 可取满
HTTP_TIMEOUT = 20
HTTP_RETRIES = 3

# ---------------------------------------------------------------- 分析

OVERLAP_DAYS = 10  # 增量重拉的回看天数，覆盖源端修订
ROLLING_WINDOWS = [20, 60, 120]
DEFAULT_WINDOW = 60
MAX_LAG = 10  # 领先滞后搜索范围 ±10 个交易日
DIVERGENCE_Z = 2.0  # 背离触发阈值
REGIME_FAST = 20
REGIME_SLOW = 60

DEFAULT_RANGE_DAYS = 730  # 首屏默认最近 2 年

# 状态标签与配色（前端也用这套）
STATE_LABELS = {
    "bull": "多头",
    "bear": "空头",
    "range": "震荡",
    "na": "数据不足",
}
STATE_COLORS = {
    "bull": "#E5484D",
    "bear": "#2FA36B",
    "range": "#8A8F98",
    "na": "#3A3F47",
}


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (DATA_DIR, BARS_DIR, RAW_DIR):
        d.mkdir(parents=True, exist_ok=True)


def bar_path(symbol: str) -> Path:
    return BARS_DIR / f"{symbol}.jsonl"


def python_env_note() -> str:
    return os.environ.get("PYTHONIOENCODING", "")
