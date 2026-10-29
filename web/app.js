/* 上海黄金 × A股指数 数据工作台 —— 前端逻辑 */
(function () {
  "use strict";

  const GOLD = "AU9999";
  const STATE_COLOR = { bull: "#e5484d", bear: "#2fa36b", range: "#5c6674", na: "#242a33" };
  const STATE_LABEL = { bull: "多头", bear: "空头", range: "震荡", na: "数据不足" };

  const AXIS = {
    axisLine: { lineStyle: { color: "#2a3039" } },
    axisTick: { show: false },
    axisLabel: { color: "#8b93a1", fontSize: 10.5 },
    splitLine: { lineStyle: { color: "#1c222b" } },
  };

  const TOOLTIP = {
    backgroundColor: "rgba(20,25,34,.96)",
    borderColor: "#2a3039",
    borderWidth: 1,
    padding: [8, 11],
    textStyle: { color: "#e6e9ef", fontSize: 11.5 },
    extraCssText: "border-radius:8px;box-shadow:0 8px 28px rgba(0,0,0,.5);",
  };

  const app = {
    meta: null,
    data: null,
    symbols: [],
    primary: "sh000300",
    window: 60,
    range: "2y",
    charts: {},
    lastGeneration: null,
    busy: false,
    timer: null,
  };

  const $ = (id) => document.getElementById(id);

  /* ---------------------------------------------------------------- 工具 */

  function ts(dateStr) {
    return Date.parse(dateStr + "T00:00:00Z");
  }

  function fmtDate(v) {
    const d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  }

  function fmtNum(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const d = digits === undefined ? 2 : digits;
    return Number(v).toLocaleString("zh-CN", {
      minimumFractionDigits: d,
      maximumFractionDigits: d,
    });
  }

  function pct(v, digits) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const d = digits === undefined ? 2 : digits;
    return (v > 0 ? "+" : "") + Number(v).toFixed(d) + "%";
  }

  function signClass(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "flat";
    return v > 0 ? "up" : v < 0 ? "down" : "flat";
  }

  function byName(sym) {
    const inst = (app.meta && app.meta.instruments || []).find((i) => i.symbol === sym);
    return inst ? inst : { name: sym, short: sym, color: "#8b93a1" };
  }

  function el(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }

  function chart(id) {
    if (!app.charts[id]) {
      app.charts[id] = echarts.init(document.getElementById(id), null, { renderer: "canvas" });
    }
    return app.charts[id];
  }

  function setLoading(on) {
    app.busy = on;
    $("loading").classList.toggle("show", on);
    $("refreshBtn").disabled = on;
  }

  async function fetchJSON(url, opts) {
    const res = await fetch(url, opts);
    const text = await res.text();
    let body;
    try {
      body = JSON.parse(text);
    } catch (e) {
      throw new Error("响应不是 JSON: " + text.slice(0, 120));
    }
    if (!res.ok) throw new Error(body && body.error ? body.error : "HTTP " + res.status);
    return body;
  }

  /* ---------------------------------------------------------------- 初始化 */

  async function init() {
    setLoading(true);
    try {
      app.meta = await fetchJSON("/api/meta");
      app.symbols = app.meta.instruments.filter((i) => i.symbol !== GOLD).map((i) => i.symbol);
      buildControls();
      await reload();
      startPolling();
    } catch (err) {
      showBanner("初始化失败：" + err.message);
      $("statusText").textContent = "连接失败";
    } finally {
      setLoading(false);
    }
  }

  function buildControls() {
    // 指数 chips
    const chips = $("symbolChips");
    chips.innerHTML = "";
    app.meta.instruments
      .filter((i) => i.symbol !== GOLD)
      .forEach((inst) => {
        const b = el("button", "chip", inst.short);
        b.style.setProperty("--chip", inst.color);
        b.dataset.symbol = inst.symbol;
        b.classList.toggle("on", app.symbols.includes(inst.symbol));
        b.dataset.on = app.symbols.includes(inst.symbol) ? "1" : "0";
        b.onclick = () => {
          const on = app.symbols.includes(inst.symbol);
          if (on && app.symbols.length === 1) return; // 至少保留一个
          app.symbols = on
            ? app.symbols.filter((s) => s !== inst.symbol)
            : [...app.symbols, inst.symbol];
          syncChips();
          reload();
        };
        chips.appendChild(b);
      });

    // 主看下拉
    const sel = $("primarySelect");
    sel.innerHTML = "";
    app.meta.instruments
      .filter((i) => i.symbol !== GOLD)
      .forEach((inst) => {
        const o = el("option", null, inst.name);
        o.value = inst.symbol;
        sel.appendChild(o);
      });
    sel.value = app.primary;
    sel.onchange = () => {
      app.primary = sel.value;
      if (!app.symbols.includes(app.primary)) {
        app.symbols = [...app.symbols, app.primary];
        syncChips();
      }
      reload();
    };

    // 区间
    $("rangeSeg").querySelectorAll("button").forEach((b) => {
      b.onclick = () => {
        app.range = b.dataset.range;
        $("rangeSeg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
        reload();
      };
    });

    const end = metaEndDate();
    $("customStart").value = shiftMonths(end, -24);
    $("customEnd").value = end;
    $("customStart").onchange = $("customEnd").onchange = () => {
      app.range = "custom";
      $("rangeSeg").querySelectorAll("button").forEach((x) => x.classList.remove("on"));
      reload();
    };

    // 窗口
    $("windowSelect").onchange = (e) => {
      app.window = parseInt(e.target.value, 10);
      reload();
    };

    $("refreshBtn").onclick = () => triggerRefresh();
  }

  function syncChips() {
    $("symbolChips").querySelectorAll(".chip").forEach((b) => {
      const on = app.symbols.includes(b.dataset.symbol);
      b.classList.toggle("on", on);
      b.dataset.on = on ? "1" : "0";
    });
  }

  function metaEndDate() {
    let max = null;
    (app.meta.instruments || []).forEach((i) => {
      if (i.date_max && (!max || i.date_max > max)) max = i.date_max;
    });
    return max || new Date().toISOString().slice(0, 10);
  }

  function shiftMonths(dateStr, months) {
    const d = new Date(dateStr + "T00:00:00Z");
    const day = d.getUTCDate();
    d.setUTCMonth(d.getUTCMonth() + months);
    if (d.getUTCDate() < day) d.setUTCDate(0);
    return d.toISOString().slice(0, 10);
  }

  function computeRange() {
    const end = metaEndDate();
    switch (app.range) {
      case "1y": return { start: shiftMonths(end, -12), end };
      case "2y": return { start: shiftMonths(end, -24), end };
      case "3y": return { start: shiftMonths(end, -36), end };
      case "5y": return { start: shiftMonths(end, -60), end };
      case "all": return { start: "all", end: "all" };
      case "custom": return { start: $("customStart").value, end: $("customEnd").value };
      default: return { start: "all", end: "all" };
    }
  }

  function requestSymbols() {
    const set = new Set(app.symbols);
    set.add(app.primary);
    return Array.from(set);
  }

  /* ---------------------------------------------------------------- 取数 */

  async function reload() {
    if (!app.meta) return;
    const { start, end } = computeRange();
    const params = new URLSearchParams({
      symbols: requestSymbols().join(","),
      window: String(app.window),
    });
    if (start) params.set("start", start);
    if (end) params.set("end", end);

    setLoading(true);
    try {
      const data = await fetchJSON("/api/analysis?" + params.toString());
      if (!data.ok) throw new Error(data.error || "分析失败");
      app.data = data;
      render();
      updateStatus();
    } catch (err) {
      showBanner("加载失败：" + err.message);
    } finally {
      setLoading(false);
    }
  }

  async function triggerRefresh() {
    try {
      await fetchJSON("/api/refresh", { method: "POST" });
      showBanner("已开始抓取最新数据…", "info");
      const t0 = Date.now();
      const poll = setInterval(async () => {
        try {
          const h = await fetchJSON("/api/health");
          if (!h.refreshing || Date.now() - t0 > 120000) {
            clearInterval(poll);
            app.lastGeneration = null;
            await reload();
            const m = await fetchJSON("/api/meta");
            app.meta = m;
            updateStatus();
            showBanner(h.last_error ? "刷新完成，但有错误：" + h.last_error : "");
          }
        } catch (e) {
          clearInterval(poll);
        }
      }, 1500);
    } catch (err) {
      showBanner("触发刷新失败：" + err.message);
    }
  }

  function startPolling() {
    if (app.timer) clearInterval(app.timer);
    app.timer = setInterval(async () => {
      try {
        const h = await fetchJSON("/api/health");
        if (app.lastGeneration !== null && h.generation !== app.lastGeneration) {
          await reload();
        }
        app.lastGeneration = h.generation;
        updateStatus(h);
      } catch (e) {
        $("statusText").textContent = "服务无响应";
      }
    }, 60000);
  }

  function showBanner(text, kind) {
    const b = $("banner");
    b.textContent = text || "";
    b.classList.toggle("show", !!text);
    b.style.borderColor = kind === "info" ? "rgba(76,154,255,.35)" : "";
    b.style.color = kind === "info" ? "#8fbaff" : "";
  }

  function updateStatus(health) {
    const stale = app.meta && app.meta.stale;
    const dot = $("dot");
    dot.className = "dot" + (app.busy ? " busy" : stale ? " stale" : "");
    const asof = app.data ? app.data.range.end : "—";
    const lr = (app.meta && app.meta.last_refresh) || (health && health.last_refresh);
    const t = lr ? lr.slice(11, 19) : "—";
    $("statusText").textContent = `数据截至 ${asof} · 抓取 ${t}`;
    if (stale) {
      const bad = (app.meta.instruments || []).filter((i) => i.stale && i.status !== "ok");
      showBanner(
        "部分数据可能已过期" +
          (bad.length ? "：" + bad.map((i) => `${i.name}(${i.status})`).join("、") : "") +
          "。点击右上角「刷新数据」重试。"
      );
    }
  }

  /* ---------------------------------------------------------------- 渲染 */

  function render() {
    const d = app.data;
    renderCards(d);
    renderPrice(d);
    renderRolling(d);
    renderHeat(d);
    renderLeadLag(d);
    renderDivergence(d);
    renderVolume(d);
    renderRegime(d);
    renderStats(d);
    renderAlerts(d);
  }

  function renderCards(d) {
    const box = $("cards");
    box.innerHTML = "";
    d.cards.forEach((c) => {
      const card = el("div", "card" + (c.kind === "gold" ? " gold" : ""));
      card.style.setProperty("--accent", c.color);
      const stateCls = c.state === "bull" ? "bull" : c.state === "bear" ? "bear" : c.state === "range" ? "range" : "na";
      card.innerHTML = `
        <div class="c-name"><span>${c.name}</span><span class="badge ${stateCls}">${c.state_label}</span></div>
        <div class="c-close">${fmtNum(c.close, 2)}<span class="c-unit">${c.unit}</span></div>
        <div class="c-row">
          <span class="c-chg ${signClass(c.chg_pct)}">${pct(c.chg_pct)}</span>
          <span>${c.date.slice(5)}</span>
        </div>
        <div class="c-row">
          <span>60日相关</span>
          <span style="font-family:var(--mono)">${c.corr60 === null ? "—" : c.corr60.toFixed(3)}</span>
        </div>`;
      box.appendChild(card);
    });
  }

  /* -- 走势 */

  function renderPrice(d) {
    const names = [GOLD, ...d.symbols].filter((s) => d.series[s]);
    const dateIndex = new Map(d.axis.map((x, i) => [x, i]));
    const series = [];
    names.forEach((sym) => {
      const inst = byName(sym);
      const closes = d.series[sym].close;
      let base = null;
      for (let i = 0; i < closes.length; i++) {
        if (closes[i] !== null) { base = closes[i]; break; }
      }
      const data = [];
      for (let i = 0; i < d.axis.length; i++) {
        const v = closes[i];
        if (v === null || base === null) continue;
        data.push([d.axis[i], +((v / base) * 100).toFixed(3)]);
      }
      series.push({
        name: inst.name,
        type: "line",
        showSymbol: false,
        symbol: "none",
        connectNulls: false,
        lineStyle: { width: sym === GOLD ? 2.6 : 1.35, color: inst.color },
        itemStyle: { color: inst.color },
        emphasis: { focus: "series", lineStyle: { width: sym === GOLD ? 3.4 : 2.2 } },
        data,
      });
    });

    chart("chartPrice").setOption({
      animation: false,
      grid: { left: 46, right: 18, top: 34, bottom: 58 },
      legend: {
        top: 2, type: "scroll", itemWidth: 16, itemHeight: 8, itemGap: 12,
        textStyle: { color: "#8b93a1", fontSize: 10.5 },
      },
      tooltip: {
        ...TOOLTIP,
        trigger: "axis",
        axisPointer: { type: "cross", label: { backgroundColor: "#2a3039" } },
        formatter: (params) => {
          if (!params.length) return "";
          const dateStr = fmtDate(params[0].value[0]);
          let html = `<div style="color:#8b93a1;font-size:10.5px;margin-bottom:5px">${dateStr}</div>`;
          params.forEach((p) => {
            const sym = names[p.seriesIndex];
            const idx = dateIndex.get(fmtDate(p.value[0]));
            const raw = idx === undefined ? null : d.series[sym].close[idx];
            const unit = byName(sym).unit;
            html += `<div style="display:flex;justify-content:space-between;gap:16px">
              <span>${p.marker}${p.seriesName}</span>
              <span style="font-family:var(--mono)">${p.value[1].toFixed(1)}
              <span style="color:#5c6472">${raw === null ? "" : "· " + fmtNum(raw, 2) + unit}</span></span></div>`;
          });
          return html;
        },
      },
      xAxis: { type: "time", ...AXIS, splitLine: { show: false } },
      yAxis: {
        type: "value", scale: true, ...AXIS,
        axisLabel: { color: "#8b93a1", fontSize: 10.5, formatter: (v) => v.toFixed(0) },
      },
      dataZoom: [
        { type: "inside", throttle: 40 },
        {
          type: "slider", height: 16, bottom: 10,
          backgroundColor: "#12171e", borderColor: "#232a34",
          fillerColor: "rgba(76,154,255,.12)",
          handleStyle: { color: "#3a4453" },
          textStyle: { color: "#5c6472", fontSize: 10 },
          dataBackground: { lineStyle: { color: "#2a3039" }, areaStyle: { color: "#1a202a" } },
        },
      ],
      series,
    }, true);
    $("priceRange").textContent = `${d.range.start} → ${d.range.end}`;
  }

  /* -- 滚动相关 */

  function renderRolling(d) {
    const series = d.symbols.map((sym, idx) => {
      const inst = byName(sym);
      const r = d.rolling[sym] || { dates: [], values: [] };
      const data = r.dates.map((dt, i) => [dt, r.values[i]]).filter((p) => p[1] !== null);
      return {
        name: inst.short,
        type: "line",
        showSymbol: false,
        symbol: "none",
        lineStyle: { width: 1.5, color: inst.color },
        itemStyle: { color: inst.color },
        emphasis: { focus: "series" },
        data,
        markLine: idx === 0 ? {
          silent: true, symbol: "none",
          lineStyle: { color: "#3a4453", width: 1 },
          label: { show: false },
          data: [{ yAxis: 0 }],
        } : undefined,
      };
    });

    chart("chartRolling").setOption({
      animation: false,
      grid: { left: 44, right: 16, top: 30, bottom: 26 },
      legend: {
        top: 0, type: "scroll", itemWidth: 14, itemHeight: 8, itemGap: 10,
        textStyle: { color: "#8b93a1", fontSize: 10 },
      },
      tooltip: {
        ...TOOLTIP,
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: "#3a4453", type: "dashed" } },
        valueFormatter: (v) => (v === null ? "—" : Number(v).toFixed(3)),
      },
      xAxis: { type: "time", ...AXIS, splitLine: { show: false } },
      yAxis: {
        type: "value", min: -1, max: 1, interval: 0.5, ...AXIS,
        axisLabel: { color: "#8b93a1", fontSize: 10.5, formatter: (v) => v.toFixed(1) },
      },
      series,
    }, true);

    $("rollWindow").textContent = `${d.window} 日`;
  }

  /* -- 相关热力图 */

  function renderHeat(d) {
    const labels = d.corr.labels.map((s) => byName(s).short);
    const cells = [];
    d.corr.matrix.forEach((row, y) => {
      row.forEach((v, x) => {
        if (v !== null) cells.push([x, y, v]);
      });
    });
    chart("chartHeat").setOption({
      animation: false,
      grid: { left: 62, right: 14, top: 8, bottom: 46 },
      tooltip: {
        ...TOOLTIP,
        formatter: (p) =>
          `${labels[p.value[1]]} × ${labels[p.value[0]]}<br/>相关系数 <b>${p.value[2].toFixed(3)}</b>`,
      },
      xAxis: {
        type: "category", data: labels, ...AXIS,
        splitArea: { show: false }, splitLine: { show: false },
        axisLabel: { color: "#8b93a1", fontSize: 9.5, interval: 0, rotate: 45 },
      },
      yAxis: {
        type: "category", data: labels, ...AXIS,
        splitArea: { show: false }, splitLine: { show: false },
        axisLabel: { color: "#8b93a1", fontSize: 9.5 },
      },
      visualMap: {
        min: -1, max: 1, calculable: false, show: false,
        inRange: { color: ["#1f6f4a", "#1a202a", "#e5484d"] },
      },
      series: [{
        type: "heatmap",
        data: cells,
        label: { show: true, fontSize: 9, color: "#dfe3ea", formatter: (p) => p.value[2].toFixed(2) },
        itemStyle: { borderColor: "#151a21", borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: "#e0a33e", borderWidth: 1.5 } },
      }],
    }, true);
  }

  /* -- 领先滞后 */

  function renderLeadLag(d) {
    const ll = d.leadlag[app.primary] || { lags: [], corrs: [] };
    const x = ll.lags.map((k) => (k > 0 ? "+" + k : String(k)));
    const data = ll.corrs.map((c, i) => ({
      value: c,
      itemStyle: {
        color:
          c === null ? "#2a3039"
            : ll.lags[i] === ll.best_lag && ll.verdict !== "none" ? "#e0a33e"
            : "#3a5c8a",
      },
    }));
    const band = ll.band || 0;

    const verdictText = {
      none: "无显著时滞",
      contemporaneous: "同步联动（无领先）",
      index_leads: `指数领先 ${ll.best_lag} 日`,
      gold_leads: `黄金领先 ${Math.abs(ll.best_lag)} 日`,
    }[ll.verdict] || "";

    chart("chartLeadLag").setOption({
      animation: false,
      grid: { left: 46, right: 16, top: 22, bottom: 24 },
      tooltip: {
        ...TOOLTIP,
        formatter: (p) =>
          `滞后 <b>${p.name}</b> 日<br/>相关系数 <b>${p.value === null ? "—" : p.value.toFixed(4)}</b>`,
      },
      xAxis: {
        type: "category", data: x, ...AXIS, splitLine: { show: false },
        axisLabel: { color: "#8b93a1", fontSize: 10, interval: 1 },
        name: "滞后(日)", nameLocation: "middle", nameGap: 26,
        nameTextStyle: { color: "#5c6472", fontSize: 10 },
      },
      yAxis: {
        type: "value", ...AXIS,
        axisLabel: { color: "#8b93a1", fontSize: 10.5, formatter: (v) => v.toFixed(2) },
      },
      series: [{
        type: "bar",
        data,
        barWidth: "62%",
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: "#3a4453", type: "dashed", width: 1 },
          label: { show: false },
          data: [{ yAxis: 0 }, { yAxis: band }, { yAxis: -band }],
        },
        markArea: band ? {
          silent: true,
          itemStyle: { color: "rgba(76,154,255,.06)" },
          data: [[{ yAxis: -band }, { yAxis: band }]],
        } : undefined,
      }],
    }, true);

    $("leadLagSymbol").textContent = byName(app.primary).name;
    $("leadLagHint").textContent = `${verdictText}${band ? ` · 显著带 ±${band.toFixed(3)}` : ""}`;
  }

  /* -- 背离 */

  function renderDivergence(d) {
    const dv = d.divergence[app.primary] || { dates: [], z: [] };
    const data = dv.dates.map((dt, i) => [dt, dv.z[i]]).filter((p) => p[1] !== null);
    const thr = (app.meta && app.meta.divergence_z) || 2;

    chart("chartDivergence").setOption({
      animation: false,
      grid: { left: 46, right: 16, top: 22, bottom: 24 },
      tooltip: {
        ...TOOLTIP,
        trigger: "axis",
        valueFormatter: (v) => (v === null ? "—" : Number(v).toFixed(2)),
      },
      xAxis: { type: "time", ...AXIS, splitLine: { show: false } },
      yAxis: {
        type: "value", ...AXIS,
        axisLabel: { color: "#8b93a1", fontSize: 10.5, formatter: (v) => v.toFixed(1) },
      },
      series: [{
        type: "line",
        showSymbol: false,
        symbol: "none",
        data,
        lineStyle: { width: 1.6, color: "#8b7cf6" },
        itemStyle: { color: "#8b7cf6" },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(139,124,246,.22)" },
            { offset: 1, color: "rgba(139,124,246,0)" },
          ]),
        },
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: "rgba(224,163,62,.55)", type: "dashed", width: 1 },
          label: { color: "#e0a33e", fontSize: 9, position: "insideEndTop" },
          data: [
            { yAxis: thr, label: { formatter: "金强 " + thr } },
            { yAxis: -thr, label: { formatter: "股强 " + thr } },
            { yAxis: 0, lineStyle: { color: "#3a4453", type: "solid" }, label: { show: false } },
          ],
        },
      }],
    }, true);

    const cur = dv.current;
    $("diverSymbol").textContent =
      cur === null ? byName(app.primary).name
        : `${byName(app.primary).name} · z=${cur.toFixed(2)}`;
  }

  /* -- 量能 */

  function renderVolume(d) {
    const v = d.volume[app.primary] || {};
    const dates = v.dates || [];
    const unit = d.volume_unit || "亿股";
    const series = [
      {
        name: "成交量",
        type: "bar",
        yAxisIndex: 0,
        data: dates.map((dt, i) => [dt, v.volume[i]]),
        itemStyle: { color: "rgba(76,154,255,.42)" },
        barMaxWidth: 8,
      },
      {
        name: "量 MA20",
        type: "line", yAxisIndex: 0, showSymbol: false, symbol: "none",
        data: dates.map((dt, i) => [dt, v.ma20[i]]),
        lineStyle: { width: 1.3, color: "#4c9aff" },
        itemStyle: { color: "#4c9aff" },
      },
      {
        name: "量 MA60",
        type: "line", yAxisIndex: 0, showSymbol: false, symbol: "none",
        data: dates.map((dt, i) => [dt, v.ma60[i]]),
        lineStyle: { width: 1.3, color: "#e0a33e" },
        itemStyle: { color: "#e0a33e" },
      },
      {
        name: "量比",
        type: "line", yAxisIndex: 1, showSymbol: false, symbol: "none",
        data: dates.map((dt, i) => [dt, v.ratio[i]]),
        lineStyle: { width: 1.2, color: "#8b7cf6", type: "dotted" },
        itemStyle: { color: "#8b7cf6" },
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: "rgba(139,124,246,.4)", type: "dashed", width: 1 },
          label: { show: false },
          data: [{ yAxis: 1 }],
        },
      },
    ];

    chart("chartVolume").setOption({
      animation: false,
      grid: { left: 58, right: 52, top: 32, bottom: 30 },
      legend: {
        top: 2, itemWidth: 14, itemHeight: 8, itemGap: 12,
        textStyle: { color: "#8b93a1", fontSize: 10.5 },
      },
      tooltip: {
        ...TOOLTIP,
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params) => {
          if (!params.length) return "";
          let html = `<div style="color:#8b93a1;font-size:10.5px;margin-bottom:5px">${fmtDate(params[0].value[0])}</div>`;
          params.forEach((p) => {
            const val = p.value[1];
            const shown = p.seriesName === "量比" ? (val === null ? "—" : val.toFixed(2))
              : (val === null ? "—" : fmtNum(val, 2) + " " + unit);
            html += `<div style="display:flex;justify-content:space-between;gap:16px">
              <span>${p.marker}${p.seriesName}</span>
              <span style="font-family:var(--mono)">${shown}</span></div>`;
          });
          return html;
        },
      },
      xAxis: { type: "time", ...AXIS, splitLine: { show: false } },
      yAxis: [
        {
          type: "value", ...AXIS, name: unit, nameTextStyle: { color: "#5c6472", fontSize: 10 },
          axisLabel: { color: "#8b93a1", fontSize: 10.5, formatter: (x) => fmtNum(x, 0) },
        },
        {
          type: "value", ...AXIS, name: "量比", nameTextStyle: { color: "#5c6472", fontSize: 10 },
          splitLine: { show: false }, min: 0,
          axisLabel: { color: "#8b7cf6", fontSize: 10.5, formatter: (x) => x.toFixed(1) },
        },
      ],
      series,
    }, true);

    $("volumeSymbol").textContent = byName(app.primary).name;
  }

  /* -- 状态时间轴 */

  function renderRegime(d) {
    const syms = [GOLD, ...d.symbols].filter((s) => d.regime[s]);
    const rows = syms.map((s) => byName(s).short);

    const runs = [];
    syms.forEach((sym, rowIdx) => {
      const r = d.regime[sym];
      const states = r.states;
      let start = 0;
      for (let i = 1; i <= states.length; i++) {
        if (i === states.length || states[i] !== states[start]) {
          const st = states[start];
          if (st !== "na" && states.length) {
            runs.push([ts(r.dates[start]), ts(r.dates[i - 1]), rowIdx, st]);
          }
          start = i;
        }
      }
    });

    chart("chartRegime").setOption({
      animation: false,
      grid: { left: 58, right: 16, top: 10, bottom: 26 },
      tooltip: {
        ...TOOLTIP,
        formatter: (p) => {
          const [a, b, row, st] = p.value;
          return `${rows[row]}<br/>${fmtDate(a)} ~ ${fmtDate(b)}<br/>状态 <b style="color:${STATE_COLOR[st]}">${STATE_LABEL[st]}</b>`;
        },
      },
      xAxis: { type: "time", ...AXIS, splitLine: { show: false } },
      yAxis: {
        type: "category", data: rows, ...AXIS,
        splitLine: { show: false },
        axisLabel: { color: "#8b93a1", fontSize: 10.5 },
      },
      series: [{
        type: "custom",
        renderItem: (params, api) => {
          const row = api.value(2);
          const p0 = api.coord([api.value(0), row]);
          const p1 = api.coord([api.value(1), row]);
          const h = api.size([0, 1])[1] * 0.6;
          return {
            type: "rect",
            shape: {
              x: p0[0],
              y: p0[1] - h / 2,
              width: Math.max(p1[0] - p0[0] + 1, 1.2),
              height: h,
            },
            style: { fill: STATE_COLOR[api.value(3)] || STATE_COLOR.na },
          };
        },
        data: runs,
        emphasis: { itemStyle: { opacity: 0.75 } },
      }],
    }, true);

    const cur = d.regime[GOLD];
    $("regimeHint").textContent = cur ? `黄金当前：${STATE_LABEL[cur.current]}` : "";
  }

  /* -- 状态统计 */

  function renderStats(d) {
    const r = d.regime[app.primary];
    $("statsSymbol").textContent = byName(app.primary).name;
    const box = $("statsTable");
    if (!r || !r.stats || !r.stats.length) {
      box.innerHTML = '<div class="empty">数据不足</div>';
      return;
    }
    let html = `<table><thead><tr>
      <th>状态</th><th>天数</th><th>占比</th><th>日均</th><th>年化波动</th><th>胜率</th>
    </tr></thead><tbody>`;
    r.stats.forEach((s) => {
      const color = s.state === "bull" ? "var(--bull)" : s.state === "bear" ? "var(--bear)" : "var(--range)";
      html += `<tr>
        <td style="color:${color};font-weight:600">${s.label}</td>
        <td>${s.count}</td>
        <td>${s.share === null ? "—" : s.share.toFixed(1) + "%"}</td>
        <td style="color:${s.mean_daily > 0 ? "var(--bull)" : s.mean_daily < 0 ? "var(--bear)" : "inherit"}">${s.mean_daily === null ? "—" : (s.mean_daily > 0 ? "+" : "") + s.mean_daily.toFixed(3) + "%"}</td>
        <td>${s.ann_vol === null ? "—" : s.ann_vol.toFixed(1) + "%"}</td>
        <td>${s.win_rate === null ? "—" : s.win_rate.toFixed(0) + "%"}</td>
      </tr>`;
    });
    html += "</tbody></table>";
    box.innerHTML = html;

    const t = r.transitions;
    const tbox = $("transTable");
    if (!t.matrix || !t.matrix.length) {
      tbox.innerHTML = '<div class="empty">数据不足</div>';
      return;
    }
    let th = `<table><thead><tr><th>从 \\ 到</th>${t.labels.map((l) => `<th>${l}</th>`).join("")}</tr></thead><tbody>`;
    t.matrix.forEach((row, i) => {
      th += `<tr><td>${t.labels[i]}</td>` +
        row.map((v) => `<td>${v === null ? "—" : (v * 100).toFixed(1) + "%"}</td>`).join("") +
        "</tr>";
    });
    th += "</tbody></table>";
    tbox.innerHTML = th;
  }

  /* -- 预警 */

  function renderAlerts(d) {
    const box = $("alerts");
    box.innerHTML = "";
    $("alertCount").textContent = d.alerts.length ? `${d.alerts.length} 条` : "";
    if (!d.alerts.length) {
      box.innerHTML = '<div class="empty">当前无触发阈值的事件</div>';
      return;
    }
    d.alerts.forEach((a) => {
      const node = el("div", "alert " + (a.severity || "info"));
      node.innerHTML = `
        <div class="a-body">
          <div class="a-title">${a.label}<span class="a-sym">${a.name}</span></div>
          <div class="a-detail">${a.detail}</div>
        </div>`;
      box.appendChild(node);
    });
  }

  /* ---------------------------------------------------------------- 自适应 */

  function resizeAll() {
    Object.values(app.charts).forEach((c) => {
      try { c.resize(); } catch (e) { /* ignore */ }
    });
  }

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(resizeAll, 120);
  });

  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(resizeAll, 160);
    });
    ro.observe(document.body);
  }

  document.addEventListener("DOMContentLoaded", init);
})();
