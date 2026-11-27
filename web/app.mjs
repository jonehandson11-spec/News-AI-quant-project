import { assetDisplayName, assetOptionText, englishText, marketStateCsvCells, marketStateCsvHeaders, selectedAssetValue } from "./core.mjs";
import { initChart, intervalLabel, isMinuteInterval, loadChart } from "./chart.mjs";

const byId = (id) => document.getElementById(id);
const state = { quote: null };
const MARKET_FIELDS = [
  ["momentum_z", "M01 Price Momentum"],
  ["realized_volatility", "M02 Realized Volatility (daily, not annualized)"],
  ["relative_trading_activity", "M03 Relative Trading Activity"],
  ["liquidity_stress_z", "M04 Liquidity Stress"],
  ["benchmark_beta", "M05 Benchmark Beta"],
  ["benchmark_momentum_z", "M06 Benchmark Momentum"],
  ["data_coverage", "Price Sample Coverage"],
];
const QUALITY_FIELDS = [
  ["evidence_reliability", "q01_evidence_reliability", "Q01 Evidence Reliability", "ESTIMATOR_NOT_READY"],
  ["data_coverage", "q02_data_coverage", "Q02 Data Coverage", "REFERENCE_MISSING"],
];
const MISSING_REASONS = {
  INSUFFICIENT_HISTORY: "Insufficient history", NOT_APPLICABLE: "Not applicable",
  ESTIMATOR_NOT_READY: "Not assessed", REFERENCE_MISSING: "Reference scope undefined",
  NOT_OBSERVED: "No observation", OUT_OF_COVERAGE: "Outside data coverage",
  AMBIGUOUS: "Method unclear", SOURCE_UNAVAILABLE: "Data source unavailable",
};
const SHOW_PRICE = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const SHOW_RATIO = new Intl.NumberFormat("en-US", { style: "percent", maximumFractionDigits: 1 });

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
    [item.window_start, item.window_end, "Window"],
    [item.reference_start || item.reference_first_end_date, item.reference_end || item.reference_last_end_date, "Reference"],
    [item.first_return_date, item.last_return_date, "Return sample"],
    [item.first_pair_date, item.last_pair_date, "Paired sample"],
    [item.first_expected_date, item.last_expected_date, "Expected sample"],
  ];
  for (const [from, to, name] of periods) {
    if (from && to) details.push(`${name} ${from} to ${to}`);
  }
  if (item.benchmark) details.push(item.benchmark);
  if (item.value == null) {
    details.push(MISSING_REASONS[item.missing_reason] || englishText(item.missing_reason, "No observation"));
  }
  for (const key of ["scope_id", "method_version", "source_ref"]) {
    if (typeof item[key] === "string" && item[key]) details.push(item[key]);
  }
  const note = englishText(item.note_en, englishText(item.note));
  if (note) details.push(note);
  return details;
}

function renderMarketState(block) {
  const failed = Boolean(block.error);
  byId("quoteMarketStateSection").hidden = false;
  setMessage("quoteMarketStateError", failed ? englishText(block.error_en, englishText(block.error, "Market state is unavailable.")) : "", failed);
  byId("quoteMarketStateError").hidden = !failed;
  byId("quoteMarketStateTableWrap").hidden = failed;
  byId("downloadQuoteFactors").hidden = failed;
  const basis = { adj: "Adjusted close", raw: "Raw close" }[block.price_basis] || "—";
  byId("quoteMarketStateMeta").textContent = failed ? block.profile_id || ""
    : `As of ${block.cutoff_date || "—"} · ${basis} · Benchmark ${block.benchmark || "—"} · ${block.profile_id || "—"}`;

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
      if (assessed) details.push(`${assessed} ${assessed === 1 ? "variable" : "variables"} assessed`);
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
  if (!response.ok) throw new Error(englishText(body.error_en, englishText(body.error, `Request failed (HTTP ${response.status}).`)));
  return body;
}

async function runQuote(event) {
  event.preventDefault();
  const button = byId("runQuote");
  if (button.disabled) return;
  const symbol = selectedAssetValue();
  if (!symbol) {
    setMessage("quoteMessage", "Select or enter an instrument.", true);
    return;
  }
  const params = new URLSearchParams({ symbol, interval: byId("quoteInterval").value });
  if (byId("quoteDate").value) params.set("date", byId("quoteDate").value);
  state.quote = null;
  byId("quoteResult").hidden = true;
  byId("chartPanel").hidden = true;
  byId("quoteMarketStateSection").hidden = true;
  setMessage("quoteMarketMessage", "");
  button.disabled = true;
  setMessage("quoteMessage", "Looking up price…");

  try {
    const result = await fetchJson(`./api/price?${params}`);
    const quote = { result, params };
    state.quote = quote;
    const name = assetDisplayName(result);
    byId("quoteAsset").textContent = [...new Set([name, result.symbol].filter(Boolean))].join(" · ");
    const price = byId("quotePrice");
    price.textContent = result.price == null ? "Price unavailable" : SHOW_PRICE.format(result.price);
    if (result.price != null && (result.unit || result.currency)) {
      addText(price, "span", result.unit || result.currency, "price-unit");
    }
    const observation = byId("quoteObservation");
    if (result.observed_at) {
      const time = result.observed_at.slice(0, 19).replace("T", " ");
      const prior = result.price_status === "prior_session" ? "Earlier trading session · " : "";
      const resolution = `${intervalLabel(result.interval)} close`;
      const source = englishText(result.source_en, englishText(result.source, "Source not recorded"));
      observation.textContent = `${prior}${time} · ${result.exchange_timezone || "Exchange local time"} · ${resolution} · ${source}`;
    } else {
      observation.textContent = englishText(result.message_en, englishText(result.message, "No completed price before the cutoff."));
    }
    byId("quoteResult").hidden = false;
    byId("chartPanel").hidden = false;
    const minute = isMinuteInterval(result.interval);
    const anchorDate = minute && result.observed_at ? result.observed_at.slice(0, 10) : result.requested_date;
    loadChart({ symbol: result.symbol, anchorDate, interval: result.interval, range: minute ? "1d" : "1w" });
    loadQuoteFactors(quote);
    setMessage("quoteMessage", "");
  } catch (error) {
    setMessage("quoteMessage", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function loadQuoteFactors(quote) {
  const params = new URLSearchParams(quote.params);
  if (quote.result.requested_date) params.set("date", quote.result.requested_date);
  if (quote.result.cutoff_at) params.set("as_of", quote.result.cutoff_at);
  params.set("interval", quote.result.interval);
  params.set("market_state", "1");
  setMessage("quoteMarketMessage", "Loading market state…");
  try {
    const result = await fetchJson(`./api/price?${params}`);
    if (state.quote !== quote) return;
    if (!result.market_state) throw new Error("No market-state indicators for this query.");
    if (!result.market_state.error) quote.factors = result;
    renderMarketState(result.market_state);
    setMessage("quoteMarketMessage", "");
  } catch (error) {
    if (state.quote === quote) setMessage("quoteMarketMessage", error.message, true);
  }
}

function fillAssets(assets) {
  if (!assets?.length) return;
  const select = byId("asset"), selected = select.value;
  const groups = new Map(["Hong Kong", "Oil-related", "Other"].map((label) => {
    const group = document.createElement("optgroup");
    group.label = label;
    return [label, group];
  }));
  for (const asset of assets) {
    const option = document.createElement("option");
    option.value = asset.ticker;
    option.textContent = assetOptionText(asset);
    option.disabled = asset.queryable === false;
    const group = asset.oil_research ? "Oil-related" : asset.market === "XHKG" ? "Hong Kong" : "Other";
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
      ? "Missing instruments are looked up on Yahoo when available." : "Availability depends on the local price database.";
    byId("priceStatus").textContent = englishText(data.price_source_en, englishText(data.price_source, "Price service connected"));
  } catch (error) {
    byId("priceStatus").textContent = "Price service unavailable";
    setMessage("quoteMessage", `Could not connect to the price service. ${error.message}`, true);
  }
}

byId("asset").addEventListener("change", () => { byId("assetInput").value = ""; });
byId("quoteForm").addEventListener("submit", runQuote);
byId("downloadQuoteFactors").addEventListener("click", downloadQuoteFactors);
loadCatalog();
initChart();
