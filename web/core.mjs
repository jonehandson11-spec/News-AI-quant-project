const HOUR = 60 * 60 * 1000;
const DAY = 24 * HOUR;
const HK_OFFSET = 8 * HOUR;

export const ASSETS = [
  { ticker: "1211.HK", names: ["比亚迪", "比亚迪股份", "BYD"] },
  { ticker: "0700.HK", names: ["腾讯", "腾讯控股", "Tencent"] },
  { ticker: "9988.HK", names: ["阿里巴巴", "阿里巴巴集团", "Alibaba"] },
  { ticker: "0981.HK", names: ["中芯国际", "SMIC"] },
  { ticker: "0005.HK", names: ["汇丰", "汇丰控股", "HSBC"] },
  { ticker: "2800.HK", names: ["盈富基金"] },
];

export function resolveAsset(name) {
  const key = String(name || "").normalize("NFKC").replace(/\s+/g, "").toLowerCase();
  for (const asset of ASSETS) {
    if (asset.ticker.toLowerCase() === key || asset.names.some((item) => item.toLowerCase() === key)) {
      return asset.ticker;
    }
  }
  const match = /^(?:([0-9]{1,5})\.hk|hk\.([0-9]{1,5}))$/.exec(key);
  if (match) {
    const number = Number(match[1] || match[2]);
    if (number > 0) return `${String(number).padStart(4, "0")}.HK`;
  }
  throw new Error(`未知港股：${name}。请输入已登记名称或港股代码（例如 2318.HK）。`);
}

function csvRows(text) {
  const rows = [];
  let row = [], field = "", quoted = false;
  const input = String(text).replace(/^\uFEFF/, "");
  for (let i = 0; i < input.length; i += 1) {
    const char = input[i];
    if (char === '"') {
      if (quoted && input[i + 1] === '"') { field += '"'; i += 1; }
      else quoted = !quoted;
    } else if (char === "," && !quoted) {
      row.push(field); field = "";
    } else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && input[i + 1] === "\n") i += 1;
      row.push(field);
      if (row.some((value) => value.trim())) rows.push(row);
      row = []; field = "";
    } else field += char;
  }
  if (quoted) throw new Error("价格 CSV 有未闭合的引号。");
  row.push(field);
  if (row.some((value) => value.trim())) rows.push(row);
  return rows;
}

function parseTime(value, interval) {
  const text = String(value).trim();
  const timestamp = interval === "day" && /^\d{4}-\d{2}-\d{2}$/.test(text)
    ? `${text}T16:00:00+08:00` : text;
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(timestamp)) {
    throw new Error(`价格时间缺少时区：${text}`);
  }
  const valueMs = Date.parse(timestamp);
  if (!Number.isFinite(valueMs)) throw new Error(`无法解析价格时间：${text}`);
  return valueMs;
}

export function parsePricesCsv(text, source = "local CSV") {
  const rows = csvRows(text);
  if (!rows.length) throw new Error("价格 CSV 为空。");
  const header = rows.shift().map((name) => name.trim().toLowerCase());
  const required = ["ticker", "interval", "timestamp", "close"];
  if (required.some((name) => !header.includes(name))) {
    throw new Error(`价格 CSV 必须包含 ${required.join(", ")} 四列。`);
  }
  const seen = new Set(), bars = [];
  for (const [index, row] of rows.entries()) {
    const item = Object.fromEntries(header.map((name, column) => [name, row[column] ?? ""]));
    const ticker = resolveAsset(item.ticker);
    const interval = item.interval.trim().toLowerCase();
    if (!["minute", "day"].includes(interval)) throw new Error(`第 ${index + 2} 行：interval 必须为 minute 或 day。`);
    const timestamp = parseTime(item.timestamp, interval);
    const close = Number(item.close);
    if (!Number.isFinite(close) || close <= 0) throw new Error(`第 ${index + 2} 行：收盘价必须为正的有限数字。`);
    const key = `${ticker}|${interval}|${timestamp}`;
    if (seen.has(key)) throw new Error(`第 ${index + 2} 行：重复价格 K 线 ${key}。`);
    seen.add(key);
    bars.push({ ticker, interval, timestamp, close, basis: item.basis?.trim() || "raw", source });
  }
  return bars.sort((left, right) => left.timestamp - right.timestamp);
}

export function hongKongInputToIso(value) {
  const text = String(value).trim();
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(text)) {
    throw new Error("请输入香港时间，格式为 YYYY-MM-DD HH:MM。");
  }
  return `${text.length === 16 ? `${text}:00` : text}+08:00`;
}

export function relativeHongKongInput(hours, now = Date.now()) {
  if (!Number.isFinite(hours) || hours < 0 || !Number.isFinite(now)) {
    throw new Error("快捷时间无效。");
  }
  return new Date(now - hours * HOUR + HK_OFFSET).toISOString().slice(0, 19);
}

function hkDateKey(ms) { return new Date(ms + HK_OFFSET).toISOString().slice(0, 10); }
function isoHk(ms) { return new Date(ms + HK_OFFSET).toISOString().replace("Z", "+08:00"); }

function monthsBefore(ms, count) {
  const date = new Date(ms + HK_OFFSET);
  const year = date.getUTCFullYear(), month = date.getUTCMonth(), day = date.getUTCDate();
  const first = new Date(Date.UTC(year, month - count, 1));
  const lastDay = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate();
  return Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), Math.min(day, lastDay),
    date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds()) - HK_OFFSET;
}

const WINDOWS = [
  ["before_1m", "before", 1], ["before_3m", "before", 3], ["before_6m", "before", 6],
  ["after_1h", "after", HOUR], ["after_3h", "after", 3 * HOUR],
  ["after_12h", "after", 12 * HOUR], ["after_24h", "after", DAY],
  ["after_3d", "after", 3 * DAY], ["after_1w", "after", 7 * DAY],
];

function eventQuery(ticker, eventMs, asOfMs, bars) {
  if (eventMs > asOfMs) throw new Error("新闻时间晚于当前查询截止时间。");
  const minutes = bars.filter((bar) => bar.ticker === ticker && bar.interval === "minute" && bar.timestamp + 60000 <= asOfMs);
  const days = bars.filter((bar) => bar.ticker === ticker && bar.interval === "day");
  const baselineBar = minutes.filter((bar) => bar.timestamp + 60000 <= eventMs).at(-1);
  if (!baselineBar) throw new Error(`${ticker} 在新闻发布前没有已完成的一分钟 K 线。`);
  const gapSeconds = Math.round((eventMs - baselineBar.timestamp - 60000) / 1000);
  const baseline = {
    observed_at: isoHk(baselineBar.timestamp), price: baselineBar.close,
    source: baselineBar.source, basis: baselineBar.basis,
    gap_seconds: gapSeconds, status: gapSeconds < 60 ? "recent" : "stale",
  };
  const output = {};
  for (const [label, direction, offset] of WINDOWS) {
    const target = direction === "before" ? monthsBefore(eventMs, offset) : eventMs + offset;
    const empty = { target_at: isoHk(target), observed_at: null, price: null,
      return_pct: null, status: "missing", delay_seconds: null, source: null, basis: null };
    if (direction === "before") {
      const bar = days.filter((item) => hkDateKey(item.timestamp) <= hkDateKey(target)).at(-1);
      if (!bar) { output[label] = empty; continue; }
      if (bar.basis !== baseline.basis) throw new Error(`${ticker} 混用了不同复权口径的价格。`);
      output[label] = { ...empty, observed_at: isoHk(bar.timestamp), price: bar.close,
        return_pct: (baseline.price - bar.close) / bar.close * 100,
        status: hkDateKey(bar.timestamp) === hkDateKey(target) ? "on_date" : "deferred_back",
        source: bar.source, basis: bar.basis };
    } else if (target > asOfMs) output[label] = { ...empty, status: "pending" };
    else {
      const bar = minutes.find((item) => item.timestamp >= target);
      if (!bar) { output[label] = empty; continue; }
      if (bar.basis !== baseline.basis) throw new Error(`${ticker} 混用了不同复权口径的价格。`);
      const delay = Math.round((bar.timestamp - target) / 1000);
      output[label] = { ...empty, observed_at: isoHk(bar.timestamp), price: bar.close,
        return_pct: (bar.close - baseline.price) / baseline.price * 100,
        status: delay < 60 ? "on_time" : "deferred", delay_seconds: delay,
        source: bar.source, basis: bar.basis };
    }
  }
  return { ticker, published_at: isoHk(eventMs), baseline, windows: output };
}

export function queryPrices(assetName, publishedAt, bars, { asOf = Date.now(), benchmark = false } = {}) {
  const ticker = resolveAsset(assetName);
  if (!/(?:Z|[+-]\d{2}:\d{2})$/i.test(publishedAt)) throw new Error("新闻时间必须包含时区。");
  const eventMs = Date.parse(publishedAt), asOfMs = typeof asOf === "number" ? asOf : Date.parse(asOf);
  if (!Number.isFinite(eventMs) || !Number.isFinite(asOfMs)) throw new Error("新闻时间或截止时间无效。");
  const result = eventQuery(ticker, eventMs, asOfMs, bars);
  if (benchmark && ticker !== "2800.HK") {
    const market = eventQuery("2800.HK", eventMs, asOfMs, bars);
    result.benchmark_ticker = market.ticker;
    result.benchmark_baseline = market.baseline;
    const baselineAligned = Math.abs(Date.parse(result.baseline.observed_at) - Date.parse(market.baseline.observed_at)) <= 60000
      && result.baseline.basis === market.baseline.basis;
    for (const [label, window] of Object.entries(result.windows)) {
      const other = market.windows[label];
      window.benchmark_observed_at = other.observed_at;
      window.benchmark_return_pct = other.return_pct;
      const aligned = baselineAligned && window.observed_at && other.observed_at &&
        (label.startsWith("before_")
          ? window.observed_at.slice(0, 10) === other.observed_at.slice(0, 10)
          : Math.abs(Date.parse(window.observed_at) - Date.parse(other.observed_at)) <= 60000);
      window.excess_return_pct = aligned && window.return_pct !== null && other.return_pct !== null
        ? window.return_pct - other.return_pct : null;
    }
  }
  return result;
}

export const WINDOW_LABELS = {
  before_1m: "新闻前 1 个月", before_3m: "新闻前 1 季度", before_6m: "新闻前半年",
  after_1h: "新闻后 1 小时", after_3h: "新闻后 3 小时", after_12h: "新闻后 12 小时",
  after_24h: "新闻后 24 小时", after_3d: "新闻后 3 天", after_1w: "新闻后 1 周",
};
