"""Tests for the K-line bars endpoint and local-DB aggregation."""

from datetime import datetime
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.chart_bars import MAX_BARS, MAX_SPAN, chart_bars, normalize_interval
from news_price_query.price_store import DailyRow, HourlyRow, LocalDbPriceProvider, PriceStore
from news_price_query.prices import PriceBar
from web.server import allowed_static_path, create_server
from web.service import QueryService


HK = ZoneInfo("Asia/Hong_Kong")
ASSET = resolve_asset("0883.HK")


def hourly(start_local, open_, high, low, close, volume, ticker="0883.HK"):
    local = start_local if isinstance(start_local, datetime) else datetime.fromisoformat(start_local)
    if local.tzinfo is None:
        local = local.replace(tzinfo=HK)
    utc = local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    return HourlyRow(
        ticker=ticker, market="XHKG", currency="HKD", interval="1h",
        bar_start_utc=utc, exchange_tz="Asia/Hong_Kong",
        bar_start_local=local.isoformat(),
        open=open_, high=high, low=low, close=close, adj_close=close,
        volume=volume, source="yfinance", fetched_at="2026-11-25T00:00:00+08:00",
        run_id="test-run",
    )


class ChartBarsUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.now = datetime(2026, 11, 25, 18, 0, tzinfo=HK)
        self.store.start_run("test", None, 1, self.now)
        # Monday 28 Sep 2026: three hours, then Tuesday 29 Sep one hour.
        # No Saturday 26 / Sunday 27 bars — those days must stay absent.
        self.store.upsert_hourly_rows([
            hourly("2026-09-28T09:30:00+08:00", 23.62, 24.14, 23.58, 23.78, 100),
            hourly("2026-09-28T10:30:00+08:00", 23.78, 23.90, 23.70, 23.78, 50),
            hourly("2026-09-28T15:30:00+08:00", 23.64, 23.72, 23.52, 23.68, 25),
            hourly("2026-09-29T09:30:00+08:00", 23.70, 23.80, 23.60, 23.75, 40),
        ])
        # A close-only daily row on Friday 25 Sep, with no hourly bars that day.
        self.store.upsert_rows([
            DailyRow("0883.HK", "XHKG", "HKD", "2026-09-25", 23.50, 23.50, 999,
                     "yfinance", "2026-11-25T00:00:00+08:00", "test-run"),
            DailyRow("0883.HK", "XHKG", "HKD", "2026-09-28", 23.68, 23.68, 175,
                     "yfinance", "2026-11-25T00:00:00+08:00", "test-run"),
        ])

    def test_hourly_bars_match_stored_ohlc_and_leave_weekend_gaps(self):
        payload = chart_bars(self.store, ASSET, "1h", "2026-09-25", "2026-09-29")
        times = [bar["time"] for bar in payload["bars"]]
        self.assertEqual(times, [
            "2026-09-28T09:30:00+08:00",
            "2026-09-28T10:30:00+08:00",
            "2026-09-28T15:30:00+08:00",
            "2026-09-29T09:30:00+08:00",
        ])
        bar = payload["bars"][1]
        self.assertEqual(bar["open"], 23.78)
        self.assertEqual(bar["high"], 23.90)
        self.assertEqual(bar["low"], 23.70)
        self.assertEqual(bar["close"], 23.78)
        self.assertEqual(bar["volume"], 50)
        self.assertFalse(any("2026-09-26" in time or "2026-09-27" in time for time in times))
        self.assertEqual(payload["exchange_timezone"], "Asia/Hong_Kong")
        self.assertEqual(payload["time_label"], "HKT")
        self.assertFalse(payload["missing"])

    def test_daily_bars_are_aggregated_from_hourly_and_do_not_invent_close_only_days(self):
        payload = chart_bars(self.store, ASSET, "1d", "2026-09-25", "2026-09-29")
        self.assertEqual(payload["source"], "hourly_aggregate")
        self.assertEqual([bar["time"] for bar in payload["bars"]], ["2026-09-28", "2026-09-29"])
        monday = payload["bars"][0]
        self.assertEqual(monday["open"], 23.62)
        self.assertEqual(monday["high"], 24.14)
        self.assertEqual(monday["low"], 23.52)
        self.assertEqual(monday["close"], 23.68)
        self.assertEqual(monday["volume"], 175)
        self.assertEqual(monday["hourly_bars"], 3)
        # Friday has a daily close in the store but no hourly bars: must not appear.
        self.assertNotIn("2026-09-25", [bar["time"] for bar in payload["bars"]])

    def test_empty_range_returns_missing_message_without_invented_bars(self):
        payload = chart_bars(self.store, ASSET, "1h", "2020-01-01", "2020-01-07")
        self.assertEqual(payload["bars"], [])
        self.assertTrue(payload["missing"])
        self.assertIn("缺数据", payload["message"])

    def test_rejects_inverted_range_unknown_interval_and_overlong_span(self):
        with self.assertRaisesRegex(ValueError, "开始时间不能晚于结束时间"):
            chart_bars(self.store, ASSET, "1h", "2026-09-29", "2026-09-28")
        with self.assertRaisesRegex(ValueError, "K线周期只支持"):
            chart_bars(self.store, ASSET, "10m", "2026-09-28", "2026-09-29")
        with self.assertRaisesRegex(ValueError, "最多查询"):
            chart_bars(self.store, ASSET, "1h", "2020-01-01", "2023-01-01")

    def test_interval_aliases_and_limits_are_documented(self):
        self.assertEqual(normalize_interval("hourly"), "1h")
        self.assertEqual(normalize_interval("日线" if False else "daily"), "1d")
        self.assertEqual(MAX_SPAN["1h"].days, 800)
        self.assertEqual(MAX_SPAN["1d"].days, 4000)
        self.assertEqual(MAX_BARS, 8000)


class MinuteProvider:
    def bars(self, ticker, start, end, interval):
        return [PriceBar("0700.HK", "minute", datetime(2026, 9, 28, 9, 59, tzinfo=HK), 100, "fixture")]


class BarsHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.store.upsert_hourly_rows([
            hourly("2026-09-28T10:30:00+08:00", 23.78, 23.90, 23.70, 23.78, 11866000),
        ])
        provider = LocalDbPriceProvider(store=self.store)
        service = QueryService(lambda: provider, news_source=lambda: [],
                               now=lambda: datetime(2026, 9, 28, 12, tzinfo=HK),
                               mode="hourly", instruments=self.store.instruments,
                               price_source="local-test")
        self.server = create_server(service, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def get_json(self, path):
        self.connection.request("GET", path)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read())

    def test_bars_endpoint_returns_hourly_ohlc(self):
        status, body = self.get_json(
            "/api/bars?symbol=0883.HK&interval=1h&start=2026-09-28&end=2026-09-28")
        self.assertEqual(status, 200)
        self.assertEqual(body["bar_count"], 1)
        bar = body["bars"][0]
        self.assertEqual(bar["open"], 23.78)
        self.assertEqual(bar["high"], 23.90)
        self.assertEqual(bar["low"], 23.70)
        self.assertEqual(bar["close"], 23.78)
        self.assertEqual(bar["volume"], 11866000)
        self.assertTrue(bar["time"].startswith("2026-09-28T10:30:00"))

    def test_bars_endpoint_validates_query_and_unknown_symbol(self):
        status, body = self.get_json("/api/bars?interval=1h&start=2026-09-28&end=2026-09-28")
        self.assertEqual(status, 400)
        self.assertIn("symbol", body["error"])
        status, body = self.get_json(
            "/api/bars?symbol=NOPE.HK&interval=1h&start=2026-09-28&end=2026-09-28")
        self.assertEqual(status, 400)

    def test_vendor_script_is_served_and_python_sources_are_not(self):
        self.connection.request("GET", "/chart.mjs")
        chart = self.connection.getresponse()
        self.assertEqual(chart.status, 200)
        self.assertEqual(chart.headers.get("Content-Type"), "text/javascript")
        chart.read()
        self.connection.request(
            "GET", "/vendor/lightweight-charts/lightweight-charts.standalone.production.mjs")
        vendor = self.connection.getresponse()
        self.assertEqual(vendor.status, 200)
        self.assertEqual(vendor.headers.get("Content-Type"), "text/javascript")
        vendor.read()
        self.connection.request("GET", "/vendor/lightweight-charts/../../service.py")
        leaked = self.connection.getresponse()
        self.assertEqual(leaked.status, 404)
        leaked.read()
        self.assertFalse(allowed_static_path("/vendor/../service.py"))
        self.assertFalse(allowed_static_path("/service.py"))


class BarsRequiresLocalStoreTests(unittest.TestCase):
    def setUp(self):
        service = QueryService(MinuteProvider, news_source=lambda: [],
                               now=lambda: datetime(2026, 9, 28, 12, tzinfo=HK), mode="minute")
        self.server = create_server(service, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def test_futu_or_minute_provider_rejects_kline_request(self):
        self.connection.request(
            "GET", "/api/bars?symbol=0700.HK&interval=1h&start=2026-09-28&end=2026-09-28")
        response = self.connection.getresponse()
        body = json.loads(response.read())
        self.assertEqual(response.status, 400)
        self.assertIn("本地价格库", body["error"])


if __name__ == "__main__":
    unittest.main()
