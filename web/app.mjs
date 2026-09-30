import { WINDOW_LABELS, hongKongInputToIso, parsePricesCsv, queryPrices, resolveAsset } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const state = { bars: [], news: [], result: null, priceSource: "" };
const statusText = {
  on_date: "Ŀ������", deferred_back: "ǰһ������", on_time: "׼ʱ",
  deferred: "����˳��", pending: "��δ����", missing: "ȱ������",
};
const showTime = (value) => value ? value.slice(0, 19).replace("T", " ") : "��";
const showPrice = (value) => value === null || value === undefined ? "��" : Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 4 });
const showReturn = (value) => value === null || value === undefined ? "��" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;

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
  byId("matchCount").textContent = `${matches.length} ��ֱ���ἰ��ѡ �� չʾ��� 25 ��`;
  if (!matches.length) {
    addText(list, "div", "��ǰ����������û��ֱ���ἰ���ʲ������¡��Կ��ֶ��������ŷ���ʱ���ѯ�۸�", "empty-news");
    return;
  }
  for (const item of matches.slice(0, 25)) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "news-item";
    addText(button, "span", item.match_type === "title_mention" ? "�����ἰ" : "�����ἰ", `tag ${item.match_type === "body_mention" ? "body" : ""}`);
    addText(button, "strong", item.title);
    addText(button, "small", `${showTime(item.published_at)} �� ${item.source}`);
    button.addEventListener("click", () => {
      byId("publishedAt").value = item.published_at.slice(0, 19);
      message(`��ѡ��${item.title}������ȷ�������ʲ���أ��ٲ�ѯ�۸�`, true);
      byId("queryForm").scrollIntoView({ behavior: "smooth", block: "start" });
    });
    list.append(button);
  }
}

function renderResult(result) {
  state.result = result;
  byId("results").hidden = false;
  byId("resultSubtitle").textContent = `${result.ticker} �� ���ŷ��� ${showTime(result.published_at)} ���ʱ�� �� ${state.priceSource}`;
  byId("baselinePrice").textContent = showPrice(result.baseline.price);
  byId("baselineDetail").textContent = `${showTime(result.baseline.observed_at)} ��ʼ�ķ����� �� ��ɺ������ŷ��� ${result.baseline.gap_seconds} �� �� ${result.baseline.status === "stale" ? "�۸�����ѹ�ʱ" : "���ڼ۸�"}`;
  const body = byId("resultRows");
  body.replaceChildren();
  for (const [label, item] of Object.entries(result.windows)) {
    const tr = document.createElement("tr");
    addText(tr, "td", WINDOW_LABELS[label] || label);
    addText(tr, "td", showTime(item.target_at));
    const observed = item.observed_at && item.delay_seconds >= 60
      ? `${showTime(item.observed_at)} (+${Math.round(item.delay_seconds / 60)} ��)`
      : showTime(item.observed_at);
    addText(tr, "td", observed);
    addText(tr, "td", showPrice(item.price));
    const pct = item.excess_return_pct !== undefined && item.excess_return_pct !== null
      ? `${showReturn(item.return_pct)} / ��� ${showReturn(item.excess_return_pct)}` : showReturn(item.return_pct);
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
    byId("newsStatus").textContent = `${manifest.news_articles_total.toLocaleString("zh-CN")} ƪ �� ${manifest.news_matches_total} ��ƥ��`;
    const newsResponse = await fetch("./news-index.json", { cache: "no-store" });
    if (!newsResponse.ok) throw new Error(`HTTP ${newsResponse.status}`);
    const index = await newsResponse.json();
    state.news = index.matches || [];
    renderNews();
    if (manifest.public_prices_available) {
      const priceResponse = await fetch("./price-history.csv", { cache: "no-store" });
      if (!priceResponse.ok) throw new Error(`�������� HTTP ${priceResponse.status}`);
      state.bars = parsePricesCsv(await priceResponse.text(), "GitHub ��������");
      state.priceSource = "GitHub ��������";
      byId("priceStatus").textContent = `${state.bars.length.toLocaleString("zh-CN")} �� K �� �� ��� ${manifest.latest_price_at?.slice(0, 10) || "����δ֪"}`;
    } else byId("priceStatus").textContent = "��δ���� �� ���ϴ� CSV";
  } catch (error) {
    byId("newsStatus").textContent = "�����ݲ�����";
    byId("priceStatus").textContent = "���ϴ����� CSV";
    byId("matchCount").textContent = "������������ʧ��";
    message(`վ����������ʧ�ܣ�${error.message}���Կ�ʹ�ñ����۸� CSV �ֶ���ѯ��`);
  }
}

byId("asset").addEventListener("input", renderNews);
byId("priceFile").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  if (!file) return;
  try {
    state.bars = parsePricesCsv(await file.text(), `�����ļ���${file.name}`);
    state.priceSource = `�����ļ���${file.name}`;
    byId("priceStatus").textContent = `${state.bars.length.toLocaleString("zh-CN")} �� K �� �� �����ļ�`;
    message(`�Ѷ�ȡ ${file.name}���� ${state.bars.length} ���۸� K �ߡ�`, true);
  } catch (error) {
    state.bars = []; state.priceSource = "";
    message(error.message);
  }
});
byId("queryForm").addEventListener("submit", (event) => {
  event.preventDefault();
  try {
    if (!state.bars.length) throw new Error("�����������ݡ����ϴ���ʵ�۸� CSV����ȴ��Ŷӷ����������顣");
    const result = queryPrices(byId("asset").value, hongKongInputToIso(byId("publishedAt").value), state.bars,
      { benchmark: byId("benchmark").checked });
    renderResult(result);
    message("��ѯ��ɡ���˶���������Ժ�������Դ��", true);
  } catch (error) { byId("results").hidden = true; message(error.message); }
});
byId("downloadJson").addEventListener("click", () => {
  if (state.result) download(`${state.result.ticker}-news-price.json`, JSON.stringify(state.result, null, 2), "application/json;charset=utf-8");
});
byId("downloadCsv").addEventListener("click", downloadCsv);
loadSiteData();
