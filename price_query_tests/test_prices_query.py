import csv
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.prices import CsvPriceProvider, FutuPriceProvider
from news_price_query.query import query_event


HK = ZoneInfo("Asia/Hong_Kong")


class PriceQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "prices.csv"
        rows = [
            ("1211.HK", "day", "2026-03-27", "70"),
            ("1211.HK", "day", "2026-06-26", "80"),
            ("1211.HK", "day", "2026-08-28", "90"),
            ("1211.HK", "minute", "2026-09-28T09:59:00+08:00", "100"),
            ("1211.HK", "minute", "2026-09-28T10:00:00+08:00", "999"),
            ("1211.HK", "minute", "2026-09-28T11:00:00+08:00", "102"),
            ("1211.HK", "minute", "2026-09-28T13:00:00+08:00", "105"),
            ("1211.HK", "minute", "2026-09-29T09:30:00+08:00", "110"),
            ("1211.HK", "minute", "2026-09-29T10:00:00+08:00", "111"),
            ("1211.HK", "minute", "2026-10-02T09:30:00+08:00", "115"),
            ("1211.HK", "minute", "2026-10-05T10:00:00+08:00", "120"),
        ]
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close"))
            writer.writerows(rows)
        self.provider = CsvPriceProvider(self.path)
        self.published = datetime(2026, 9, 28, 10, 0, tzinfo=HK)

    def test_csv_provider_filters_interval_and_preserves_source(self):
        rows = self.provider.bars(
            "1211.HK",
            datetime(2026, 9, 28, 9, 59, tzinfo=HK),
            datetime(2026, 9, 28, 10, 0, tzinfo=HK),
            "minute",
        )
        self.assertEqual([bar.close for bar in rows], [100.0, 999.0])
        self.assertEqual(rows[0].source, "csv:prices.csv")
        self.assertEqual(rows[0].basis, "raw")

    def test_query_uses_strict_pre_news_price_and_reports_each_window(self):
        result = query_event(
            self.provider, resolve_asset("BYD"), self.published,
            as_of=datetime(2026, 10, 10, 12, 0, tzinfo=HK),
        )
        self.assertEqual(result.baseline.price, 100.0)
        self.assertEqual(result.baseline.observed_at.isoformat(), "2026-09-28T09:59:00+08:00")
        windows = {item.label: item for item in result.windows}
        self.assertEqual(len(windows), 9)
        self.assertEqual(windows["before_1m"].price, 90.0)
        self.assertAlmostEqual(windows["before_1m"].return_pct, 11.1111111)
        self.assertEqual(windows["after_1h"].price, 102.0)
        self.assertEqual(windows["after_1h"].status, "on_time")
        self.assertEqual(windows["after_1h"].return_pct, 2.0)
        self.assertEqual(windows["after_12h"].price, 110.0)
        self.assertEqual(windows["after_12h"].status, "deferred")
        self.assertEqual(windows["after_12h"].delay_seconds, 41400)
        self.assertEqual(windows["after_3d"].status, "deferred")
        self.assertEqual(windows["after_1w"].price, 120.0)

    def test_partial_minute_cannot_supply_pre_news_close(self):
        result = query_event(
            self.provider, resolve_asset("BYD"),
            datetime(2026, 9, 28, 10, 0, 30, tzinfo=HK),
            as_of=datetime(2026, 9, 28, 12, 0, tzinfo=HK),
        )
        self.assertEqual(result.baseline.price, 100.0)
        self.assertEqual(result.baseline_gap_seconds, 30)
        self.assertEqual(result.baseline_status, "recent")

    def test_cutoff_does_not_expose_unfinished_minute_close(self):
        result = query_event(
            self.provider, resolve_asset("BYD"), self.published,
            as_of=datetime(2026, 9, 28, 11, 0, 30, tzinfo=HK),
        )
        one_hour = next(item for item in result.windows if item.label == "after_1h")
        self.assertEqual(one_hour.status, "missing")

    def test_old_baseline_is_flagged(self):
        result = query_event(
            self.provider, resolve_asset("BYD"),
            datetime(2026, 9, 28, 10, 5, tzinfo=HK),
            as_of=datetime(2026, 9, 28, 12, 0, tzinfo=HK),
        )
        self.assertEqual(result.baseline_status, "stale")

    def test_future_window_is_pending_while_eligible_past_window_can_be_missing(self):
        result = query_event(
            self.provider, resolve_asset("1211.HK"), self.published,
            as_of=datetime(2026, 9, 28, 12, 0, tzinfo=HK),
        )
        windows = {item.label: item for item in result.windows}
        self.assertEqual(windows["after_1h"].status, "on_time")
        self.assertEqual(windows["after_3h"].status, "pending")

    def test_missing_baseline_raises_clear_error(self):
        with self.assertRaisesRegex(ValueError, "baseline"):
            query_event(
                self.provider, resolve_asset("BYD"),
                datetime(2026, 1, 1, 10, 0, tzinfo=HK),
                as_of=datetime(2026, 10, 10, tzinfo=HK),
            )

    def test_elapsed_window_with_no_market_bar_is_missing_not_zero(self):
        path = Path(self.tmp.name) / "sparse.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close"))
            writer.writerow(("1211.HK", "minute", "2026-09-28T09:59:00+08:00", 100))
        result = query_event(
            CsvPriceProvider(path), resolve_asset("BYD"), self.published,
            as_of=datetime(2026, 9, 29, 12, 0, tzinfo=HK),
        )
        one_hour = next(item for item in result.windows if item.label == "after_1h")
        self.assertEqual(one_hour.status, "missing")
        self.assertIsNone(one_hour.return_pct)

    def test_mixed_raw_and_adjusted_bars_cannot_form_a_return(self):
        path = Path(self.tmp.name) / "mixed.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close", "basis"))
            writer.writerow(("1211.HK", "minute", "2026-09-28T09:59:00+08:00", 100, "raw"))
            writer.writerow(("1211.HK", "day", "2026-08-28", 90, "adjusted"))
        with self.assertRaisesRegex(ValueError, "price basis"):
            query_event(
                CsvPriceProvider(path), resolve_asset("BYD"), self.published,
                as_of=datetime(2026, 9, 29, 12, 0, tzinfo=HK),
            )

    def test_duplicate_minute_bar_is_rejected_before_matching(self):
        path = Path(self.tmp.name) / "duplicate.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close"))
            writer.writerow(("1211.HK", "minute", "2026-09-28T09:59:00+08:00", 100))
            writer.writerow(("1211.HK", "minute", "2026-09-28T09:59:00+08:00", 999))
        with self.assertRaisesRegex(ValueError, "Duplicate price bar"):
            CsvPriceProvider(path)

    def test_non_finite_close_is_rejected(self):
        path = Path(self.tmp.name) / "nan.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close"))
            writer.writerow(("1211.HK", "minute", "2026-09-28T09:59:00+08:00", "nan"))
        with self.assertRaisesRegex(ValueError, "finite"):
            CsvPriceProvider(path)

    def test_futu_adapter_maps_code_and_paginates(self):
        class Rows:
            def __init__(self, rows):
                self.rows = rows

            def iterrows(self):
                return enumerate(self.rows)

        class Context:
            def __init__(self):
                self.calls = []

            def request_history_kline(self, code, **kwargs):
                self.calls.append((code, kwargs))
                if kwargs["page_req_key"] is None:
                    return 0, Rows([{"time_key": "2026-09-28 09:59:00", "close": 100}]), "next"
                return 0, Rows([{"time_key": "2026-09-28 10:00:00", "close": 101}]), None

        fake_futu = SimpleNamespace(
            KLType=SimpleNamespace(K_1M="K_1M", K_DAY="K_DAY"),
            AuType=SimpleNamespace(NONE="NONE"), RET_OK=0,
        )
        context = Context()
        with patch.dict(sys.modules, {"futu": fake_futu}):
            provider = FutuPriceProvider(context=context)
            rows = provider.bars(
                "1211.HK", datetime(2026, 9, 28, 9, 59, tzinfo=HK),
                datetime(2026, 9, 28, 10, 0, tzinfo=HK), "minute",
            )
        self.assertEqual([row.close for row in rows], [100, 101])
        self.assertEqual([call[0] for call in context.calls], ["HK.01211", "HK.01211"])
        self.assertEqual([call[1]["page_req_key"] for call in context.calls], [None, "next"])


if __name__ == "__main__":
    unittest.main()
