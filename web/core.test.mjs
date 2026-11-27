import test from "node:test";
import assert from "node:assert/strict";
import { assetOptionText, dailyColumns, detailPreference, detailToggleAvailable, hongKongInputToIso, marketStateCsvCells, marketStateCsvHeaders, parsePricesCsv, queryPrices, relativeHongKongInput, resolveAsset, selectedAssetValue } from "./core.mjs";

test("detail preference defaults off and URL overrides stored preference", () => {
  assert.equal(detailPreference("", null), false);
  assert.equal(detailPreference("?detail=1", "0"), true);
  assert.equal(detailPreference("?detail=0", "1"), false);
  assert.equal(detailPreference("", "1"), true);
});

test("daily columns show optional stock detail and never expose it for futures", () => {
  const stock = { mode: "daily", ticker: "1211.HK", price_unit: "HKD",
    benchmarks: { "2800.HK": {}, "^HSI": {} } };
  const compact = dailyColumns(stock);
  assert.deepEqual(compact, ["窗口", "起点（HKD）", "终点（HKD）", "涨跌幅（原始）", "状态"]);
  assert.equal(compact.some((label) => label.includes("复权") || label.includes("超额")), false);
  const detailed = dailyColumns(stock, { showDetail: true });
  assert.ok(detailed.includes("涨跌幅（复权）"));
  assert.ok(detailed.includes("超额 vs 盈富 2800（原始 / 复权）"));
  assert.ok(detailed.includes("超额 vs 恒指（原始 / 复权）"));
  const futures = { ...stock, contract: { contract: "CLZ26" } };
  assert.equal(detailToggleAvailable(futures), false);
  assert.deepEqual(dailyColumns(futures, { showDetail: true }), compact);
});

const header = "ticker,interval,timestamp,close\n";
const fixture = header + [
  "1211.HK,day,2026-03-27,70",
  "1211.HK,day,2026-06-26,80",
  "1211.HK,day,2026-08-28,90",
  "1211.HK,minute,2026-09-28T09:59:00+08:00,100",
  "1211.HK,minute,2026-09-28T10:00:00+08:00,999",
  "1211.HK,minute,2026-09-28T11:00:00+08:00,102",
  "1211.HK,minute,2026-09-28T11:01:00+08:00,103",
  "1211.HK,minute,2026-09-29T09:30:00+08:00,110",
  "1211.HK,minute,2026-10-05T10:00:00+08:00,120",
  "1211.HK,minute,2026-10-05T10:01:00+08:00,121",
].join("\n");

test("only the first seven assets resolve", () => {
  assert.equal(resolveAsset("比亚迪"), "1211.HK");
  assert.equal(resolveAsset("南方恒生科技 ETF"), "3033.HK");
  assert.throws(() => resolveAsset("HK.02318"), /首版仅支持/);
  assert.throws(() => resolveAsset("电动车"), /未知港股/);
  assert.equal(hongKongInputToIso("2026-09-28T10:00"), "2026-09-28T10:00:00+08:00");
});

test("relative time presets use the click time in Hong Kong across date boundaries", () => {
  const now = Date.parse("2026-10-01T00:30:45+08:00");
  assert.equal(relativeHongKongInput(1, now), "2026-09-30T23:30:45");
  assert.equal(relativeHongKongInput(6, now), "2026-09-30T18:30:45");
  assert.equal(relativeHongKongInput(24, now), "2026-09-30T00:30:45");
});

test("query uses completed minute bars and reports actual observations", () => {
  const bars = parsePricesCsv(fixture);
  const result = queryPrices("BYD", "2026-09-28T10:00:30+08:00", bars,
    { asOf: "2026-10-10T12:00:00+08:00" });
  assert.equal(result.baseline.price, 100);
  assert.equal(result.baseline.gap_seconds, 30);
  assert.equal(result.windows.before_1m.price, 90);
  assert.equal(result.windows.before_1m.price_change, 10);
  assert.equal(result.windows.after_1h.price, 103);
  assert.equal(result.windows.after_1h.price_change, 3);
  assert.equal(result.windows.after_1h.status, "on_time");
  assert.equal(result.windows.after_12h.status, "deferred");
  assert.equal(result.windows.after_1w.price, 121);
});

test("uncompleted minute bars stay unavailable at the cutoff", () => {
  const result = queryPrices("BYD", "2026-09-28T10:00:00+08:00", parsePricesCsv(fixture),
    { asOf: "2026-09-28T11:00:30+08:00" });
  assert.equal(result.windows.after_1h.status, "missing");
  assert.equal(result.windows.after_3h.status, "pending");
});

test("calendar month end clamps and missing earlier daily bars remain missing", () => {
  const bars = parsePricesCsv(header + "1211.HK,minute,2026-03-31T09:59:00+08:00,100\n");
  const result = queryPrices("BYD", "2026-03-31T10:00:00+08:00", bars,
    { asOf: "2026-04-01T10:00:00+08:00" });
  assert.equal(result.windows.before_1m.target_at.slice(0, 10), "2026-02-28");
  assert.equal(result.windows.before_1m.status, "missing");
  assert.equal(result.windows.before_1m.price_change, null);
});

test("bad price CSV is rejected before a query", () => {
  assert.throws(() => parsePricesCsv(header + "1211.HK,minute,2026-09-28T09:59:00,100\n"), /时区/);
  assert.throws(() => parsePricesCsv(header + "1211.HK,minute,2026-09-28T09:59:00+08:00,nan\n"), /有限数字/);
  const duplicated = header + "1211.HK,minute,2026-09-28T09:59:00+08:00,100\n1211.HK,minute,2026-09-28T09:59:00+08:00,101\n";
  assert.throws(() => parsePricesCsv(duplicated), /重复价格/);
});

test("benchmark excess return needs aligned baselines", () => {
  const csv = fixture + "\n2800.HK,minute,2026-09-28T09:55:00+08:00,50\n2800.HK,minute,2026-09-28T11:00:00+08:00,51\n";
  const result = queryPrices("BYD", "2026-09-28T10:00:00+08:00", parsePricesCsv(csv),
    { asOf: "2026-09-28T12:00:00+08:00", benchmark: true });
  assert.equal(result.windows.after_1h.excess_return_pct, null);
});

test("asset options prefer English labels and preserve symbol and availability", () => {
  assert.equal(
    assetOptionText({ name: "腾讯", name_en: "Tencent", ticker: "0700.HK", currency: "HKD" }),
    "Tencent · 0700.HK · HKD",
  );
  assert.equal(
    assetOptionText({ name: "布伦特原油", ticker: "BZ=F", currency: "USD" }),
    "Brent Crude Oil Proxy · BZ=F · USD",
  );
  assert.equal(
    assetOptionText({ name: "ICE 低硫柴油", name_en: "ICE Low Sulphur Gasoil", ticker: "ICE-LSGO", currency: "USD", data_status: "no_free_source" }),
    "ICE Low Sulphur Gasoil · ICE-LSGO · USD (no free data)",
  );
  assert.equal(
    assetOptionText({ name: "外部港股", ticker: "0388.HK", currency: "HKD" }),
    "0388.HK · HKD",
  );
});

test("selected asset prefers the free-text input over the catalog select", () => {
  const nodes = { assetInput: { value: "阿里巴巴" }, asset: { value: "BZ=F" } };
  const root = { getElementById: (id) => nodes[id] };
  assert.equal(selectedAssetValue(root), "阿里巴巴");
  nodes.assetInput.value = "  ";
  assert.equal(selectedAssetValue(root), "BZ=F");
});

test("market state CSV cells keep computed values and leave missing fields empty", () => {
  const headers = marketStateCsvHeaders();
  assert.deepEqual(headers, [
    "momentum_z", "momentum_z_missing_reason",
    "realized_volatility", "realized_volatility_missing_reason",
    "relative_trading_activity", "relative_trading_activity_missing_reason",
    "liquidity_stress_z", "liquidity_stress_z_missing_reason",
    "benchmark_beta", "benchmark_beta_missing_reason",
    "benchmark_momentum_z", "benchmark_momentum_z_missing_reason",
    "data_coverage", "data_coverage_missing_reason",
    "profile_id", "cutoff_date", "benchmark",
  ]);
  const cells = marketStateCsvCells({
    market_state: {
      profile_id: "pq-market-state-daily-v1",
      cutoff_date: "2026-09-25",
      benchmark: "2800.HK",
      fields: {
        momentum_z: { value: -0.6595588260084632, missing_reason: null },
        realized_volatility: { value: 0.01535311845855941 },
        relative_trading_activity: { value: 0.5807046764688161 },
        liquidity_stress_z: { value: null, missing_reason: "ESTIMATOR_NOT_READY" },
        benchmark_beta: { value: 0.6884755979714041 },
        benchmark_momentum_z: { value: -0.8964502213602309 },
        data_coverage: { value: 1.0 },
      },
    },
  });
  assert.equal(Number(cells[0]).toFixed(4), "-0.6596");
  assert.equal(Number(cells[2]).toFixed(6), "0.015353");
  assert.equal(Number(cells[4]).toFixed(4), "0.5807");
  assert.equal(cells[6], "");
  assert.equal(cells[7], "ESTIMATOR_NOT_READY");
  assert.equal(Number(cells[8]).toFixed(4), "0.6885");
  assert.equal(Number(cells[10]).toFixed(4), "-0.8965");
  assert.equal(cells[12], 1);
  assert.equal(cells[13], "");
  assert.equal(cells[14], "pq-market-state-daily-v1");
  assert.equal(cells[15], "2026-09-25");
  assert.equal(cells[16], "2800.HK");

  const missing = marketStateCsvCells({ market_state: { profile_id: "pq-market-state-daily-v1", error: "需要本地价格库" } });
  assert.deepEqual(missing.slice(0, 14), ["", "", "", "", "", "", "", "", "", "", "", "", "", ""]);
  assert.equal(missing[14], "pq-market-state-daily-v1");
  assert.deepEqual(marketStateCsvCells({}), Array(17).fill(""));
});

