import test from "node:test";
import assert from "node:assert/strict";
import { hongKongInputToIso, parsePricesCsv, queryPrices, resolveAsset } from "./core.mjs";

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

test("asset names and arbitrary Hong Kong tickers resolve", () => {
  assert.equal(resolveAsset("比亚迪"), "1211.HK");
  assert.equal(resolveAsset("HK.02318"), "2318.HK");
  assert.throws(() => resolveAsset("电动车"), /未知港股/);
  assert.equal(hongKongInputToIso("2026-09-28T10:00"), "2026-09-28T10:00:00+08:00");
});

test("query uses completed minute bars and reports actual observations", () => {
  const bars = parsePricesCsv(fixture);
  const result = queryPrices("BYD", "2026-09-28T10:00:30+08:00", bars,
    { asOf: "2026-10-10T12:00:00+08:00" });
  assert.equal(result.baseline.price, 100);
  assert.equal(result.baseline.gap_seconds, 30);
  assert.equal(result.windows.before_1m.price, 90);
  assert.equal(result.windows.after_1h.price, 103);
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
