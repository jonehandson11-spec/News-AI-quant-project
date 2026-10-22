import argparse
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.daily_report import daily_report
from news_price_query.news import NewsItem
from news_price_query.price_store import DailyRow, LocalDbPriceProvider, PriceStore
from news_price_query.price_update import UniverseEntry, read_universe, update_prices
from news_price_query.windows import TradingWindowConfig
from web.service import QueryService


HK = ZoneInfo("Asia/Hong_Kong")


def hk(*parts):
    return datetime(*parts, tzinfo=HK)


# Synthetic fixture prices (not market data). (date, close, adj_close, volume)
STOCK = [
    ("2023-08-29", 10.0, 9.0, 100), ("2023-08-30", 10.5, 9.45, 100),
    ("2023-08-31", 11.0, 9.9, 100),
    ("2023-09-01", 11.0, 9.9, 0),        # typhoon closure filler: must be dropped
    ("2023-09-02", 11.0, 9.9, 0),        # Saturday filler: must be dropped
    ("2023-09-04", 10.0, 10.0, 100),     # ex-dividend day: raw falls, adj rises
    ("2023-09-05", 10.2, 10.2, 0),       # zero volume on a normal day
    # 2023-09-06 deliberately missing (suspension or data gap)
    ("2023-09-07", 10.4, 10.4, 100),
    ("2023-09-11", 10.6, 10.6, 100),
    ("2023-09-12", 10.8, 10.8, 100),
    ("2025-12-24", 12.0, 12.0, 0),       # half day with zero volume: kept, no flag
    ("2026-10-22", 13.0, 13.0, 0),       # "today": unfinalized, must be dropped
]
TRACKER = [("2023-08-31", 20.0, 18.0, 1), ("2023-09-04", 20.4, 18.36, 1), ("2023-09-05", 20.2, 18.18, 1),
           ("2023-09-06", 20.0, 18.0, 1), ("2023-09-07", 20.6, 18.54, 1)]
HSI = [("2023-08-31", 18000.0, 18000.0, 0), ("2023-09-04", 18360.0, 18360.0, 0),
       ("2023-09-05", 18180.0, 18180.0, 0), ("2023-09-07", 18540.0, 18540.0, 0)]


def fake_downloader(tables, calls=None):
    def download(ticker, start, end):
        if calls is not None:
            calls.append((ticker, start, end))
        if ticker not in tables:
            raise RuntimeError(f"no data for {ticker}")
        return [{"date": d, "close": c, "adj_close": a, "volume": v}
                for d, c, a, v in tables[ticker] if start <= d < end]
    return download


class PriceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "prices.sqlite3"
        self.store = PriceStore(self.db)
        self.now = hk(2026, 10, 22, 18, 0)
        self.tables = {"0883.HK": STOCK, "2800.HK": TRACKER, "^HSI": HSI}
        self.result = update_prices(self.store, [UniverseEntry("0883.HK", "中国海洋石油", ("中海油",))],
                                    fake_downloader(self.tables), now=self.now, start="2023-08-01")

    def test_update_filters_by_calendar_not_volume(self):
        dates = [row.date for row in self.store.rows("0883.HK")]
        self.assertNotIn("2023-09-01", dates)
        self.assertNotIn("2023-09-02", dates)
        self.assertIn("2023-09-05", dates)       # zero volume trading day kept
        self.assertIn("2025-12-24", dates)       # half day kept
        self.assertNotIn("2026-10-22", dates)    # today is never stored
        summary = {t.ticker: t for t in self.result["tickers"]}
        self.assertEqual(summary["0883.HK"].dropped_non_trading, ["2023-09-01", "2023-09-02"])
        self.assertIn("2023-09-06", summary["0883.HK"].missing_trading_days)
        self.assertEqual(set(summary), {"0883.HK", "2800.HK", "^HSI"})  # benchmarks always added
        self.assertEqual(self.result["status"], "ok")

    def test_rows_carry_market_currency_source_and_run(self):
        row = self.store.rows("0883.HK", "2023-08-31", "2023-08-31")[0]
        self.assertEqual((row.market, row.currency, row.source), ("XHKG", "HKD", "yfinance"))
        self.assertEqual(row.run_id, self.result["run_id"])
        self.assertEqual(self.store.latest_run()["run_id"], self.result["run_id"])
        names = {i.ticker: i for i in self.store.instruments()}
        self.assertEqual(names["0883.HK"].aliases, ("中海油",))
        self.assertEqual(names["^HSI"].role, "benchmark")

    def test_incremental_fetch_starts_inside_stored_range(self):
        calls = []
        update_prices(self.store, [UniverseEntry("0883.HK")], fake_downloader(self.tables, calls),
                      now=self.now, start="2023-08-01", overlap_days=2)
        ticker, start, end = [c for c in calls if c[0] == "0883.HK"][0]
        self.assertEqual((start, end), ("2025-12-22", "2026-10-22"))

    def test_new_dividend_triggers_full_refetch(self):
        changed = [(d, c, round(a * 0.95, 6), v) for d, c, a, v in STOCK]
        calls = []
        result = update_prices(self.store, [UniverseEntry("0883.HK")],
                               fake_downloader({**self.tables, "0883.HK": changed}, calls),
                               now=self.now, start="2023-08-01", overlap_days=300)
        summary = {t.ticker: t for t in result["tickers"]}
        self.assertTrue(summary["0883.HK"].rebased)
        self.assertEqual([c[1] for c in calls if c[0] == "0883.HK"][-1], "2023-08-01")
        row = self.store.rows("0883.HK", "2023-08-31", "2023-08-31")[0]
        self.assertAlmostEqual(row.adj_close, 9.9 * 0.95)

    def test_non_hk_ticker_is_reported_not_guessed(self):
        result = update_prices(self.store, [UniverseEntry("XOM")], fake_downloader(self.tables),
                               now=self.now, start="2023-08-01")
        summary = {t.ticker: t for t in result["tickers"]}
        self.assertIn("only Hong Kong", summary["XOM"].error)
        self.assertEqual(result["status"], "partial")

    def test_provider_raw_adj_and_minute(self):
        raw = LocalDbPriceProvider(self.db)
        adj = raw.with_basis("adj")
        start, end = hk(2023, 9, 4), hk(2023, 9, 4, 23)
        self.assertEqual([b.close for b in raw.bars("0883.HK", start, end, "day")], [10.0])
        self.assertEqual(adj.bars("0883.HK", start, end, "day")[0].basis, "adj")
        self.assertIn("local-db:yfinance", raw.bars("0883.HK", start, end, "day")[0].source)
        self.assertEqual(raw.bars("0883.HK", start, end, "minute"), [])
        with self.assertRaises(FileNotFoundError):
            LocalDbPriceProvider(Path(self.tmp.name) / "missing.sqlite3")

    def test_daily_report_raw_adj_excess_and_flags(self):
        provider = LocalDbPriceProvider(self.db)
        config = TradingWindowConfig(before_days=(1,), after_days=(0, 1, 2))
        report = daily_report(provider, resolve_asset("0883.HK"), hk(2023, 8, 31, 17, 0),
                              as_of=hk(2023, 10, 1), config=config)
        self.assertEqual(report["t0"], "2023-09-04")
        w = report["windows"]
        self.assertAlmostEqual(w["after_t0"]["return_pct"], (10.0 - 11.0) / 11.0 * 100)
        self.assertAlmostEqual(w["after_t0"]["adj_return_pct"], (10.0 - 9.9) / 9.9 * 100)
        self.assertEqual(w["after_t1"]["flag"], "zero_volume")
        self.assertEqual((w["after_t2"]["status"], w["after_t2"]["flag"]), ("deferred", "missing_data"))
        self.assertEqual(w["after_t2"]["observed_at"][:10], "2023-09-07")
        hsi = report["benchmarks"]["^HSI"]["windows"]
        self.assertAlmostEqual(hsi["after_t0"]["return_pct"], 2.0)
        self.assertAlmostEqual(hsi["after_t0"]["excess_return_pct"], (10.0 - 11.0) / 11.0 * 100 - 2.0)
        self.assertIsNone(hsi["after_t1"]["flag"])  # index volume is not flagged
        tracker = report["benchmarks"]["2800.HK"]["windows"]
        self.assertAlmostEqual(tracker["after_t0"]["adj_excess_return_pct"],
                               (10.0 - 9.9) / 9.9 * 100 - (18.36 - 18.0) / 18.0 * 100)
        # Stock observed 09-07 (deferred), tracker has 09-06: dates differ, no excess.
        self.assertIsNone(tracker["after_t2"]["excess_return_pct"])
        self.assertEqual(report["data_version"]["run_id"], self.result["run_id"])
        self.assertIn("local-db:yfinance", report["baseline"]["source"])

    def test_service_uses_store_instruments_and_aliases(self):
        provider = LocalDbPriceProvider(self.db)
        news = NewsItem("a1", "test", "中海油发布公告", "", hk(2023, 8, 31, 17, 0), "https://example.com", "zh")
        service = QueryService(lambda: provider, news_source=lambda: [news], now=lambda: hk(2023, 10, 1),
                               mode="daily", instruments=provider.store.instruments,
                               price_source="本地价格库", data_version=provider.data_version)
        tickers = [item["ticker"] for item in service.catalog()]
        self.assertIn("0883.HK", tickers)
        self.assertNotIn("^HSI", tickers)
        self.assertEqual(service.news("中海油")[0]["article_id"], "a1")
        payload = service.query("0883.HK", "2023-08-31T17:00:00+08:00")
        self.assertEqual(set(payload["benchmarks"]), {"2800.HK", "^HSI"})
        self.assertEqual(payload["mode"], "daily")
        self.assertEqual(service.source_info()["mode"], "daily")
        with self.assertRaisesRegex(ValueError, "本地价格库"):
            service.query("2318.HK", "2023-08-31T17:00:00+08:00")

    def test_server_builds_local_daily_service_by_default(self):
        from web.server import build_service

        args = argparse.Namespace(news_csv=None, provider="local", db=str(self.db), mode="daily",
                                  futu_host="127.0.0.1", futu_port=11111)
        service = build_service(args)
        self.assertEqual(service.mode, "daily")
        self.assertIn("本地价格库", service.price_source)
        args.db = str(Path(self.tmp.name) / "missing.sqlite3")
        with self.assertRaises(FileNotFoundError):
            build_service(args)


class UniverseTests(unittest.TestCase):
    def test_reads_codes_and_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pool.csv"
            path.write_text("代码,中文名,英文名,常用别名\n00883,中国海洋石油,CNOOC Limited,中海油;CNOOC\n"
                            "386,中国石化,,\n", encoding="utf-8")
            entries = read_universe(path)
            self.assertEqual([e.ticker for e in entries], ["0883.HK", "0386.HK"])
            self.assertEqual(entries[0].aliases, ("CNOOC Limited", "中海油", "CNOOC"))
            path.write_text("ticker\n0883.HK\n^HSI\n", encoding="utf-8")
            self.assertEqual([e.ticker for e in read_universe(path)], ["0883.HK", "^HSI"])
            path.write_text("foo\n1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_universe(path)

    def test_example_pool_in_repo_is_codes_only(self):
        path = Path(__file__).resolve().parents[1] / "news_price_query" / "data" / "universe_example_hk.csv"
        entries = read_universe(path)
        self.assertEqual([e.ticker for e in entries], ["0883.HK", "0857.HK", "0386.HK"])
        self.assertTrue(all(not e.aliases for e in entries))


if __name__ == "__main__":
    unittest.main()
