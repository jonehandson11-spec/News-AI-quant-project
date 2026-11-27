from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.intraday_sync import IntradayUnavailable, minute_bar_end, sync_intraday
from news_price_query.market_calendars import calendar_for
from news_price_query.price_store import PriceStore


HK = ZoneInfo("Asia/Hong_Kong")


def hk(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=HK)


def candle(stamp, close=100.05):
    return {"timestamp": stamp, "open": 100, "high": 101, "low": 99,
            "close": close, "volume": 35}


class IntradaySyncTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PriceStore(Path(directory.name) / "market.sqlite3")
        self.asset = resolve_asset("0700.HK")
        self.now = hk(27, 14)

    def test_filter_incomplete_before_cutoff_and_outside_session(self):
        source_rows = [candle(hk(27, 9, 29)), candle(hk(27, 9, 30)),
                       candle(hk(27, 12, 0)), candle(hk(27, 13)),
                       candle(hk(27, 13, 5), close=float("nan")), candle(hk(27, 13, 10))]
        count = sync_intraday(self.store, self.asset, "5m", hk(27, 9), hk(27, 13, 13),
                              now=self.now, downloader=lambda *_: source_rows)
        self.assertEqual(count, 2)
        self.assertEqual(self.store.hourly_rows(self.asset.ticker), [])
        rows = self.store.hourly_rows(self.asset.ticker, interval="5m")
        self.assertEqual([row.bar_start_utc for row in rows],
                         ["2026-11-27T01:30:00Z", "2026-11-27T05:00:00Z"])
        self.assertEqual(rows[1].bar_start_local, "2026-11-27T13:00:00+08:00")
        self.assertEqual(rows[1].exchange_tz, "Asia/Hong_Kong")
        self.assertEqual(rows[1].source, "yfinance-ondemand")
        self.assertEqual(self.store.hourly_coverage(self.asset.ticker, interval="5m")["stored_rows"], 2)
        self.assertEqual(self.store.hourly_coverage(self.asset.ticker)["stored_rows"], 0)

    def test_one_minute_requests_chunk_within_retention_without_stale_fetches(self):
        calls = []

        def download(ticker, interval, start, end):
            calls.append((ticker, interval, start, end))
            return []

        old = self.now - timedelta(days=40)
        self.assertEqual(sync_intraday(self.store, self.asset, "1m", old, old + timedelta(days=1),
                                       now=self.now, downloader=download), 0)
        self.assertEqual(calls, [])
        self.assertEqual(sync_intraday(self.store, self.asset, "1m", old, self.now,
                                       now=self.now, downloader=download), 0)
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[0][2], (self.now - timedelta(days=30, minutes=-1)).astimezone(timezone.utc))
        self.assertEqual(calls[-1][3], self.now)
        self.assertTrue(all(timedelta(0) < end - start <= timedelta(days=7)
                            for _, _, start, end in calls))

    def test_failed_download_leaves_no_partial_cache(self):
        def download(*_):
            yield candle(hk(27, 9, 30))
            raise OSError("network provider failed")

        with self.assertRaises(IntradayUnavailable):
            sync_intraday(self.store, self.asset, "1m", hk(27, 9), hk(27, 11),
                          now=self.now, downloader=download)
        self.assertEqual(self.store.hourly_rows(self.asset.ticker, interval="1m"), [])
        self.assertIsNone(self.store.latest_run())

    def test_nymex_evening_and_hk_lunch_completion(self):
        calendar = calendar_for("NYMEX")
        sunday_evening = datetime(2026, 11, 22, 18, 0, tzinfo=calendar.tz)
        self.assertEqual(minute_bar_end(sunday_evening, calendar, "5m"),
                         sunday_evening + timedelta(minutes=5))
        self.assertIsNone(minute_bar_end(hk(27, 12, 30), calendar_for("XHKG"), "30m"))
