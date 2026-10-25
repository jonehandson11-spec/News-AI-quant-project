import { WINDOW_LABELS, dailyColumns, detailPreference, detailToggleAvailable, hongKongInputToIso, relativeHongKongInput } from "./core.mjs";

const byId = (id) => document.getElementById(id);
const state = { news: [], result: null, newsRequest: 0,
  showDetail: detailPreference(window.location.search, localStorage.getItem("pq.showAdjExcess")) };
const fullResultNote = "日线模式：t0 为事件日（收盘前发布则当天，否则下一交易日）；基准为发布前最后一个收盘价。新闻前涨跌幅＝从 N 个交易日前收盘到基准；新闻后涨跌幅＝从基准到 t+K 收盘。原始＝未除权收盘价（仅拆股调整），复权＝含分红调整。超额＝个股涨跌幅－同市场对照涨跌幅（简单差值，不是市场模型）；恒生指数、多伦多综指为价格指数，复权与原始相同。时间列为交易所当地时间，涨跌幅以标的当地货币计（未做汇率换算）。缺数据不按零处理。新闻与价格同时变化不代表因果关系。";
const shortResultNote = "日线模式：t0 为事件日（收盘前发布则当天，否则下一交易日）；基准为发布前最后一个收盘价。新闻前涨跌幅＝从 N 个交易日前收盘到基准；新闻后涨跌幅＝从基准到 t+K 收盘。时间列为交易所当地时间，涨跌幅以标的当地货币计（未做汇率换算）。缺数据不按零处理。新闻与价格同时变化不代表因果关系。";
const hourlyResultNote = "小时模式：基准＝新闻发布前最后一根已结束小时线的收盘价；新闻前 N 小时＝从 N 个交易小时前到基准的涨跌，新闻后 N 小时＝从基准到 N 个交易小时后的涨跌，各取该时刻前最后一根已结束小时线。交易小时只算交易时段，午休、收盘后、周末和假期不算；期货按 Globex 时段（美东前一日 18:00 至当日 17:00）。时间为交易所当地时间。小时线约覆盖最近两年，更早的新闻显示缺数据，缺数据不按零处理。新闻与价格同时变化不代表因果关系。";
const statusText = {
  on_date: "目标日期", deferred_back: "向前顺延", on_time: "准时",
  deferred: "顺延取价", pending: "未到期", missing: "缺数据", suspended: "停牌", expired: "合约已到期",
};
const flagText = { missing_data: "该交易日无行情（停牌或缺数据，无法区分）", zero_volume: "成交量为 0（零成交，或数据源缺成交量）", nonpositive_price: "价格≤0（如 2020-04-20 WTI 负结算价），收益率无意义" };
const benchmarkName = {
  "2800.HK": "盈富 2800", "^HSI": "恒指", "3033.HK": "恒科 3033",
  SPY: "标普500 ETF SPY", XLE: "能源板块 ETF XLE", "XIU.TO": "TSX 60 ETF XIU", "^GSPTSE": "多伦多综指",
};
const marketText = { XHKG: "港交所", XNYS: "美股（纽交所日历）", XTSE: "多伦多交易所", XSAU: "沙特交易所", NYMEX: "纽约商品交易所", ICE_EU: "ICE 欧洲" };
const showTime = (value) => value ? value.slice(0, 19).replace("T", " ") : "—";
const showDate = (value) => value ? String(value).slice(0, 10) : "缺失";
// 小时模式显示到分钟（交易所当地时间），日线模式只显示日期。
const showEndpoint = (value) => value && state.result?.mode === "hourly" ? String(value).slice(0, 16).replace("T", " ") : showDate(value);
const hourlyFlagText = { missing_data: "没有可用的小时线（超出约两年覆盖、停牌或缺数据）" };
const flagLabel = (flag) => (state.result?.mode === "hourly" && hourlyFlagText[flag]) || flagText[flag] || flag;
const showPrice = (value) => value === null || value === undefined ? "缺失" : Number(value).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
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
  for (const column of columns) {
    const th = addText(head, "th", typeof column === "string" ? column : column.text);
    if (typeof column === "object" && column.title) th.title = column.title;
  }
}

function priceUnit(result) {
  return result.price_unit || result.currency || "";
}

function priceWithUnit(value, unit) {
  const price = showPrice(value);
  return value === null || value === undefined || !unit ? price : `${price} ${unit}`;
}

function addPrice(parent, value, unit, className = "") {
  return addText(parent, "span", priceWithUnit(value, unit), `price-value ${className}`.trim());
}

function setBaselinePrice(value, unit) {
  const target = byId("baselinePrice");
  target.replaceChildren();
  addText(target, "span", showPrice(value), "price-value");
  if (value !== null && value !== undefined && unit) addText(target, "small", unit, "price-unit");
}

function returnCell(tr, value) {
  addText(tr, "td", showReturn(value), value === null || value === undefined ? "" : value >= 0 ? "positive" : "negative");
}

function endpointCell(tr, date, price, unit) {
  const cell = document.createElement("td");
  addText(cell, "span", showEndpoint(date), "endpoint-date");
  addPrice(cell, price, unit);
  tr.append(cell);
}

function adjustedReturnCell(tr, item, unit) {
  const cell = document.createElement("td");
  addText(cell, "span", showReturn(item.adj_return_pct), item.adj_return_pct === null || item.adj_return_pct === undefined ? "" : item.adj_return_pct >= 0 ? "positive" : "negative");
  const detail = addText(cell, "small", "", "comparison-note price-range");
  addPrice(detail, item.adj_start_price, unit);
  detail.append(document.createTextNode(" → "));
  addPrice(detail, item.adj_end_price, unit);
  tr.append(cell);
}

function statusCell(tr, item) {
  const cell = document.createElement("td");
  addText(cell, "span", statusText[item.status] || item.status, `status ${item.status}`);
  if (item.flag) addText(cell, "small", flagLabel(item.flag), "comparison-note");
  tr.append(cell);
}

function renderDaily(result) {
  const codes = Object.keys(result.benchmarks || {});
  const contract = result.contract;
  const unit = priceUnit(result);
  byId("baselineLabel").textContent = contract
    ? `${result.mode === "hourly" ? "发布前最后一根小时线的收盘价" : "发布前最后一个结算价"}（固定合约 ${contract.contract}${contract.switched ? "，临近到期改用" : ""}）` : result.mode === "hourly" ? "发布前最后一根小时线的收盘价（基准）" : "发布前最后一个收盘价（基准）";
  setBaselinePrice(result.baseline.price, unit);
  const adj = result.baseline.adj_price === null || result.baseline.adj_price === undefined ? "" : ` · 复权 ${priceWithUnit(result.baseline.adj_price, unit)}`;
  const flag = result.baseline.flag ? ` · ${flagLabel(result.baseline.flag)}` : "";
  const localNote = result.market && result.market !== "XHKG" ? "（当地时间）" : "";
  byId("baselineDetail").textContent = result.mode === "hourly"
    ? `${showTime(result.baseline.observed_at)}${localNote} 小时线结束时间${adj} · 发布时段 ${sessionText[result.session] || result.session}${flag}`
    : `${showTime(result.baseline.observed_at)}${localNote} 收盘${adj} · 事件日 t0 = ${result.t0} · 发布时段 ${sessionText[result.session] || result.session}${flag}`;
  if (contract) {
    renderFutures(result, contract);
    return;
  }
  setHead(dailyColumns(result, { showDetail: state.showDetail }));
  const body = byId("resultRows");
  body.replaceChildren();
  for (const [label, item] of Object.entries(result.windows)) {
    const tr = document.createElement("tr");
    addText(tr, "td", item.label_zh || WINDOW_LABELS[label] || label);
    endpointCell(tr, item.start_at, item.start_price, unit);
    endpointCell(tr, item.end_at, item.end_price, unit);
    returnCell(tr, item.return_pct);
    if (state.showDetail) adjustedReturnCell(tr, item, unit);
    for (const code of state.showDetail ? codes : []) {
      const compared = result.benchmarks[code];
      const row = compared.windows?.[label];
      const cell = document.createElement("td");
      addText(cell, "span", compared.error ? "无对照行情"
        : `${showReturn(row?.excess_return_pct)} / ${showReturn(row?.adj_excess_return_pct)}`);
      if (!compared.error && row) {
        const benchmarkUnit = compared.price_unit || compared.currency || result.currency || "";
        const detail = addText(cell, "small", `${benchmarkName[code] || code} `, "comparison-note");
        addPrice(detail, row.start_price, benchmarkUnit);
        detail.append(document.createTextNode(" → "));
        addPrice(detail, row.end_price, benchmarkUnit);
        detail.append(document.createTextNode(` ${showReturn(row.return_pct)}`));
      }
      tr.append(cell);
    }
    statusCell(tr, item);
    body.append(tr);
  }
  renderSubtitle(result, codes);
  byId("resultNote").textContent = result.mode === "hourly"
    ? hourlyResultNote
    : state.showDetail ? fullResultNote : shortResultNote;
}

function renderFutures(result, contract) {
  const unit = priceUnit(result);
  setHead(["窗口", `起点（${unit}）`, `终点（${unit}）`, `涨跌幅（固定合约 ${contract.contract}）`,
    "状态"]);
  const body = byId("resultRows");
  body.replaceChildren();
  for (const [label, item] of Object.entries(result.windows)) {
    const tr = document.createElement("tr");
    addText(tr, "td", item.label_zh || WINDOW_LABELS[label] || label);
    endpointCell(tr, item.start_at, item.start_price, unit);
    endpointCell(tr, item.end_at, item.end_price, unit);
    returnCell(tr, item.return_pct);
    statusCell(tr, item);
    body.append(tr);
  }
  if (contract.error) byId("baselineDetail").textContent = `固定合约 ${contract.contract} 无基准价格（雅虎不提供已到期合约，且本地库未存到该日）：${contract.error}`;
  renderSubtitle(result, []);
  byId("resultNote").textContent = result.mode === "hourly" ? hourlyResultNote : shortResultNote;
}

function renderSubtitle(result, codes) {
  const version = result.data_version ? ` · 数据版本 ${result.data_version.run_id}` : "";
  const contractNote = result.contract
    ? ` · 固定合约 ${result.contract.contract}（最后交易日 ${result.contract.last_trade_date}）` : "";
  const local = result.market && result.market !== "XHKG"
    ? ` = ${showTime(result.published_at_local)} 交易所当地时间（${result.exchange_timezone}）` : "";
  const currency = result.currency ? ` · 价格与涨跌幅以 ${result.currency} 计` : "";
  const unit = result.price_unit || result.currency ? ` · 价格单位：${result.price_unit || result.currency}` : "";
  const noBench = codes.length === 0 ? ` · 无对照${result.market === "XSAU" ? "（沙特指数无免费历史数据）" : result.asset_type === "futures" ? "（期货）" : ""}` : "";
  byId("resultSubtitle").textContent = `${result.ticker} · ${marketText[result.market] || result.market || "港交所"} · 新闻发布 ${showTime(result.published_at)} 香港时间${local}${currency}${unit}${contractNote}${noBench} · ${result.baseline.source || "—"}${version}`;
}

const sessionText = { pre_open: "开盘前", morning: "上午盘", lunch_break: "午休", afternoon: "下午盘", after_close: "收盘后", non_trading_day: "非交易日", regular_session: "交易时段内" };

function renderResult(result) {
  state.result = result;
  byId("results").hidden = false;
  const toggle = byId("detailToggle");
  const available = detailToggleAvailable(result);
  toggle.disabled = !available;
  toggle.title = available ? "" : "期货无复权/超额列";
  toggle.checked = state.showDetail;
  if (result.mode === "daily" || result.mode === "hourly") {
    renderDaily(result);
    byId("results").scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  setHead(["窗口", "目标时间", "实际取价", "价格", "差价", "涨跌幅", "状态"]);
  byId("baselineLabel").textContent = "发布前最后一根已完成分钟线";
  byId("resultSubtitle").textContent = `${result.ticker} · 新闻发布 ${showTime(result.published_at)} 香港时间 · ${result.baseline.source}`;
  byId("resultNote").textContent = shortResultNote;
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
  if (result.mode === "daily" || result.mode === "hourly") {
    const codes = Object.keys(result.benchmarks || {});
    const headers = ["ticker", "price_unit", "currency", "published_at", "session", "t0", "baseline_at", "baseline_price", "baseline_adj_price",
      "window", "target_at", "observed_at", "price", "adj_price", "start_at", "start_price", "end_at", "end_price", "adj_start_price", "adj_end_price", "return_pct", "adj_return_pct", "status", "flag", "contract", "continuous_price", "continuous_return_pct", "continuous_start_price", "continuous_end_price", "continuous_crosses_roll", "continuous_rolls", "source",
      ...codes.flatMap((code) => [`${code}_return_pct`, `${code}_excess_pct`, `${code}_adj_excess_pct`, `${code}_start_price`, `${code}_end_price`, `${code}_price_unit`]), "data_version"];
    const rows = Object.entries(result.windows).map(([label, item]) => [
      result.ticker, result.price_unit || result.currency || "", result.currency || "", result.published_at, result.session, result.t0, result.baseline.observed_at, result.baseline.price,
      result.baseline.adj_price, label, item.target_at, item.observed_at, item.price, item.adj_price, item.start_at, item.start_price,
      item.end_at, item.end_price, item.adj_start_price, item.adj_end_price, item.return_pct,
      item.adj_return_pct, item.status, item.flag, item.contract, item.continuous?.price, item.continuous?.return_pct,
      item.continuous?.start_price, item.continuous?.end_price,
      item.continuous?.crosses_roll ?? item.crosses_roll, JSON.stringify(item.continuous?.rolls || item.rolls || []), item.source,
      ...codes.flatMap((code) => { const compared = result.benchmarks[code]; const row = compared.windows?.[label]; return [row?.return_pct, row?.excess_return_pct, row?.adj_excess_return_pct, row?.start_price, row?.end_price, compared.price_unit || compared.currency || result.currency || ""]; }),
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
    const pending = asset.data_status === "no_free_source" ? "（无免费数据）" : "";
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
    state.mode = data.mode || "hourly";
    byId("detailToggle").disabled = !["daily", "hourly"].includes(state.mode);
    byId("detailToggle").title = ["daily", "hourly"].includes(state.mode) ? "" : "期货无复权/超额列";
    byId("priceStatus").textContent = ["daily", "hourly"].includes(state.mode) ? `${data.price_source}` : `${data.price_source} · 查询时取价`;
    byId("sourceHelp").textContent = state.mode === "hourly"
      ? "默认使用本机价格库的一小时线，按各交易所交易时段累计小时；午休、收盘后、周末和假期不计。小时线约覆盖最近两年，更早新闻显示缺数据。每日仍更新日线，命令行/服务可选 daily 交易日窗口。"
      : state.mode === "daily"
      ? "使用本机价格库日线，窗口按标的所在交易所交易日计算；股票提供原始与复权收益及同市场对照。"
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
byId("detailToggle").checked = state.showDetail;
byId("detailToggle").addEventListener("change", (event) => {
  state.showDetail = event.currentTarget.checked;
  localStorage.setItem("pq.showAdjExcess", state.showDetail ? "1" : "0");
  const url = new URL(window.location.href);
  url.searchParams.set("detail", state.showDetail ? "1" : "0");
  history.replaceState(null, "", url);
  if (state.result) renderResult(state.result);
});
loadCatalog();
