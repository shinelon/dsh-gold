# AGENTS.md

上海黄金（上金所 Au99.99）× A 股指数数据工作台。本地常驻服务：抓取 → JSONL 落盘 → pandas 分析 → 单页 ECharts 看板。

本文件是给后续改动者（人与 agent）的工作契约。**改代码前先读「不变量」和「数据契约」两节**，那里写着几处踩过的坑，破坏它们不会报错，只会让结论悄悄变错。

---

## 1. 快速开始

```bash
python run.py serve            # 默认 127.0.0.1:8770，端口被占用则顺延尝试 8770..8779
python run.py serve --no-refresh --open
python run.py fetch            # 抓一次全部标的并落盘，不启服务
python run.py test -v          # unittest 全量
python -m unittest tests.test_analytics -v      # 单文件
python -m unittest tests.test_store.TestMerge.test_new_wins   # 单用例
```

- 无 build 步骤、无前端打包、无数据库、无 Docker。
- `_shot.png` 是看板截图，可直接对照 UI 理解各面板含义。

### 环境

- Python **3.10+**（代码用了 `X | None` 与 `from __future__ import annotations`；现有 `__pycache__` 为 3.12）。
- 依赖仅三个，但**没有 requirements.txt**，需自行安装：
  ```bash
  pip install pandas numpy tzdata
  ```
- `tzdata` 不是可选项：`sources.py:27`、`store.py:23`、`server.py:25` 都调用 `ZoneInfo("Asia/Shanghai")`，Windows 无系统时区库，缺包会在 import 阶段直接崩。
- Web 端只依赖 `web/vendor/echarts.min.js`（已内置），不要引入 CDN 或前端框架。

---

## 2. 目录与职责

```
run.py              CLI 入口：serve / fetch / test 三个子命令
app/config.py       唯一配置源：标的表、端口、窗口、阈值、颜色、路径
app/sources.py      上游抓取与解析（SGE 黄金 / 新浪指数），含原始快照归档
app/store.py        JSONL 读写：原子写、按 date 合并、meta.json、内存 Store
app/analytics.py    纯计算层：面板、相关、领先滞后、背离、量能、状态判定
app/server.py       标准库 HTTP 服务 + 后台刷新线程 + 内存缓存
web/index.html      单页骨架
web/app.js          全部前端逻辑（IIFE，无模块系统）
web/style.css       深色主题
tests/              unittest，全部离线（fixtures + 合成数据）
data/bars/*.jsonl   行情主存储（一标的一文件）
data/raw/<日期>/    每次成功抓取的原始响应快照
data/meta.json      各标的抓取状态摘要
```

分层是单向的：`analytics` 不 import `store`/`server`，`store` 不 import `analytics`，`server` 向上调用全部。新增分析不要往 `store` 里塞业务逻辑。

---

## 3. 数据契约（改动前必读）

### 3.1 `data/bars/<symbol>.jsonl`

每行一个紧凑 JSON 对象，字段固定为 `store.RECORD_FIELDS`，**按 `date` 升序、`date` 唯一**：

```json
{"symbol":"AU9999","date":"2016-12-19","open":262.45,"high":263.5,"low":262.02,"close":262.76,"volume":null,"source":"sge","fetched_at":"2026-10-29T17:51:01+08:00"}
```

- `date` 一律 `YYYY-MM-DD` 字符串（不是时间戳），比较与排序都按字符串做。
- `volume`：指数为股数（`float`），黄金现货恒为 `null`（SGE 日线不提供成交量）——所以**任何量能指标都必须能容忍 `volume is None`**。
- 多余字段在写入时被 `_dumps` 丢弃；不要在记录里塞临时字段指望持久化。
- 写入必须走 `store.write_atomic()`（`.tmp` + `fsync` + `os.replace`）。直接 `open(...,"w")` 会让进程被杀时留下半个文件。
- 读取必须走 `store.load()`（坏行跳过并计数告警）。**不要假设文件一定干净。**

### 3.2 `data/meta.json`

`{generated_at, instruments:{symbol:{symbol,name,rows,date_min,date_max,last_close,source,last_fetch,status,error}}}`。它只是抓取状态摘要，不是行情数据源；`/api/meta` 会在其之上补齐 `unit/kind/color/short/stale/age_seconds`。

### 3.3 上游接口与已固化的坑

| 源 | 接口 | 坑 |
|---|---|---|
| SGE | `https://www.sge.com.cn/graph/Dailyhq?instid=Au99.99` | 列序是 **`[日期, 开, 收, 低, 高]`**，不是 OHLC；存在高低价夹不住开收价的脏行；有无成交日的全 0 行 |
| 新浪 | `money.finance.sina.com.cn/.../CN_MarketData.getKLineData` | 数值全是**字符串**；可能返回 `null`；`datalen` 上限 5000 |

- 所有记录统一经 `sources._norm_record()` 归一：校验日期、丢弃全 0 行、拒绝非正价、`high=max(o,c,l,h)` / `low=min(...)` 兜底。新增数据源请复用这个函数，不要自己拼 dict。
- 两端都需要 `Referer` 与浏览器 UA，否则被拒。
- 增量拉取实际是「每次重拉近 5000 根 + 按 date 覆盖合并」，`config.OVERLAP_DAYS` 目前**未被使用**（见第 7 节）。

### 3.4 `/api/analysis` 响应结构

`{ok, asof, range{start,end}, window, axis[], symbols[], series, cards, corr, rolling, leadlag, divergence, conditional, volume, regime, alerts, refreshing, last_refresh}`

- `axis` 是「黄金 ∪ 选中指数」的日期并集；`series[sym].close` 与 `axis` **等长且按位对齐**，缺失处为 `null`（前端靠 `null` 断线）。
- 成交量统一除以 `1e8` 转「亿股」后再下发，单位由 `volume_unit` 声明。
- 所有浮点都经过 `_num`/`_arr` 处理，**NaN / inf 必须是 `null`**，因为它最终由 `json.dumps(..., allow_nan=False)` 序列化（`server.py:202`）。

---

## 4. 不变量（破坏后不会报错，但结论会错）

1. **先在完整历史上算，再按请求区间切片。** 均线、滚动相关、状态判定都需要 `start` 之前的历史，否则视图开头凭空出现一段 `NaN`。切片只允许发生在 `analytics._slice()`。
2. **配对分析用日期交集，收益绝不前向填充。** `_pair()` → `pct_change(fill_method=None)` → `dropna()`。停牌日当 0 收益会污染相关系数。
3. **不允许未来函数。** 任何滚动/状态量在 `t` 时刻只能依赖 `<= t` 的数据；`tests/test_analytics.py::TestNoLookahead` 用「截断历史后末值必须不变」来守这条线。新增指标请补同类测试。
4. **原子写 + 幂等合并。** `merge()` 新记录覆盖旧记录、按 date 排序去重，重复 `upsert` 同一批数据行数不变。
5. **时区固定 Asia/Shanghai。** 所有 `now()` 走 `datetime.now(TZ)`，日期键用 `%Y-%m-%d`。不要用 `date.today()` 或裸 `datetime.now()`。
6. **抓取失败是常态，必须逐标的隔离。** 单标的异常只 `mark_failed` 并继续，不能让整轮刷新中断，也不能把旧数据覆盖成空。
7. **前端不发散。** 新增图表 = 后端加一个 payload 字段 + `app.js` 加一个 `renderXxx` + `index.html` 加一个 `.panel`；配色从 `config.INSTRUMENTS[symbol].color` 来。

---

## 5. 测试

```bash
python run.py test -v
```

- 全部离线：`tests/fixtures/*.json` 是上游真实响应样本，其余用 `numpy` 合成价格序列。**新增测试同样不许触网**。
- 组织方式：`test_sources` 解析与容错、`test_store` 合并/原子写/坏行、`test_analytics` 计算正确性与无未来函数、`test_api` 进程内起真实 HTTP 服务（端口 0）测路由与参数校验。
- 需要改路径的测试用「临时改 `config.BARS_DIR` / `config.RAW_DIR`，`tearDown` 再改回」的模式（见 `test_store.py:68`、`test_sources.py:105`）。因为模块里是 `config.XXX` 属性访问而非 `from config import`，这种 monkeypatch 才生效——**新代码请保持 `config.` 前缀访问，不要 `from .config import BARS_DIR`**。
- `test_api.py` 里 `synth_bars()` 生成 300 个交易日 × 全部标的，是加「端到端」断言时最省事的底座。

---

## 6. 代码风格

- 注释、docstring、日志、UI 文案一律**中文**；标识符英文。docstring 写「为什么」而不是「做了什么」——现有文件头部的设计说明就是范例，新增模块请照此写。
- 4 空格缩进、双引号、行宽约 88–100，`from __future__ import annotations` 开头。
- 只在真正需要兜底的地方 `except Exception` 并注释原因（现有代码都写了 `# noqa: BLE001` + 理由）。
- 日志用 `logging.getLogger(__name__)`，`server`/`run` 负责 `basicConfig`；库代码不许自己配 handler。
- 前端 ES5+ 风格的 IIFE，`"use strict"`，不引第三方库，不写构建产物。
- 用户可见数字统一走 `fmtNum` / `pct`，缺失显示 `—` 而不是 `0`。

---

## 7. 已知问题与技术债（评审结论，按优先级）

**P1 · 会造成误判或异常**

1. `run.py:40` `--open` 打开的是 `args.port`，但 `server.pick_port()` 在端口被占用时会顺延，实际端口可能不同 → 打开空页面。修法：让 `serve()` 返回真实端口（或用回调）后再 `webbrowser.open`。
2. `server.py:90-91` `if not self.refreshing: pass` 是死代码，说明这里原本打算做重入保护但没有写。`refresh_once()` 目前**无重入保护**：后台 `refresh-loop` 与 `/api/refresh` 触发的线程可能同时抓取，导致重复写盘与 `generation` 抖动。应在 `refresh_once` 入口用锁做「已在跑就返回」。
3. `server.py:116-127` `refresh_async()` 是 check-then-act（先查 `refreshing` 再起线程，标志在线程内才置位），两次快速点击会并发。
4. `config.py:111` + `server.py:129-133`：`STALE_AFTER_SEC=3h`，但自动刷新只在工作日 9:00–16:30 的窗口内进行。**每个交易日 19:30 之后、以及整个周末，`stale` 必为真**，前端会弹「部分数据可能已过期」。这是误报，不是数据真的坏了。要么把 `stale` 判定改成「按交易日历对齐最近收盘」，要么放宽阈值。

**P2 · 健壮性 / 一致性**

5. `server.py:292-300` `_symbols_param()` 不去重。`?symbols=sh000300,sh000300` 会让相关矩阵拿到重复标签（`mat.loc[a,b]` 退化成 DataFrame，被 `_num` 的 `except TypeError` 吞成 `null`），同时 `alerts`、`rolling` 重复计算一遍。加一句去重即可，别依赖下游容错。
6. `server.py:345-348` 拿到的是缓存里的**同一个 dict 对象**，随后 `payload["refreshing"]=...` 属于原地修改缓存项，且在锁外。建议返回 `dict(payload)` 副本。
7. `sources.py:64,75` 请求头声明 `gzip, deflate`，但只解 gzip；若上游真返回 deflate，会解出乱码并被 `errors="replace"` 静默吞掉。要么去掉 deflate，要么补 `zlib.decompress(raw, -zlib.MAX_WBITS)`。
8. `store.parse_line()` 只校验 `symbol`/`date` 存在，不校验日期格式与数值类型；半损坏的行会一路进到 `pd.to_datetime` 才炸。
9. `sources.py:72` 每次重试都 `ssl.create_default_context()`，握手参数重建；提到模块级常量即可（同时便于统一 CA/超时策略）。

**P3 · 死代码与文档漂移**

10. 计算了但没人用：`analytics.conditional_stats()` 的结果 `conditional` 进了 payload，**前端从未渲染**（`app.js` 无相关代码）。要么补 UI（「指数下跌日黄金表现」是这套分析里最有说服力的避险证据），要么从 payload 移除。
11. 从未被引用的配置/函数：`config.OVERLAP_DAYS`、`config.python_env_note()`、`sources.fetch_all()`（`run.py` 与 `server.py` 各自手写了循环）。删除或接上，别留着误导。
12. `config.py:129` 注释写「前端也用这套」，但 `web/app.js:6-7` 硬编码了另一套 `STATE_COLOR`/`STATE_LABEL`，且颜色已漂移（range：`#8A8F98` vs `#5c6674`）。同理 `/api/meta` 里的 `state_colors`/`state_labels`/`windows`/`default_window`/`max_lag` 前端一个都没用。要么前端改读 `meta`，要么删掉 payload 字段。
13. `tests/test_api.py:52` `cls.state.instruments_meta = {}` 写错了对象（应为 `cls.state.store.instruments_meta`），`AppState` 上根本没有这个属性；测试实际依赖了磁盘上真实的 `data/meta.json`，隔离性打折。`test_analytics.py:185` 的 `valid` 变量算了但没用。
14. 仓库卫生：`.gitignore`/`.gitattributes` 已补，`data/raw/`（只写不读、逐日增长的上游快照）不入库；仍缺 `README.md` 与 `requirements.txt`。注意 `data/bars/*.jsonl` **已入库**，而每次 `fetch` 会整文件重写这 8 个文件，所以提交数据要挑时间点，否则历史迅速膨胀。
15. `store.write_meta()` 原先漏写 `newline="\n"`，导致 `data/meta.json` 在 Windows 下是 CRLF、而 `*.jsonl` 是 LF，每次刷新都产生整文件 diff。已修（`store.py:158`），若再新增落盘代码记得同样显式指定换行。

---

## 8. 常见任务 recipe

- **加一个指数**：只改 `config.INSTRUMENTS`（补 `name/short/color/unit`）+ 加到 `ORDER` 顺序里。抓取、存储、分析、卡片、热力图、状态轴全部自动生效；前端 chips 也是从 `/api/meta` 生成的。无需动其他文件。
- **加一个指标**：在 `analytics.py` 写纯函数 → 补 `tests/test_analytics.py`（含无未来函数）→ 在 `build_payload` 里按「完整历史算、`_slice` 切」的模式接入 → `app.js` 加 `renderXxx` 并在 `render()` 里调用 → `index.html` 加面板。
- **改阈值/窗口**：全部集中在 `config.py` 的「分析」段，并同步 `web/index.html` 里 `windowSelect` 的硬编码选项（两处目前是手抄的）。
- **换数据源**：实现 `parse_xxx(text) -> list[dict]`（必须复用 `_norm_record`），在 `fetch_symbol()` 里按 `symbol` 分发，并把 `source` 字段写成新值；记得补 fixture 与解析测试。

## 9. 不要做的事

- 不要把 `data/bars/*.jsonl` 改成 SQLite/Parquet —— 纯文本可 diff、可手工修复是刻意选择。
- 不要为了「性能」把完整历史切片放到分析之前。
- 不要在 `analytics` 里引入 `store`/`config.INSTRUMENTS` 之外的耦合，它必须能用纯 DataFrame 单测。
- 不要把 `serve` 绑到 `0.0.0.0`：`/api/refresh` 无鉴权，服务默认只监听回环地址是有意的。
- 不要给指数成交量做「亿元」换算——历史成交额不可得，只有股数。
- 不要把本项目输出当成投资建议；页脚已有免责声明，保留它。
