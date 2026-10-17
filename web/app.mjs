import { WINDOW_LABELS, hongKongInputToIso, relativeHongKongInput } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const state = { news: [], result: null, newsRequest: 0 };
const statusText = {
  on_date: "目标日期", deferred_back: "前一交易日", on_time: "准时",
  deferred: "休市顺延", pending: "尚未到期", missing: "缺少行情",
};
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

function renderResult(result) {
  state.result = result;
  byId("results").hidden = false;
  byId("resultSubtitle").textContent = `${result.ticker} · 新闻发布 ${showTime(result.published_at)} 香港时间 · ${result.baseline.source}`;
  byId("baselinePrice").textContent = showPrice(result.baseline.price);
  byId("baselineDetail").textContent = `${showTime(result.baseline.observed_at)} 开始的分钟线 · 完成后至新闻发布 ${result.baseline.gap_seconds} 秒 · ${result.baseline.status === "stale" ? "价格可能已过时" : "近期价格"}`;
  const body = byId("resultRows");
  body.replaceChildren();
  for (const [label, item] of Object.entries(result.windows)) {
    const tr = document.createElement("tr");
    addText(tr, "td", WINDOW_LABELS[label] || label);
    addText(tr, "td", showTime(item.target_at));
    const observed = item.observed_at && item.delay_seconds >= 60
      ? `${showTime(item.observed_at)} (+${Math.round(item.delay_seconds / 60)} 分)`
      : showTime(item.observed_at);
    addText(tr, "td", observed);
    addText(tr, "td", showPrice(item.price));
    addText(tr, "td", showDifference(item.price_change), item.price_change === null ? "" : item.price_change >= 0 ? "positive" : "negative");
    const returnCell = document.createElement("td");
    addText(returnCell, "div", showReturn(item.return_pct),
      item.return_pct === null ? "" : item.return_pct >= 0 ? "positive" : "negative");
    for (const [code, comparison] of Object.entries(result.benchmarks || {})) {
      const labelText = code === "2800.HK" ? "大盘" : "科技";
      const compared = comparison.windows?.[label];
      const detail = compared?.excess_return_pct === null || compared?.excess_return_pct === undefined
        ? `${labelText}对照：无法比较` : `${labelText}超额：${showReturn(compared.excess_return_pct)}`;
      addText(returnCell, "small", detail, "comparison-note");
    }
    tr.append(returnCell);
    const statusCell = document.createElement("td");
    addText(statusCell, "span", statusText[item.status] || item.status, `status ${item.status}`);
    tr.append(statusCell);
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
  const headers = ["ticker", "published_at", "baseline_at", "baseline_price", "window", "target_at", "observed_at", "price", "price_change", "return_pct", "status", "delay_seconds", "source", "basis", "return_2800_pct", "excess_2800_pct", "return_3033_pct", "excess_3033_pct"];
  const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
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

async function loadCatalog() {
  try {
    const data = await fetchJson("./api/assets", { cache: "no-store" });
    const select = byId("asset"), selected = select.value;
    select.replaceChildren();
    for (const asset of data.assets) {
      const option = document.createElement("option");
      option.value = asset.ticker;
      option.textContent = `${asset.name} · ${asset.ticker}`;
      select.append(option);
    }
    select.value = selected;
    byId("priceStatus").textContent = `${data.price_source} · 查询时取价`;
  } catch (error) {
    byId("priceStatus").textContent = "内部查询服务不可用";
    message(`无法连接内部查询服务：${error.message}`);
  }
  await loadNews();
}

byId("asset").addEventListener("change", loadNews);
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
  message("正在从富途 OpenD 获取行情并计算…");
  try {
    const benchmarks = [];
    if (byId("benchmarkMarket").checked) benchmarks.push("2800.HK");
    if (byId("benchmarkTech").checked) benchmarks.push("3033.HK");
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
