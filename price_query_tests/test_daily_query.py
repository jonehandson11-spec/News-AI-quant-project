import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.cli import main
from news_price_query.prices import CsvPriceProvider
from news_price_query.query import query_event
from news_price_query.windows import TradingWindowConfig
from web.service import QueryService


HK = ZoneInfo("Asia/Hong_Kong")
CONFIG = TradingWindowConfig(before_days=(1, 5), after_days=(1, 3, 5))

# Synthetic fixture prices (not market data).
ROWS = [
    ("1211.HK", "day", "2023-08-23", "90"),
    ("1211.HK", "day", "2023-08-30", "95"),
    ("1211.HK", "day", "2023-08-31", "100"),
    ("1211.HK", "day", "2023-09-01", "999"),   # typhoon closure: filler row must be ignored
    ("1211.HK", "day", "2023-09-04", "101"),
    ("1211.HK", "day", "2023-09-05", "102"),
    ("1211.HK", "day", "2023-09-11", "105"),   # 09-07 missing, 09-08 closed
    ("1211.HK", "day", "2023-09-12", "106"),
    ("2800.HK", "day", "2023-08-30", "19.0"),
    ("2800.HK", "day", "2023-08-31", "20.0"),
    ("2800.HK", "day", "2023-09-04", "20.2"),
    ("1211.HK", "day", "2025-12-23", "50"),
    ("1211.HK", "day", "2025-12-24", "51"),
    ("1211.HK", "day", "2025-12-29", "52"),
    ("1211.HK", "minute", "2023-08-31T15:59:00+08:00", "100"),
]


def hk(*parts):
    return datetime(*parts, tzinfo=HK)


class DailyQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "prices.csv"
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close"))
            writer.writerows(ROWS)
        self.provider = CsvPriceProvider(self.path)
        self.byd = resolve_asset("BYD")

    def run_query(self, published, as_of, config=CONFIG):
        return query_event(self.provider, self.byd, published, as_of=as_of, mode="daily", config=config)

    def test_after_close_before_typhoon(self):
        result = self.run_query(hk(2023, 8, 31, 17, 0), hk(2023, 9, 20, 12, 0))
        self.assertEqual(result.t0, date(2023, 9, 4))
        self.assertEqual(result.session, "after_close")
        self.assertEqual(result.baseline.price, 100.0)
        self.assertEqual(result.baseline.observed_at, hk(2023, 8, 31, 16, 0))
        self.assertEqual(result.baseline_status, "on_date")
        w = {item.label: item for item in result.windows}
        self.assertEqual((w["after_t1"].observed_at.date(), w["after_t1"].price, w["after_t1"].status),
                         (date(2023, 9, 5), 102.0, "on_date"))
        self.assertAlmostEqual(w["after_t1"].return_pct, 2.0)
        self.assertEqual((w["after_t3"].observed_at.date(), w["after_t3"].status),
                         (date(2023, 9, 11), "deferred"))
        self.assertAlmostEqual(w["after_t3"].return_pct, 5.0)
        self.assertEqual(w["after_t5"].status, "on_date")
        self.assertAlmostEqual(w["after_t5"].return_pct, 6.0)
        self.assertEqual(w["before_1td"].observed_at.date(), date(2023, 8, 30))
        self.assertAlmostEqual(w["before_1td"].return_pct, (100 - 95) / 95 * 100)
        self.assertEqual((w["before_5td"].observed_at.date(), w["before_5td"].status),
                         (date(2023, 8, 23), "deferred_back"))

    def test_news_on_typhoon_day_uses_next_session(self):
        result = self.run_query(hk(2023, 9, 1, 10, 0), hk(2023, 9, 20, 12, 0))
        self.assertEqual(result.session, "non_trading_day")
        self.assertEqual(result.t0, date(2023, 9, 4))
        self.assertEqual(result.baseline.price, 100.0)  # not the 999 filler row

    def test_pending_windows(self):
        result = self.run_query(hk(2023, 8, 31, 17, 0), hk(2023, 9, 6, 12, 0))
        w = {item.label: item for item in result.windows}
        self.assertEqual(w["after_t1"].status, "on_date")
        self.assertEqual(w["after_t3"].status, "pending")
        self.assertEqual(w["after_t5"].status, "pending")
        self.assertIsNone(w["after_t5"].return_pct)

    def test_missing_is_not_zero(self):
        config = TradingWindowConfig(before_days=(1,), after_days=(3,), max_gap_days=1)
        result = self.run_query(hk(2023, 8, 31, 17, 0), hk(2023, 10, 30, 12, 0), config)
        w = {item.label: item for item in result.windows}
        # t+3 = 09-07 missing; one-day fill reaches 09-11 (09-08 closed) which exists.
        self.assertEqual(w["after_t3"].status, "deferred")
        config = TradingWindowConfig(before_days=(1,), after_days=(3,), max_gap_days=0)
        result = self.run_query(hk(2023, 8, 31, 17, 0), hk(2023, 10, 30, 12, 0), config)
        w = {item.label: item for item in result.windows}
        self.assertEqual(w["after_t3"].status, "missing")
        self.assertIsNone(w["after_t3"].return_pct)
        self.assertIsNone(w["after_t3"].price)

    def test_half_day_baseline_close_at_noon(self):
        result = self.run_query(hk(2025, 12, 24, 13, 0), hk(2026, 1, 20, 12, 0),
                                TradingWindowConfig(before_days=(1,), after_days=(1,)))
        self.assertEqual(result.session, "after_close")
        self.assertEqual(result.baseline.observed_at, hk(2025, 12, 24, 12, 0))
        self.assertEqual(result.t0, date(2025, 12, 29))
        w = {item.label: item for item in result.windows}
        self.assertEqual(w["before_1td"].price, 50.0)
        self.assertEqual(w["after_t1"].status, "missing")  # 12-30 onwards not in fixture

    def test_no_baseline_raises(self):
        with self.assertRaises(ValueError):
            self.run_query(hk(2024, 6, 3, 10, 0), hk(2024, 7, 1, 12, 0))

    def test_minute_mode_is_default(self):
        hourly = query_event(self.provider, self.byd, hk(2023, 8, 31, 17, 0), as_of=hk(2023, 9, 20), mode="hourly")
        self.assertEqual(hourly.mode, "hourly")
        result = query_event(self.provider, self.byd, hk(2023, 8, 31, 16, 0), as_of=hk(2023, 9, 20))
        self.assertEqual(result.mode, "minute")
        self.assertEqual(result.windows[0].label, "before_1m")

    def test_cli_daily_with_benchmark(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["query", "--asset", "BYD", "--time", "2023-08-31T17:00:00+08:00",
                         "--as-of", "2023-09-20T12:00:00+08:00", "--prices", str(self.path),
                         "--provider", "csv", "--mode", "daily", "--before-days", "1", "--after-days", "1",
                         "--benchmark", "2800.HK"])
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["t0"], "2023-09-04")
        self.assertEqual(payload["mode"], "daily")
        window = payload["windows"]["after_t1"]
        self.assertEqual(window["label_zh"], "事件后第 1 个交易日（t+1）")
        self.assertAlmostEqual(window["return_pct"], 2.0)
        self.assertEqual(window["benchmark_return_pct"], None)  # 2800 has no 09-05 bar
        before = payload["windows"]["before_1td"]
        self.assertAlmostEqual(before["excess_return_pct"],
                               (100 - 95) / 95 * 100 - (20.0 - 19.0) / 19.0 * 100)

    def test_service_daily_mode(self):
        service = QueryService(lambda: self.provider, news_source=lambda: [],
                               now=lambda: hk(2023, 9, 20, 12, 0), mode="daily")
        payload = service.query("BYD", "2023-08-31T17:00:00+08:00", benchmarks=["2800.HK"])
        self.assertEqual(list(payload["windows"])[-1], "after_t5")
        self.assertEqual(payload["windows"]["after_t1"]["price_change"], 2.0)
        self.assertIn("2800.HK", payload["benchmarks"])
        with self.assertRaises(ValueError):
            QueryService(lambda: self.provider, mode="weekly")


if __name__ == "__main__":
    unittest.main()
