import { assetOptionText, marketStateCsvCells, marketStateCsvHeaders, selectedAssetValue } from "./core.mjs";
import { initChart, loadChart } from "./chart.mjs";

const byId = (id) => document.getElementById(id);
const state = { quote: null };
const MARKET_FIELDS = [
  ["momentum_z", "M01 价格动量"],
  ["realized_volatility", "M02 已实现波动（日，未年化）"],
  ["relative_trading_activity", "M03 相对成交活跃度"],
  ["liquidity_stress_z", "M04 流动性压力"],
  ["benchmark_beta", "M05 基准 Beta"],
  ["benchmark_momentum_z", "M06 基准动量"],
  ["data_coverage", "行情样本覆盖率"],
];
const QUALITY_FIELDS = [
  ["evidence_reliability", "q01_evidence_reliability", "Q01 证据可靠性", "ESTIMATOR_NOT_READY"],
  ["data_coverage", "q02_data_coverage", "Q02 数据覆盖", "REFERENCE_MISSING"],
];
const MISSING_REASONS = {
  INSUFFICIENT_HISTORY: "历史不足", NOT_APPLICABLE: "不适用",
  ESTIMATOR_NOT_READY: "未评估", REFERENCE_MISSING: "未定义参照范围",
  NOT_OBSERVED: "无观测", OUT_OF_COVERAGE: "超出覆盖",
  AMBIGUOUS: "口径不明", SOURCE_UNAVAILABLE: "数据源不可用",
};
const SHOW_PRICE = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const SHOW_RATIO = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });

function addText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  parent.append(node);
  return node;
}

function setMessage(id, text, error = false) {
  const node = byId(id);
  node.textContent = text;
  node.classList.toggle("error", error);
}

function qualityField(block, key, defaultReason) {
  return block.quality?.[key] || { value: null, missing_reason: defaultReason };
}

function addFactorRow(body, label, code, value, details) {
  const row = document.createElement("tr");
  addText(addText(row, "td", label), "code", code);
  addText(row, "td", value);
  addText(row, "td", details.join(" · ") || "—");
  body.append(row);
}

function factorDetails(item) {
  const details = [];
  const periods = [
    [item.window_start, item.window_end, "观察"],
    [item.reference_start || item.reference_first_end_date, item.reference_end || item.reference_last_end_date, "参照"],
    [item.first_return_date, item.last_return_date, "收益样本"],
    [item.first_pair_date, item.last_pair_date, "配对样本"],
    [item.first_expected_date, item.last_expected_date, "预期样本"],
  ];
  for (const [from, to, name] of periods) {
    if (from && to) details.push(`${name} ${from} 至 ${to}`);
  }
  if (item.benchmark) details.push(item.benchmark);
  if (item.value == null) details.push(MISSING_REASONS[item.missing_reason] || item.missing_reason || "无观测");
  for (const key of ["scope_id", "method_version", "source_ref", "note"]) {
    if (typeof item[key] === "string" && item[key]) details.push(item[key]);
  }
  return details;
}

function renderMarketState(block) {
  const failed = Boolean(block.error);
  byId("quoteMarketStateSection").hidden = false;
  setMessage("quoteMarketStateError", block.error || "", failed);
  byId("quoteMarketStateError").hidden = !failed;
  byId("quoteMarketStateTableWrap").hidden = failed;
  byId("downloadQuoteFactors").hidden = failed;
  const basis = { adj: "复权收盘", raw: "原始收盘" }[block.price_basis] || "—";
  byId("quoteMarketStateMeta").textContent = failed ? block.profile_id || ""
    : `截止 ${block.cutoff_date || "—"} · ${basis} · 基准 ${block.benchmark || "—"} · ${block.profile_id || "—"}`;

  const market = byId("quoteMarketStateRows");
  market.replaceChildren();
  for (const [code, label] of MARKET_FIELDS) {
    const item = block.fields?.[code] || {};
    let value = "—";
    if (item.value != null) value = code === "data_coverage" ? SHOW_RATIO.format(item.value) : Number(item.value).toFixed(4);
    addFactorRow(market, label, code, value, factorDetails(item));
  }
  const quality = byId("quoteQualityRows");
  quality.replaceChildren();
  for (const [key, code, label, defaultReason] of QUALITY_FIELDS) {
    const item = qualityField(block, key, defaultReason);
    const details = factorDetails(item);
    if (item.by_variable) {
      const assessed = Object.values(item.by_variable).filter((score) => score.value != null).length;
      if (assessed) details.push(`已评估 ${assessed} 项变量`);
    }
    addFactorRow(quality, label, code, item.value == null ? "—" : Number(item.value).toFixed(4), details);
  }
}

function downloadQuoteFactors() {
  const quote = state.quote?.factors;
  const block = quote?.market_state;
  if (!block || block.error) return;
  const qualityHeaders = QUALITY_FIELDS.flatMap(([, code]) => [code, `${code}_missing_reason`]);
  const qualityCells = QUALITY_FIELDS.flatMap(([key, , , defaultReason]) => {
    const item = qualityField(block, key, defaultReason);
    return [item.value, item.missing_reason];
  });
  const headers = ["symbol", "requested_date", "cutoff_at", ...marketStateCsvHeaders(), ...qualityHeaders];
  const row = [quote.symbol, quote.requested_date, quote.cutoff_at, ...marketStateCsvCells(quote), ...qualityCells]
    .map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`);
  const csv = `\uFEFF${headers.join(",")}\r\n${row.join(",")}\r\n`;
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${quote.symbol}-market-state.csv`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function showChartForQuote(quote) {
  loadChart({ symbol: quote.symbol, anchorDate: quote.requested_date, interval: quote.interval, range: "1w" });
}

async function runQuote(event) {
  event.preventDefault();
  const button = byId("runQuote");
  if (button.disabled) return;
  const symbol = selectedAssetValue();
  if (!symbol) {
    setMessage("quoteMessage", "请选择或输入标的。", true);
    return;
  }
  const params = new URLSearchParams({ symbol, interval: byId("quoteInterval").value });
  if (byId("quoteDate").value) params.set("date", byId("quoteDate").value);
  state.quote = null;
  byId("quoteResult").hidden = true;
  byId("quoteMarketDetails").open = false;
  byId("quoteMarketStateSection").hidden = true;
  setMessage("quoteMarketMessage", "");
  button.disabled = true;
  setMessage("quoteMessage", "正在查询价格…");

  try {
    const result = await fetchJson(`./api/price?${params}`);
    state.quote = { result, params };
    const name = result.symbol === "BZ=F" ? "布伦特原油代理" : result.name;
    byId("quoteAsset").textContent = [...new Set([name, result.symbol].filter(Boolean))].join(" · ");
    const price = byId("quotePrice");
    price.textContent = result.price == null ? "缺少价格" : SHOW_PRICE.format(result.price);
    if (result.price != null && (result.unit || result.currency)) {
      addText(price, "span", result.unit || result.currency, "price-unit");
    }
    const observation = byId("quoteObservation");
    if (result.observed_at) {
      const time = result.observed_at.slice(0, 19).replace("T", " ");
      const prior = result.price_status === "prior_session" ? "较早交易时段 · " : "";
      const resolution = result.interval === "1h" ? "小时线收盘" : "日线收盘";
      observation.textContent = `${prior}${time} · ${result.exchange_timezone || "交易所当地时间"} · ${resolution} · ${result.source || "来源未记录"}`;
    } else {
      observation.textContent = result.message || "查询截点之前没有已完成的行情。";
    }
    byId("quoteResult").hidden = false;
    if (byId("chartPanel").open) showChartForQuote(result);
    setMessage("quoteMessage", "");
  } catch (error) {
    setMessage("quoteMessage", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function loadQuoteFactors() {
  const quote = state.quote;
  if (!byId("quoteMarketDetails").open || !quote || quote.factors || quote.loadingFactors) return;
  const params = new URLSearchParams(quote.params);
  if (quote.result.requested_date) params.set("date", quote.result.requested_date);
  if (quote.result.cutoff_at) params.set("as_of", quote.result.cutoff_at);
  params.set("market_state", "1");
  quote.loadingFactors = true;
  setMessage("quoteMarketMessage", "正在读取市场状态…");
  try {
    const result = await fetchJson(`./api/price?${params}`);
    if (state.quote !== quote) return;
    if (!result.market_state) throw new Error("本次查询没有市场状态指标。");
    if (!result.market_state.error) quote.factors = result;
    renderMarketState(result.market_state);
    setMessage("quoteMarketMessage", "");
  } catch (error) {
    if (state.quote === quote) setMessage("quoteMarketMessage", error.message, true);
  } finally {
    quote.loadingFactors = false;
  }
}

function fillAssets(assets) {
  if (!assets?.length) return;
  const select = byId("asset"), selected = select.value;
  const groups = new Map(["港股", "石油相关", "其他"].map((label) => {
    const group = document.createElement("optgroup");
    group.label = label;
    return [label, group];
  }));
  for (const asset of assets) {
    const option = document.createElement("option");
    option.value = asset.ticker;
    option.textContent = assetOptionText(asset);
    if (asset.ticker === "BZ=F") option.textContent = "布伦特原油代理 · BZ=F · USD";
    option.disabled = asset.queryable === false;
    const group = asset.oil_research ? "石油相关" : asset.market === "XHKG" ? "港股" : "其他";
    groups.get(group).append(option);
  }
  select.replaceChildren(...[...groups.values()].filter((group) => group.children.length));
  if ([...select.options].some((option) => option.value === selected && !option.disabled)) {
    select.value = selected;
  }
}

async function loadCatalog() {
  try {
    const data = await fetchJson("./api/assets");
    fillAssets(data.assets);
    byId("assetHelp").textContent = data.yahoo_ondemand
      ? "未入库的标的会尝试从 Yahoo 获取。" : "可查范围取决于本地行情库。";
    byId("priceStatus").textContent = data.price_source || "行情服务已连接";
  } catch (error) {
    byId("priceStatus").textContent = "行情服务不可用";
    setMessage("quoteMessage", `无法连接行情服务：${error.message}`, true);
  }
}

byId("asset").addEventListener("change", () => { byId("assetInput").value = ""; });
byId("quoteForm").addEventListener("submit", runQuote);
byId("quoteMarketDetails").addEventListener("toggle", loadQuoteFactors);
byId("downloadQuoteFactors").addEventListener("click", downloadQuoteFactors);
byId("showQuoteChart").addEventListener("click", () => {
  if (!state.quote) return;
  byId("chartPanel").open = true;
  showChartForQuote(state.quote.result);
  byId("chartPanel").scrollIntoView({ block: "start" });
});
loadCatalog();
initChart();
