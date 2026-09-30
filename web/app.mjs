import { WINDOW_LABELS, hongKongInputToIso, parsePricesCsv, queryPrices, resolveAsset } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const state = { bars: [], news: [], result: null, priceSource: "" };
const statusText = {
  on_date: "目标日期", deferred_back: "前一交易日", on_time: "准时",
  deferred: "休市顺延", pending: "尚未到期", missing: "缺少行情",
};
const showTime = (value) => value ? value.slice(0, 19).replace("T", " ") : "—";
const showPrice = (value) => value === null || value === undefined ? "—" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 4 });
const showReturn = (value) => value === null || value === undefined ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

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
  let ticker;
  try { ticker = resolveAsset(byId("asset").value); }
  catch { ticker = null; }
  const matches = state.news.filter((item) => item.ticker === ticker)
    .sort((a, b) => b.published_at.localeCompare(a.published_at));
  byId("matchCount").textContent = `${matches.length} 条直接提及候选 · 展示最近 25 条`;
  if (!matches.length) {
    addText(list, "div", "当前新闻索引中没有直接提及该资产的文章。仍可手动输入新闻发布时间查询价格。", "empty-news");
    return;
  }
  for (const item of matches.slice(0, 25)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "news-item";
    addText(button, "span", item.match_type === "title_mention" ? "标题提及" : "正文提及", `tag ${item.match_type === "body_mention" ? "body" : ""}`);
    addText(button, "strong", item.title);
    addText(button, "small", `${showTime(item.published_at)} · ${item.source}`);
    button.addEventListener("click", () => {
      byId("publishedAt").value = item.published_at.slice(0, 19);
      message(`已选择「${item.title}」；请确认其与资产相关，再查询价格。`, true);
      byId("queryForm").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    list.append(button);
  }
}

function renderResult(result) {
  state.result = result;
  byId("results").hidden = false;
  byId("resultSubtitle").textContent = `${result.ticker} · 新闻发布 ${showTime(result.published_at)} 香港时间 · ${state.priceSource}`;
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
    const pct = item.excess_return_pct !== undefined && item.excess_return_pct !== null
      ? `${showReturn(item.return_pct)} / 相对 ${showReturn(item.excess_return_pct)}` : showReturn(item.return_pct);
    addText(tr, "td", pct, item.return_pct === null ? "" : item.return_pct >= 0 ? "positive" : "negative");
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
  const headers = ["ticker", "published_at", "baseline_at", "baseline_price", "window", "target_at", "observed_at", "price", "return_pct", "status", "delay_seconds", "source", "basis", "benchmark_return_pct", "excess_return_pct"];
  const quote = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
  const rows = Object.entries(result.windows).map(([label, item]) => [
    result.ticker, result.published_at, result.baseline.observed_at, result.baseline.price,
    label, item.target_at, item.observed_at, item.price, item.return_pct, item.status,
    item.delay_seconds, item.source, item.basis, item.benchmark_return_pct, item.excess_return_pct,
  ].map(quote).join(","));
  download(`${result.ticker}-news-price.csv`, `\uFEFF${headers.join(",")}\r\n${rows.join("\r\n")}\r\n`, "text/csv;charset=utf-8");
}

async function loadSiteData() {
  try {
    const manifestResponse = await fetch("./site-data.json", { cache: "no-store" });
    if (!manifestResponse.ok) throw new Error(`HTTP ${manifestResponse.status}`);
    const manifest = await manifestResponse.json();
    byId("newsStatus").textContent = `${manifest.news_articles_total.toLocaleString("zh-CN")} 篇 · ${manifest.news_matches_total} 条匹配`;
    const newsResponse = await fetch("./news-index.json", { cache: "no-store" });
    if (!newsResponse.ok) throw new Error(`HTTP ${newsResponse.status}`);
    const index = await newsResponse.json();
    state.news = index.matches || [];
    renderNews();
    if (manifest.public_prices_available) {
      const priceResponse = await fetch("./price-history.csv", { cache: "no-store" });
      if (!priceResponse.ok) throw new Error(`共享行情 HTTP ${priceResponse.status}`);
      state.bars = parsePricesCsv(await priceResponse.text(), "GitHub 共享行情");
      state.priceSource = "GitHub 共享行情";
      byId("priceStatus").textContent = `${state.bars.length.toLocaleString("zh-CN")} 根 K 线 · 最近 ${manifest.latest_price_at?.slice(0, 10) || "日期未知"}`;
    } else byId("priceStatus").textContent = "尚未发布 · 可上传 CSV";
  } catch (error) {
    byId("newsStatus").textContent = "索引暂不可用";
    byId("priceStatus").textContent = "可上传本机 CSV";
    byId("matchCount").textContent = "新闻索引载入失败";
    message(`站点数据载入失败：${error.message}。仍可使用本机价格 CSV 手动查询。`);
  }
}

byId("asset").addEventListener("input", renderNews);
byId("priceFile").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    state.bars = parsePricesCsv(await file.text(), `本机文件：${file.name}`);
    state.priceSource = `本机文件：${file.name}`;
    byId("priceStatus").textContent = `${state.bars.length.toLocaleString("zh-CN")} 根 K 线 · 本机文件`;
    message(`已读取 ${file.name}，共 ${state.bars.length} 根价格 K 线。`, true);
  } catch (error) {
    state.bars = []; state.priceSource = "";
    message(error.message);
  }
});
byId("queryForm").addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    if (!state.bars.length) throw new Error("尚无行情数据。请上传真实价格 CSV，或等待团队发布共享行情。");
    const result = queryPrices(byId("asset").value, hongKongInputToIso(byId("publishedAt").value), state.bars,
      { benchmark: byId("benchmark").checked });
    renderResult(result);
    message("查询完成。请核对新闻相关性和行情来源。", true);
  } catch (error) { byId("results").hidden = true; message(error.message); }
});
byId("downloadJson").addEventListener("click", () => {
  if (state.result) download(`${state.result.ticker}-news-price.json`, JSON.stringify(state.result, null, 2), "application/json;charset=utf-8");
});
byId("downloadCsv").addEventListener("click", downloadCsv);
loadSiteData();
