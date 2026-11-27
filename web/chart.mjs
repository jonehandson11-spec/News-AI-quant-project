import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  TickMarkType,
  createChart,
  createSeriesMarkers,
} from "./vendor/lightweight-charts/lightweight-charts.standalone.production.mjs";
import { selectedAssetValue } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const RANGE_DAYS = { "1d": 1, "1w": 7, "1m": 30, "3m": 90 };
const PACK_BASE = 1_000_000_000;
const PACK_STEP = 3600;

const UP = "#0f8c6c";
const DOWN = "#bb5d48";
const NEWS = "#c45c26";
const GRID = "#e6eee8";
const TEXT = "#355f59";
const AXIS = "#dbe2da";

const state = {
  interval: "1h",
  range: "1w",
  newsTime: null,
  anchorDate: null,
  symbol: null,
  selection: null,
  request: 0,
  chart: null,
  candle: null,
  volume: null,
  markers: null,
  primitives: [],
  packed: new Map(),
  resize: null,
};

function isoDate(value) {
  if (!value) return "";
  const text = String(value);
  return text.length >= 10 ? text.slice(0, 10) : text;
}

function formatLocal(iso, interval, label) {
  if (!iso) return "—";
  const stamp = String(iso).replace("T", " ");
  if (interval === "1d") return `${isoDate(stamp)} ${label || ""}`.trim();
  return `${stamp.slice(0, 16)} ${label || ""}`.trim();
}


function nearestBar(time) {
  if (state.packed.has(time)) return state.packed.get(time);
  let best = null;
  let bestDist = Infinity;
  for (const [key, bar] of state.packed) {
    if (typeof key !== "number" || typeof time !== "number") continue;
    const dist = Math.abs(key - time);
    if (dist < bestDist) {
      bestDist = dist;
      best = bar;
    }
  }
  return best;
}

function tickLabel(time, tickMarkType) {
  const bar = nearestBar(time) || (typeof time === "string" ? state.packed.get(time) : null);
  if (!bar) return null;
  const iso = String(bar.time);
  const day = isoDate(iso);
  const clock = iso.includes("T") ? iso.replace("T", " ").slice(11, 16) : "";
  if (tickMarkType === TickMarkType.Year) return day.slice(0, 4);
  if (tickMarkType === TickMarkType.Month) return `${Number(day.slice(5, 7))}月`;
  if (tickMarkType === TickMarkType.DayOfMonth) return `${Number(day.slice(8, 10))}日`;
  return clock || `${Number(day.slice(8, 10))}日`;
}

function centerTime() {
  const published = byId("publishedAt")?.value;
  if (state.newsTime) return new Date(state.newsTime);
  if (state.anchorDate) return new Date(`${state.anchorDate}T12:00:00`);
  if (published) return new Date(`${published}:00+08:00`);
  return new Date();
}

function shiftDate(base, days) {
  const copy = new Date(base.getTime());
  copy.setDate(copy.getDate() + days);
  const year = copy.getFullYear();
  const month = String(copy.getMonth() + 1).padStart(2, "0");
  const day = String(copy.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function applyPreset(key) {
  const days = RANGE_DAYS[key];
  if (days == null) return;
  const center = centerTime();
  byId("chartStart").value = shiftDate(center, -days);
  byId("chartEnd").value = shiftDate(center, days);
}

function selectedInterval() {
  return state.interval;
}

function priceFormat(bars) {
  const prices = bars.flatMap((bar) => [bar.open, bar.high, bar.low, bar.close]);
  const max = Math.max(...prices);
  if (max >= 1000) return { type: "price", precision: 1, minMove: 0.1 };
  if (max >= 10) return { type: "price", precision: 2, minMove: 0.01 };
  return { type: "price", precision: 3, minMove: 0.001 };
}

function volumeColor(bar) {
  const up = bar.close >= bar.open;
  return up ? "rgba(15, 140, 108, 0.55)" : "rgba(187, 93, 72, 0.55)";
}

function packBars(bars, interval) {
  state.packed.clear();
  if (interval === "1d") {
    return bars.map((bar) => {
      state.packed.set(bar.time, bar);
      return bar;
    });
  }
  return bars.map((bar, index) => {
    const chartTime = PACK_BASE + index * PACK_STEP;
    state.packed.set(chartTime, bar);
    return { ...bar, chartTime };
  });
}

function chartTimeOf(bar, interval) {
  return interval === "1d" ? bar.time : bar.chartTime;
}

function barForNews(bars, newsIso, interval) {
  if (!newsIso || !bars.length) return null;
  const news = Date.parse(newsIso);
  if (Number.isNaN(news)) return null;
  if (interval === "1d") {
    const day = isoDate(newsIso);
    return bars.find((bar) => bar.time === day) || null;
  }
  for (let index = 0; index < bars.length; index += 1) {
    const start = Date.parse(bars[index].time);
    const end = index + 1 < bars.length
      ? Date.parse(bars[index + 1].time)
      : start + 60 * 60 * 1000;
    if (news >= start && news < end) return bars[index];
  }
  const last = bars[bars.length - 1];
  const start = Date.parse(last.time);
  if (news >= start && news < start + 60 * 60 * 1000) return last;
  return null;
}

class VerticalLinePaneRenderer {
  constructor(x, color) {
    this._x = x;
    this._color = color;
  }

  draw(target) {
    target.useMediaCoordinateSpace((scope) => {
      if (this._x == null) return;
      const ctx = scope.context;
      ctx.save();
      ctx.strokeStyle = this._color;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(this._x + 0.5, 0);
      ctx.lineTo(this._x + 0.5, scope.mediaSize.height);
      ctx.stroke();
      ctx.restore();
    });
  }
}

class VerticalLinePaneView {
  constructor(source) {
    this._source = source;
    this._x = null;
  }

  update() {
    this._x = this._source.chart.timeScale().timeToCoordinate(this._source.time);
  }

  renderer() {
    return new VerticalLinePaneRenderer(this._x, this._source.color);
  }
}

class VerticalLinePrimitive {
  constructor(chart, time, color) {
    this.chart = chart;
    this.time = time;
    this.color = color;
    this._views = [new VerticalLinePaneView(this)];
  }

  updateAllViews() {
    this._views.forEach((view) => view.update());
  }

  paneViews() {
    return this._views;
  }
}

function ensureChart() {
  const container = byId("chartContainer");
  if (state.chart) return;
  const chart = createChart(container, {
    autoSize: true,
    layout: {
      background: { color: "#fbfcfa" },
      textColor: TEXT,
      fontFamily: 'Inter, "Noto Sans SC", "Microsoft YaHei", system-ui, sans-serif',
      attributionLogo: false,
    },
    grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
    crosshair: { mode: CrosshairMode.Normal },
    rightPriceScale: { borderColor: AXIS, scaleMargins: { top: 0.12, bottom: 0.04 } },
    timeScale: {
      borderColor: AXIS,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 4,
      barSpacing: 9,
      minBarSpacing: 3,
      tickMarkFormatter: (time, tickMarkType) => tickLabel(time, tickMarkType),
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    localization: {
      locale: "zh-CN",
      timeFormatter: (time) => {
        const bar = nearestBar(time) || state.packed.get(time);
        if (!bar) return "";
        const interval = selectedInterval();
        const label = byId("chartMessage")?.dataset.label || "";
        return formatLocal(bar.time, interval, label);
      },
    },
  });
  const candle = chart.addSeries(CandlestickSeries, {
    upColor: UP,
    downColor: DOWN,
    borderUpColor: UP,
    borderDownColor: DOWN,
    wickUpColor: UP,
    wickDownColor: DOWN,
  });
  const volume = chart.addSeries(HistogramSeries, {
    priceFormat: { type: "volume" },
    priceScaleId: "volume",
  }, 1);
  const panes = chart.panes();
  if (panes.length > 1 && typeof panes[1].setStretchFactor === "function") {
    panes[1].setStretchFactor(0.28);
  }
  volume.priceScale().applyOptions({
    scaleMargins: { top: 0.15, bottom: 0 },
    borderColor: AXIS,
  });
  state.chart = chart;
  state.candle = candle;
  state.volume = volume;
  state.markers = createSeriesMarkers(candle, []);
  chart.subscribeCrosshairMove((param) => updateTooltip(param));
  state.resize = new ResizeObserver(() => {
    const rect = container.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      chart.resize(rect.width, rect.height, true);
    }
  });
  state.resize.observe(container);
}

function clearNewsGuides() {
  for (const primitive of state.primitives) {
    try { primitive.series.detachPrimitive(primitive.object); } catch { /* already removed */ }
  }
  state.primitives = [];
  state.markers?.setMarkers([]);
}

function attachNewsGuides(bar, interval, newsIso, label) {
  const time = chartTimeOf(bar, interval);
  const text = interval === "1d"
    ? `新闻 ${isoDate(newsIso)}`
    : `新闻 ${String(newsIso).replace("T", " ").slice(0, 16)}`;
  state.markers.setMarkers([{
    time,
    position: "aboveBar",
    color: NEWS,
    shape: "arrowDown",
    text,
  }]);
  for (const series of [state.candle, state.volume]) {
    const primitive = new VerticalLinePrimitive(state.chart, time, NEWS);
    series.attachPrimitive(primitive);
    state.primitives.push({ series, object: primitive });
  }
  const note = byId("chartNewsNote");
  if (note) {
    note.hidden = false;
    note.textContent = `图中虚线为新闻发布时间（${formatLocal(newsIso, "1h", label)}），落在该根K线区间内。`;
  }
}

function updateTooltip(param) {
  const tooltip = byId("chartTooltip");
  if (!tooltip) return;
  if (!param || !param.time || param.point == null || !state.candle) {
    tooltip.hidden = true;
    return;
  }
  const bar = state.packed.get(param.time);
  const price = param.seriesData.get(state.candle);
  if (!bar || !price) {
    tooltip.hidden = true;
    return;
  }
  const label = byId("chartMessage")?.dataset.label || "";
  const change = bar.close - bar.open;
  const pct = bar.open ? (change / bar.open) * 100 : 0;
  const sign = change >= 0 ? "positive" : "negative";
  const signed = `${change >= 0 ? "+" : ""}${change.toFixed(3)}（${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%）`;
  const volume = bar.volume == null ? "—" : Number(bar.volume).toLocaleString("zh-CN");
  tooltip.hidden = false;
  tooltip.innerHTML = `<strong>${formatLocal(bar.time, selectedInterval(), label)}</strong>`
    + `<span>开 ${price.open}　高 ${price.high}　低 ${price.low}　收 ${price.close}</span>`
    + `<span class="${sign}">${signed}</span>`
    + `<span>成交量 ${volume}</span>`;
  const box = byId("chartContainer").getBoundingClientRect();
  const tip = tooltip.getBoundingClientRect();
  let left = param.point.x + 16;
  let top = param.point.y + 16;
  if (left + tip.width + 8 > box.width) left = param.point.x - tip.width - 16;
  if (top + tip.height + 8 > box.height) top = param.point.y - tip.height - 16;
  tooltip.style.left = `${Math.max(8, left)}px`;
  tooltip.style.top = `${Math.max(8, top)}px`;
}

function setPressed(group, attribute, value) {
  for (const button of byId(group).querySelectorAll("button")) {
    button.setAttribute("aria-pressed", String(button.getAttribute(attribute) === value));
  }
}

function message(text, ok = false) {
  const node = byId("chartMessage");
  node.textContent = text || "";
  node.classList.toggle("ok", Boolean(ok) && Boolean(text));
}

async function fetchBars(symbol, interval, start, end) {
  const params = new URLSearchParams({ symbol, interval, start, end });
  const response = await fetch(`./api/bars?${params.toString()}`, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

export async function loadChart({ symbol, newsTime, anchorDate, interval, range } = {}) {
  if (symbol) {
    state.symbol = symbol;
    state.selection = selectedAssetValue();
  }
  const ticker = state.symbol || selectedAssetValue();
  if (!ticker) {
    message("请先选择标的。");
    return;
  }
  if (newsTime !== undefined) {
    state.newsTime = newsTime;
    state.anchorDate = anchorDate || null;
  }
  if (["1h", "1d"].includes(interval)) {
    state.interval = interval;
    setPressed("chartIntervals", "data-interval", interval);
  }
  if (range && RANGE_DAYS[range] != null) {
    state.range = range;
    applyPreset(range);
    setPressed("chartRanges", "data-range", range);
  } else if (!byId("chartStart").value || !byId("chartEnd").value) {
    applyPreset(state.range);
  }
  const start = byId("chartStart").value;
  const end = byId("chartEnd").value;
  if (!start || !end) {
    message("请选择开始和结束日期。");
    return;
  }
  const resolution = selectedInterval();
  const request = ++state.request;
  message("正在读取K线…");
  const note = byId("chartNewsNote");
  if (note) { note.hidden = true; note.textContent = ""; }
  if (state.chart) {
    clearNewsGuides();
    state.packed.clear();
    state.candle.setData([]);
    state.volume.setData([]);
    byId("chartTooltip").hidden = true;
  }
  try {
    const payload = await fetchBars(ticker, resolution, start, end);
    if (request !== state.request) return;
    byId("chartMessage").dataset.label = payload.time_label || "";
    ensureChart();
    const packed = packBars(payload.bars, resolution);
    state.candle.applyOptions({ priceFormat: priceFormat(payload.bars.length ? payload.bars : [{ open: 1, high: 1, low: 1, close: 1 }]) });
    state.candle.setData(packed.map((bar) => ({
      time: chartTimeOf(bar, resolution),
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    })));
    state.volume.setData(packed.map((bar) => ({
      time: chartTimeOf(bar, resolution),
      value: bar.volume ?? 0,
      color: volumeColor(bar),
    })));
    const newsIso = state.newsTime;
    let newsBar = null;
    if (newsIso && packed.length) {
      newsBar = barForNews(packed, newsIso, resolution);
      if (newsBar) attachNewsGuides(newsBar, resolution, newsIso, payload.time_label);
    }
    state.chart.timeScale().fitContent();
    if (payload.missing) {
      message(payload.message || "缺数据");
      return;
    }
    const bits = [
      `${payload.symbol} · ${resolution === "1h" ? "1小时" : "日线"}`,
      `${payload.bar_count} 根K线`,
      payload.message,
    ].filter(Boolean);
    if (newsIso && !newsBar) bits.push("新闻时间落在当前区间的缺口中（非交易时段或缺数据），未画标记。");
    message(bits.join(" · "), true);
  } catch (error) {
    if (request !== state.request) return;
    message(error.message || String(error));
  }
}

export function initChart() {
  setPressed("chartIntervals", "data-interval", state.interval);
  setPressed("chartRanges", "data-range", state.range);
  applyPreset(state.range);
  for (const button of byId("chartIntervals").querySelectorAll("button")) {
    button.addEventListener("click", () => {
      state.interval = button.getAttribute("data-interval");
      setPressed("chartIntervals", "data-interval", state.interval);
      loadChart();
    });
  }
  for (const button of byId("chartRanges").querySelectorAll("button")) {
    button.addEventListener("click", () => {
      state.range = button.getAttribute("data-range");
      applyPreset(state.range);
      setPressed("chartRanges", "data-range", state.range);
      loadChart();
    });
  }
  byId("chartLoad").addEventListener("click", () => {
    for (const button of byId("chartRanges").querySelectorAll("button")) {
      button.setAttribute("aria-pressed", "false");
    }
    const current = selectedAssetValue();
    if (current && current !== state.selection) loadChart({ symbol: current, newsTime: null });
    else loadChart();
  });
  byId("publishedAt")?.addEventListener("input", () => {
    if (!state.newsTime && RANGE_DAYS[state.range] != null
        && document.activeElement?.closest?.("#chartRanges") == null) {
      applyPreset(state.range);
    }
  });
}

export { barForNews, packBars };
