import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  TickMarkType,
  createChart,
} from "./vendor/lightweight-charts/lightweight-charts.standalone.production.mjs";
import { englishText, selectedAssetValue } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const RANGE_DAYS = { "1d": 1, "1w": 7, "1m": 30, "3m": 90 };
const PACK_BASE = 1_000_000_000;
const PACK_STEP = 3600;
const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const UP = "#0f8c6c";
const DOWN = "#bb5d48";
const GRID = "#edf1f6";
const TEXT = "#4e5d6f";
const AXIS = "#dce2eb";

const state = {
  interval: "1h",
  range: "1w",
  anchorDate: null,
  symbol: null,
  selection: null,
  request: 0,
  chart: null,
  candle: null,
  volume: null,
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
  const month = MONTH_NAMES[Number(day.slice(5, 7)) - 1];
  const dateLabel = `${month} ${Number(day.slice(8, 10))}`;
  if (tickMarkType === TickMarkType.Month) return month;
  if (tickMarkType === TickMarkType.DayOfMonth) return dateLabel;
  return clock || dateLabel;
}

function centerTime() {
  if (state.anchorDate) return new Date(`${state.anchorDate}T12:00:00`);
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

function ensureChart() {
  const container = byId("chartContainer");
  if (state.chart) return;
  const chart = createChart(container, {
    autoSize: true,
    layout: {
      background: { color: "#ffffff" },
      textColor: TEXT,
      fontFamily: '"Segoe UI", sans-serif',
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
      locale: "en-US",
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
  chart.subscribeCrosshairMove((param) => updateTooltip(param));
  state.resize = new ResizeObserver(() => {
    const rect = container.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      chart.resize(rect.width, rect.height, true);
    }
  });
  state.resize.observe(container);
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
  const volume = bar.volume == null ? "—" : Number(bar.volume).toLocaleString("en-US");
  tooltip.hidden = false;
  tooltip.innerHTML = `<strong>${formatLocal(bar.time, selectedInterval(), label)}</strong>`
    + `<span>Open ${price.open}  ·  High ${price.high}  ·  Low ${price.low}  ·  Close ${price.close}</span>`
    + `<span class="${sign}">${signed}</span>`
    + `<span>Volume ${volume}</span>`;
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
  if (!response.ok) throw new Error(englishText(body.error_en, englishText(body.error, `Could not load chart data (HTTP ${response.status}).`)));
  return body;
}

export async function loadChart({ symbol, anchorDate, interval, range } = {}) {
  if (symbol) {
    state.symbol = symbol;
    state.selection = selectedAssetValue();
    state.anchorDate = anchorDate || null;
  }
  const ticker = state.symbol || selectedAssetValue();
  if (!ticker) {
    message("Select an instrument first.");
    return;
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
    message("Select a start and end date.");
    return;
  }
  const resolution = selectedInterval();
  const request = ++state.request;
  message("Loading chart…");
  if (state.chart) {
    state.packed.clear();
    state.candle.setData([]);
    state.volume.setData([]);
    byId("chartTooltip").hidden = true;
  }
  try {
    const payload = await fetchBars(ticker, resolution, start, end);
    if (request !== state.request) return;
    byId("chartMessage").dataset.label = englishText(payload.time_label_en, englishText(payload.time_label, payload.exchange_timezone || ""));
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
    state.chart.timeScale().fitContent();
    const note = englishText(payload.message_en, englishText(payload.message));
    if (payload.missing) {
      message(note || "No price bars available.");
      return;
    }
    const bits = [
      `${payload.symbol} · ${resolution === "1h" ? "Hourly" : "Daily"}`,
      `${payload.bar_count} ${payload.bar_count === 1 ? "bar" : "bars"}`,
      note,
    ].filter(Boolean);
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
    if (current && current !== state.selection) loadChart({ symbol: current });
    else loadChart();
  });
}

export { packBars };
