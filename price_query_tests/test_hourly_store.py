import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.price_store import PriceStore
from news_price_query.price_update import UniverseEntry, update_prices


HK = ZoneInfo("Asia/Hong_Kong")
NY = ZoneInfo("America/New_York")


class HourlyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.now = datetime(2026, 10, 22, 18, 0, tzinfo=HK)
        self.data = [{"timestamp": datetime(2026, 10, 22, 9, 30, tzinfo=HK),
                      "open": 10.123456, "high": 11.0, "low": 9.0, "close": 10.5,
                      "adj_close": 10.4, "volume": 100},
                     {"timestamp": datetime(2026, 10, 22, 17, 0, tzinfo=HK),
                      "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 100}]

    def update(self, downloader, calls=None):
        return update_prices(self.store, [UniverseEntry("0883.HK")], None, now=self.now,
                             hourly_downloader=downloader, update_daily=False)

    def test_hourly_upsert_is_idempotent(self):
        self.update(lambda ticker, start, end: self.data[:1])
        self.update(lambda ticker, start, end: self.data[:1])
        rows = self.store.hourly_rows("0883.HK")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].close, 10.5)
        self.assertEqual(rows[0].source, "yfinance")

    def test_incremental_fetch_starts_five_days_before_last_bar(self):
        calls = []
        downloader = lambda ticker, start, end: (calls.append((ticker, start, end)) or self.data[:1])
        self.update(downloader)
        calls.clear()
        self.update(downloader)
        ticker_call = next(call for call in calls if call[0] == "0883.HK")
        self.assertEqual(ticker_call[1], "2026-10-17")

    def test_one_ticker_error_does_not_stop_others(self):
        def downloader(ticker, start, end):
            if ticker == "0883.HK":
                raise RuntimeError("temporary failure")
            return self.data[:1]
        result = self.update(downloader)
        summaries = {row.ticker: row for row in result["hourly_tickers"]}
        self.assertEqual(summaries["0883.HK"].error, "temporary failure")
        self.assertEqual(self.store.hourly_coverage("2800.HK")["stored_rows"], 1)
        self.assertEqual(result["status"], "ok")

    def test_unfinished_bar_is_dropped(self):
        unfinished = {**self.data[1], "timestamp": datetime(2026, 10, 22, 17, 30, tzinfo=HK)}
        result = self.update(lambda ticker, start, end: [self.data[0], unfinished])
        self.assertEqual(len(self.store.hourly_rows("0883.HK")), 1)
        self.assertEqual(result["hourly_tickers"][0].rows_written, 1)

    def test_nymex_evening_and_sunday_bars_are_kept_but_unfinished_is_dropped(self):
        now = datetime(2026, 10, 26, 12, 0, tzinfo=NY)
        bars = [
            {"timestamp": datetime(2026, 10, 23, 18, 0, tzinfo=NY),
             "open": 60, "high": 61, "low": 59, "close": 60.5},
            {"timestamp": datetime(2026, 10, 25, 18, 0, tzinfo=NY),
             "open": 61, "high": 62, "low": 60, "close": 61.5},
            {"timestamp": datetime(2026, 10, 26, 11, 30, tzinfo=NY),
             "open": 62, "high": 63, "low": 61, "close": 62.5},
        ]
        result = update_prices(
            self.store, [UniverseEntry("CL=F")], None, now=now,
            hourly_downloader=lambda ticker, start, end: bars, update_daily=False)
        rows = self.store.hourly_rows("CL=F")
        self.assertEqual(result["hourly_tickers"][0].rows_written, 2)
        self.assertEqual([row.bar_start_utc for row in rows],
                         ["2026-10-23T22:00:00Z", "2026-10-25T22:00:00Z"])

    def test_hong_kong_bar_time_is_stored_as_utc_and_local(self):
        self.update(lambda ticker, start, end: self.data[:1])
        row = self.store.hourly_rows("0883.HK")[0]
        self.assertEqual(row.bar_start_utc, "2026-10-22T01:30:00Z")
        self.assertEqual(row.bar_start_local, "2026-10-22T09:30:00+08:00")
        self.assertEqual(row.exchange_tz, "Asia/Hong_Kong")


if __name__ == "__main__":
    unittest.main()
