"""On-demand Yahoo fetch when a ticker is missing from the local price database."""

from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.price_store import LocalDbPriceProvider, PriceStore
from news_price_query.price_update import (
    ONDEMAND_SOURCE, UniverseEntry, ensure_ticker, normalize_ticker, ticker_has_hourly, update_prices,
)
from web.service import QueryService


HK = ZoneInfo("Asia/Hong_Kong")


def daily_row(day="2026-11-18", close=80.0):
    return {"date": day, "close": close, "adj_close": close, "volume": 1000}


def hourly_row(start="2026-11-19T09:30:00+08:00", close=80.5, volume=100):
    stamp = datetime.fromisoformat(start)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=HK)
    return {"timestamp": stamp, "open": close - 0.2, "high": close + 0.3,
            "low": close - 0.4, "close": close, "adj_close": close, "volume": volume}


class NormalizeTickerTests(unittest.TestCase):
    def test_bare_and_unpadded_hk_codes(self):
        self.assertEqual(normalize_ticker("9988"), "9988.HK")
        self.assertEqual(normalize_ticker("700.HK"), "0700.HK")
        self.assertEqual(normalize_ticker("883"), "0883.HK")
        self.assertEqual(normalize_ticker("0883.HK"), "0883.HK")
        self.assertEqual(normalize_ticker("AAPL"), "AAPL")

    def test_resolve_asset_accepts_bare_hk_number(self):
        asset = resolve_asset("9988")
        self.assertEqual(asset.ticker, "9988.HK")
        self.assertEqual(resolve_asset("700").ticker, "0700.HK")


class RecordingDownloader:
    def __init__(self, daily=None, hourly=None, daily_error=None, hourly_empty=False):
        self.daily_tables = daily or {}
        self.hourly_tables = hourly or {}
        self.daily_error = daily_error
        self.hourly_empty = hourly_empty
        self.daily_calls = []
        self.hourly_calls = []

    def daily(self, ticker, start, end):
        self.daily_calls.append((ticker, start, end))
        if self.daily_error:
            raise RuntimeError(self.daily_error)
        return list(self.daily_tables.get(ticker, []))

    def hourly(self, ticker, start, end):
        self.hourly_calls.append((ticker, start, end))
        if self.hourly_empty:
            return []
        return list(self.hourly_tables.get(ticker, []))


class EnsureTickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.now = datetime(2026, 11, 19, 18, 0, tzinfo=HK)
        self.feeds = RecordingDownloader(
            daily={"9988.HK": [daily_row("2026-11-18", 150.0), daily_row("2026-11-19", 151.0)]},
            hourly={"9988.HK": [
                hourly_row("2026-11-18T15:30:00+08:00", 150.0),
                hourly_row("2026-11-19T09:30:00+08:00", 150.5),
                hourly_row("2026-11-19T10:30:00+08:00", 151.0),
            ]},
        )

    def ensure(self, ticker="9988.HK", **kwargs):
        return ensure_ticker(
            self.store, ticker, now=self.now, downloader=self.feeds.daily,
            hourly_downloader=self.feeds.hourly, timeout=5, **kwargs)

    def test_fetches_missing_ticker_and_stores_with_ondemand_source(self):
        result = self.ensure("9988")
        self.assertTrue(result["fetched"])
        self.assertFalse(result["cached"])
        self.assertEqual(result["ticker"], "9988.HK")
        self.assertEqual(result["source"], ONDEMAND_SOURCE)
        self.assertTrue(ticker_has_hourly(self.store, "9988.HK"))
        rows = self.store.hourly_rows("9988.HK")
        self.assertGreaterEqual(len(rows), 2)
        self.assertEqual(rows[0].source, ONDEMAND_SOURCE)
        self.assertEqual(self.store.rows("9988.HK")[0].source, ONDEMAND_SOURCE)
        # Default benchmarks are not pulled in during a single-ticker on-demand fetch.
        self.assertEqual(self.store.hourly_coverage("2800.HK")["stored_rows"], 0)

    def test_second_call_uses_cache_and_does_not_refetch(self):
        self.ensure()
        self.feeds.daily_calls.clear()
        self.feeds.hourly_calls.clear()
        result = self.ensure()
        self.assertFalse(result["fetched"])
        self.assertTrue(result["cached"])
        self.assertEqual(self.feeds.daily_calls, [])
        self.assertEqual(self.feeds.hourly_calls, [])

    def test_concurrent_calls_share_one_fetch(self):
        errors = []

        def worker():
            try:
                self.ensure()
            except Exception as exc:  # noqa: BLE001 — collect any worker failure
                errors.append(exc)

        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual([call[0] for call in self.feeds.daily_calls], ["9988.HK"])
        self.assertEqual([call[0] for call in self.feeds.hourly_calls], ["9988.HK"])

    def test_empty_yahoo_response_is_missing_data_and_stores_nothing(self):
        self.feeds.hourly_empty = True
        self.feeds.daily_tables["NOPE.HK"] = []
        with self.assertRaisesRegex(ValueError, "缺数据"):
            self.ensure("NOPE.HK")
        self.assertFalse(ticker_has_hourly(self.store, "NOPE.HK"))
        self.assertEqual(self.store.rows("NOPE.HK"), [])

    def test_timeout_does_not_fabricate_rows(self):
        def hang(ticker, start, end):
            import time
            time.sleep(0.4)
            return [daily_row()]

        with self.assertRaisesRegex(RuntimeError, "超时"):
            ensure_ticker(self.store, "9988.HK", now=self.now, downloader=hang,
                          hourly_downloader=self.feeds.hourly, timeout=0.05)
        self.assertFalse(ticker_has_hourly(self.store, "9988.HK"))

    def test_include_defaults_still_adds_benchmarks_for_regular_updates(self):
        result = update_prices(
            self.store, [UniverseEntry("0883.HK")],
            lambda ticker, start, end: [daily_row()], now=self.now,
            hourly_downloader=lambda ticker, start, end: [hourly_row()],
            include_defaults=True)
        tickers = {item.ticker for item in result["hourly_tickers"]}
        self.assertIn("2800.HK", tickers)
        self.assertIn("^HSI", tickers)


class QueryServiceOndemandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.now = datetime(2026, 11, 19, 16, 0, tzinfo=HK)
        self.ensured = []

        def ensure(store, ticker, now=None, **kwargs):
            self.ensured.append(ticker)
            if ticker != "9988.HK":
                return {"ticker": ticker, "fetched": False, "cached": True}
            # Seed enough hourly bars around 10:30 for a real query after ensure.
            from news_price_query.price_store import HourlyRow, Instrument
            store.upsert_instrument(Instrument("9988.HK", "XHKG", "HKD", "阿里巴巴", ("阿里巴巴",), "equity"))
            rows = []
            start = datetime(2026, 11, 19, 9, 30, tzinfo=HK)
            for index in range(6):
                local = start + timedelta(hours=index)
                if local.hour == 12:
                    continue
                utc = local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
                close = 150.0 + index
                rows.append(HourlyRow(
                    "9988.HK", "XHKG", "HKD", "1h", utc, "Asia/Hong_Kong", local.isoformat(),
                    close - 0.2, close + 0.3, close - 0.4, close, close, 100,
                    ONDEMAND_SOURCE, "2026-11-19T18:00:00+08:00", "ondemand-test"))
            store.upsert_hourly_rows(rows)
            return {"ticker": ticker, "fetched": True, "cached": False, "source": ONDEMAND_SOURCE}

        self.ensure = ensure
        self.service = QueryService(
            lambda: LocalDbPriceProvider(store=self.store), news_source=lambda: [],
            now=lambda: self.now, mode="hourly", instruments=self.store.instruments,
            price_source="local-test", yahoo_ondemand=True, ondemand_ensure=ensure)

    def test_local_only_still_rejects_ticker_not_in_database(self):
        local = QueryService(
            lambda: LocalDbPriceProvider(store=self.store), news_source=lambda: [],
            now=lambda: self.now, mode="hourly", instruments=self.store.instruments)
        # 2318.HK is a valid unlisted HK ticker but is not in this empty store.
        with self.assertRaisesRegex(ValueError, "本地价格库"):
            local.query("2318.HK", "2026-11-19T10:30:00+08:00")

    def test_ondemand_query_fetches_missing_ticker_and_benchmarks(self):
        result = self.service.query("阿里巴巴", "2026-11-19T10:30:00+08:00")
        self.assertEqual(result["ticker"], "9988.HK")
        self.assertIn("9988.HK", self.ensured)
        self.assertIn("2800.HK", self.ensured)
        self.assertIn("^HSI", self.ensured)
        self.assertIsNotNone(result["baseline"]["price"])
        self.assertIn(ONDEMAND_SOURCE, result["baseline"].get("source_detail") or result["baseline"]["source"])
        after = result["windows"]["after_1th"]
        self.assertEqual(after["status"], "on_time")
        # Baseline is the 09:30 bar close 150.0; after 1h is the 10:30 bar close 151.0.
        self.assertAlmostEqual(after["price"], 151.0)
        self.assertAlmostEqual(after["return_pct"], (151.0 / 150.0 - 1) * 100)

    def test_ondemand_allows_typing_any_us_ticker(self):
        asset = self.service._asset("AAPL")
        self.assertEqual(asset.ticker, "AAPL")
        self.assertEqual(asset.market, "XNYS")

    def test_bars_endpoint_triggers_ensure(self):
        payload = self.service.bars("9988", "1h", "2026-11-19", "2026-11-19")
        self.assertIn("9988.HK", self.ensured)
        self.assertGreaterEqual(payload["bar_count"], 1)
        self.assertFalse(payload["missing"])

    def test_source_info_advertises_flag(self):
        self.assertTrue(self.service.source_info()["yahoo_ondemand"])
        off = QueryService(lambda: None, news_source=lambda: [], mode="minute")
        self.assertFalse(off.source_info()["yahoo_ondemand"])


if __name__ == "__main__":
    unittest.main()
