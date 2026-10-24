import { WINDOW_LABELS, hongKongInputToIso, relativeHongKongInput } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const state = { news: [], result: null, newsRequest: 0 };
const statusText = {
  on_date: "目标日期", deferred_back: "前一交易日", on_time: "准时",
  deferred: "休市顺延", pending: "尚未到期", missing: "缺少行情",
};
const flagText = { missing_data: "该交易日无行情（停牌或缺数据，无法区分）", zero_volume: "成交量为 0（零成交，或数据源缺成交量）" };
const benchmarkName = {
  "2800.HK": "盈富 2800", "^HSI": "恒指", "3033.HK": "恒科 3033",
  SPY: "标普500 ETF SPY", XLE: "能源板块 ETF XLE", "XIU.TO": "TSX 60 ETF XIU", "^GSPTSE": "多伦多综指",
};
const marketText = { XHKG: "港交所", XNYS: "美股（纽交所日历）", XTSE: "多伦多交易所", XSAU: "沙特交易所", NYMEX: "纽约商品交易所", ICE_EU: "ICE 欧洲" };
const showTime = (value) => value ? value.slice(0, 19).replace("T", " ") : "—";
const showPrice = (value) => value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 4 });
const showDifference = (value) => value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${showPrice(value)}`;
const showReturn = (value) => value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
const timePresetButtons = [...document.querySelectorAll(".time-presets button")];

function clearTimePreset() {
  for (const button of timePresetButtons) button.setAttribute("aria-pressed", "false");
}

function message(text, ok = false) {
  byId("message").textContent = text;
  byId("message").classList.toggle("ok", ok);
}

function addText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function renderNews() {
  const list = byId("newsList");
  list.replaceChildren();
  byId("matchCount").textContent = `${state.news.length} 条直接提及候选`;
  if (!state.news.length) {
    addText(list, "div", "当前新闻索引中没有直接提及该资产的文章。仍可手动输入新闻发布时间查询价格。", "empty-news");
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of state.news) {
    const card = document.createElement("div");
    card.className = "news-card";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "news-item";
    addText(button, "span", item.match_type === "title_mention" ? "标题提及" : "正文提及", `tag ${item.match_type === "body_mention" ? "body" : ""}`);
    addText(button, "strong", item.title);
    addText(button, "small", `${showTime(item.published_at)} · ${item.source}`);
    button.addEventListener("click", () => {
      byId("publishedAt").value = item.published_at.slice(0, 19);
      clearTimePreset();
      message(`已选择「${item.title}」；请确认其与资产相关，再查询价格。`, true);
      byId("queryForm").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    card.append(button);
    let sourceUrl;
    try { sourceUrl = new URL(item.url); } catch { /* No usable source link. */ }
    if (sourceUrl && ["http:", "https:"].includes(sourceUrl.protocol)) {
      const link = document.createElement("a");
      link.className = "news-source-link";
      link.href = sourceUrl.href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "查看原文 ↗";
      card.append(link);
    }
    fragment.append(card);
  }
  list.append(fragment);
}

function setHead(columns) {
  const head = byId("resultHead");
  head.replaceChildren();
  for (const column of columns) addText(head, "th", column);
}

function returnCell(tr, value) {
  addText(tr, "td", showReturn(value), value === null || value === undefined ? "" : value >= 0 ? "positive" : "negative");
}

function statusCell(tr, item) {
  const cell = document.createElement("td");
  addText(cell, "span", statusText[item.status] || item.status, `status ${item.status}`);
  if (item.flag) addText(cell, "small", flagText[item.flag] || item.flag, "comparison-note");
  tr.append(cell);
}

function renderDaily(result) {
  const codes = Object.keys(result.benchmarks || {});
  byId("baselineLabel").textContent = "发布前最后一个收盘价（基准）";
  byId("baselinePrice").textContent = showPrice(result.baseline.price);
  const adj = result.baseline.adj_price === null || result.baseline.adj_price === undefined ? "" : ` · 复权 ${showPrice(result.baseline.adj_price)}`;
  const flag = result.baseline.flag ? ` · ${flagText[result.baseline.flag] || result.baseline.flag}` : "";
  const localNote = result.market && result.market !== "XHKG" ? "（当地时间）" : "";
  byId("baselineDetail").textContent = `${showTime(result.baseline.observed_at)}${localNote} 收盘${adj} · 事件日 t0 = ${result.t0} · 发布时段 ${sessionText[result.session] || result.session}${flag}`;
  setHead(["窗口", "目标交易日收盘", "实际取价", "价格", "涨跌幅（原始）", "涨跌幅（复权）",
    ...codes.map((code) => `超额 vs ${benchmarkName[code] || code}（原始 / 复权）`), "状态"]);
  const body = byId("resultRows");
  body.replaceChildren();
  for (const [label, item] of Object.entries(result.windows)) {
    const tr = document.createElement("tr");
    addText(tr, "td", item.label_zh || WINDOW_LABELS[label] || label);
    addText(tr, "td", showTime(item.target_at));
    addText(tr, "td", showTime(item.observed_at));
    addText(tr, "td", showPrice(item.price));
    returnCell(tr, item.return_pct);
    returnCell(tr, item.adj_return_pct);
    for (const code of codes) {
      const compared = result.benchmarks[code];
      const row = compared.windows?.[label];
      const text = compared.error ? "无对照行情"
        : `${showReturn(row?.excess_return_pct)} / ${showReturn(row?.adj_excess_return_pct)}`;
      addText(tr, "td", text);
    }
    statusCell(tr, item);
    body.append(tr);
  }
  const version = result.data_version ? ` · 数据版本 ${result.data_version.run_id}` : "";
  const local = result.market && result.market !== "XHKG"
    ? ` = ${showTime(result.published_at_local)} 交易所当地时间（${result.exchange_timezone}）` : "";
  const currency = result.currency ? ` · 价格与涨跌幅以 ${result.currency} 计` : "";
  const noBench = codes.length === 0 ? " · 该市场暂无免费对照指数" : "";
  byId("resultSubtitle").textContent = `${result.ticker} · ${marketText[result.market] || result.market || "港交所"} · 新闻发布 ${showTime(result.published_at)} 香港时间${local}${currency}${noBench} · ${result.baseline.source}${version}`;
}

const sessionText = { pre_open: "开盘前", morning: "上午盘", lunch_break: "午休", afternoon: "下午盘", after_close: "收盘后", non_trading_day: "非交易日", regular_session: "交易时段内" };

function renderResult(result) {
  state.result = result;
  byId("results").hidden = false;
  if (result.mode === "daily") {
    renderDaily(result);
    byId("results").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  setHead(["窗口", "目标时间", "实际取价", "价格", "差价", "涨跌幅", "状态"]);
  byId("baselineLabel").textContent = "发布前最后一根已完成分钟线";
  byId("resultSubtitle").textContent = `${result.ticker} · 新闻发布 ${showTime(result.published_at)} 香港时间 · ${result.baseline.source}`;
  byId("baselinePrice").textContent = showPrice(result.baseline.price);
  byId("baselineDetail").textContent = `${showTime(result.baseline.observed_at)} 开始的分钟线 · 完成后至新闻发布 ${result.baseline.gap_seconds} 秒 · ${result.baseline.status === "stale" ? "价格可能已过时" : "近期价格"}`;
  const body = byId("resultRows");
  body.replaceChildren();
  for (const [label, item] of Object.entries(result.windows)) {
    const tr = document.createElement("tr");
    addText(tr, "td", item.label_zh || WINDOW_LABELS[label] || label);
    addText(tr, "td", showTime(item.target_at));
    const observed = item.observed_at && item.delay_seconds >= 60
      ? `${showTime(item.observed_at)} (+${Math.round(item.delay_seconds / 60)} 分)`
      : showTime(item.observed_at);
    addText(tr, "td", observed);
    addText(tr, "td", showPrice(item.price));
    addText(tr, "td", showDifference(item.price_change), item.price_change === null ? "" : item.price_change >= 0 ? "positive" : "negative");
    const cell = document.createElement("td");
    addText(cell, "div", showReturn(item.return_pct),
      item.return_pct === null ? "" : item.return_pct >= 0 ? "positive" : "negative");
    for (const [code, comparison] of Object.entries(result.benchmarks || {})) {
      const labelText = code === "2800.HK" ? "大盘" : "科技";
      const compared = comparison.windows?.[label];
      const detail = compared?.excess_return_pct === null || compared?.excess_return_pct === undefined
        ? `${labelText}对照：无法比较` : `${labelText}超额：${showReturn(compared.excess_return_pct)}`;
      addText(cell, "small", detail, "comparison-note");
    }
    tr.append(cell);
    statusCell(tr, item);
    body.append(tr);
  }
  byId("results").scrollIntoView({ behavior: "smooth", block: "start" });
}

function download(name, content, mime) {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function downloadCsv() {
  const result = state.result;
  if (!result) return;
  const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
  if (result.mode === "daily") {
    const codes = Object.keys(result.benchmarks || {});
    const headers = ["ticker", "published_at", "session", "t0", "baseline_at", "baseline_price", "baseline_adj_price",
      "window", "target_at", "observed_at", "price", "adj_price", "return_pct", "adj_return_pct", "status", "flag", "source",
      ...codes.flatMap((code) => [`${code}_return_pct`, `${code}_excess_pct`, `${code}_adj_excess_pct`]), "data_version"];
    const rows = Object.entries(result.windows).map(([label, item]) => [
      result.ticker, result.published_at, result.session, result.t0, result.baseline.observed_at, result.baseline.price,
      result.baseline.adj_price, label, item.target_at, item.observed_at, item.price, item.adj_price, item.return_pct,
      item.adj_return_pct, item.status, item.flag, item.source,
      ...codes.flatMap((code) => { const row = result.benchmarks[code].windows?.[label]; return [row?.return_pct, row?.excess_return_pct, row?.adj_excess_return_pct]; }),
      result.data_version?.run_id,
    ].map(quote).join(","));
    download(`${result.ticker}-news-price.csv`, `\uFEFF${headers.join(",")}\r\n${rows.join("\r\n")}\r\n`, "text/csv;charset=utf-8");
    return;
  }
  const headers = ["ticker", "published_at", "baseline_at", "baseline_price", "window", "target_at", "observed_at", "price", "price_change", "return_pct", "status", "delay_seconds", "source", "basis", "return_2800_pct", "excess_2800_pct", "return_3033_pct", "excess_3033_pct"];
  const rows = Object.entries(result.windows).map(([label, item]) => {
    const market = result.benchmarks?.["2800.HK"]?.windows?.[label];
    const tech = result.benchmarks?.["3033.HK"]?.windows?.[label];
    return [result.ticker, result.published_at, result.baseline.observed_at, result.baseline.price,
      label, item.target_at, item.observed_at, item.price, item.price_change, item.return_pct, item.status,
      item.delay_seconds, item.source, item.basis, market?.return_pct, market?.excess_return_pct,
      tech?.return_pct, tech?.excess_return_pct].map(quote).join(",");
  });
  download(`${result.ticker}-news-price.csv`, `\uFEFF${headers.join(",")}\r\n${rows.join("\r\n")}\r\n`, "text/csv;charset=utf-8");
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

async function loadNews() {
  const request = ++state.newsRequest;
  const params = new URLSearchParams({ asset: byId("asset").value });
  if (byId("newsStart").value) params.set("start", byId("newsStart").value);
  if (byId("newsEnd").value) params.set("end", byId("newsEnd").value);
  byId("newsStatus").textContent = "读取新闻中…";
  try {
    const data = await fetchJson(`./api/news?${params.toString()}`, { cache: "no-store" });
    if (request !== state.newsRequest) return;
    state.news = data.matches || [];
    byId("newsStatus").textContent = `${state.news.length.toLocaleString("zh-CN")} 条候选${data.stale ? " · 缓存" : ""}`;
    renderNews();
  } catch (error) {
    if (request !== state.newsRequest) return;
    state.news = [];
    byId("newsStatus").textContent = "新闻暂不可用";
    byId("matchCount").textContent = "新闻读取失败";
    byId("newsList").replaceChildren();
    message(`新闻读取失败：${error.message}。仍可手动输入新闻时间查询行情。`);
  }
}

function syncBenchmarkOptions() {
  const asset = (state.assets || []).find((item) => item.ticker === byId("asset").value);
  const hk = !asset || !asset.market || asset.market === "XHKG";
  for (const node of document.querySelectorAll(".hk-only")) node.hidden = !hk;
}

function fillAssets() {
  const select = byId("asset"), selected = select.value, stage = byId("stageFilter").value;
  select.replaceChildren();
  for (const asset of state.assets || []) {
    if (stage && asset.stage !== stage) continue;
    const option = document.createElement("option");
    option.value = asset.ticker;
    const extra = [asset.stage, asset.currency].filter(Boolean).join(" · ");
    const pending = asset.queryable === false ? "（期货：已登记，下一块接入行情）" : "";
    option.textContent = `${asset.name} · ${asset.ticker}${extra ? ` · ${extra}` : ""}${pending}`;
    option.disabled = asset.queryable === false;
    select.append(option);
  }
  const enabled = [...select.options].filter((option) => !option.disabled);
  if (enabled.some((option) => option.value === selected)) select.value = selected;
  else if (enabled.length) select.value = enabled[0].value;
  syncBenchmarkOptions();
}

async function loadCatalog() {
  try {
    const data = await fetchJson("./api/assets", { cache: "no-store" });
    state.assets = data.assets;
    const stages = [...new Set(data.assets.map((asset) => asset.stage).filter(Boolean))];
    const stageSelect = byId("stageFilter");
    stageSelect.hidden = byId("stageLabel").hidden = stages.length === 0;
    const chosenStage = stageSelect.value;
    stageSelect.replaceChildren(new Option("全部环节", ""));
    for (const stage of stages) stageSelect.append(new Option(stage, stage));
    if (stages.includes(chosenStage)) stageSelect.value = chosenStage;
    fillAssets();
    state.mode = data.mode || "minute";
    byId("priceStatus").textContent = state.mode === "daily" ? `${data.price_source}` : `${data.price_source} · 查询时取价`;
    byId("sourceHelp").textContent = state.mode === "daily"
      ? "价格来自本机价格库（每天由 update_prices.py 更新的日线），无需导入 CSV 或登录富途。窗口按标的所在交易所的交易日计算（港股港交所、美股纽交所、加股多伦多、沙特 Tadawul），涨跌幅以当地货币计，同时给出原始与复权涨跌幅和同市场对照（港股 2800.HK/恒指，美股 SPY/XLE，加股 XIU.TO/多伦多综指）的超额收益。"
      : "查询时由内部服务连接富途 OpenD。需要运行服务的电脑已登录富途并具备历史行情权限；网页无需选择价格文件。";
  } catch (error) {
    byId("priceStatus").textContent = "内部查询服务不可用";
    message(`无法连接内部查询服务：${error.message}`);
  }
  await loadNews();
}

byId("asset").addEventListener("change", () => { syncBenchmarkOptions(); loadNews(); });
byId("stageFilter").addEventListener("change", () => { fillAssets(); loadNews(); });
byId("newsFilter").addEventListener("submit", (event) => { event.preventDefault(); loadNews(); });
byId("publishedAt").addEventListener("input", clearTimePreset);
for (const button of timePresetButtons) {
  button.addEventListener("click", () => {
    byId("publishedAt").value = relativeHongKongInput(Number(button.dataset.hours));
    clearTimePreset();
    button.setAttribute("aria-pressed", "true");
    message(`已选 ${button.textContent.trim()}：${showTime(byId("publishedAt").value)}（香港时间）。`, true);
  });
}
byId("queryForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = byId("runQuery");
  button.disabled = true;
  message(state.mode === "daily" ? "正在读取本地价格库并计算…" : "正在从富途 OpenD 获取行情并计算…");
  try {
    const benchmarks = [];
    if (byId("benchmarkMarket").checked) benchmarks.push("2800.HK");
    if (byId("benchmarkTech").checked) benchmarks.push("3033.HK");
    if (state.mode === "daily") benchmarks.push("^HSI");
    const result = await fetchJson("./api/query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset: byId("asset").value,
        published_at: hongKongInputToIso(byId("publishedAt").value), benchmarks }),
    });
    renderResult(result);
    message("查询完成。请核对新闻相关性和行情来源。", true);
  } catch (error) { byId("results").hidden = true; message(error.message); }
  finally { button.disabled = false; }
});
byId("downloadJson").addEventListener("click", () => {
  if (state.result) download(`${state.result.ticker}-news-price.json`, JSON.stringify(state.result, null, 2), "application/json;charset=utf-8");
});
byId("downloadCsv").addEventListener("click", downloadCsv);
loadCatalog();
