"""Synthetic hourly window fixtures; these prices are deliberately not market data."""
import unittest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.market_calendars import calendar_for
from news_price_query.prices import PriceBar
from news_price_query.query import query_event
from news_price_query.daily_report import hourly_report
from news_price_query.windows import shift_trading_hours, trading_seconds_between
from news_price_query.price_store import (DailyRow, HourlyRow, LocalDbPriceProvider,
                                         PriceStore)

HK = ZoneInfo("Asia/Hong_Kong")


class SyntheticProvider:
    def __init__(self, bars): self.data = bars
    def bars(self, ticker, start, end, interval):
        return [b for b in self.data if b.ticker == ticker and b.interval == interval and start <= b.timestamp <= end]


def bar(ticker, stamp, n, *, volume=100):
    return PriceBar(ticker, "hour", datetime.fromisoformat(stamp), 100 + n,
                    "synthetic fixture (not real prices)", "raw", volume)


class HourlyWindowTests(unittest.TestCase):
    def test_hk_lunch_is_skipped(self):
        cal = calendar_for("XHKG")
        base = datetime(2026, 9, 1, 12, 0, tzinfo=HK)
        self.assertEqual(shift_trading_hours(base, 1, cal), datetime(2026, 9, 1, 14, 0, tzinfo=HK))

    def test_after_close_rolls_to_next_open(self):
        self.assertEqual(shift_trading_hours(datetime(2026, 9, 1, 16, tzinfo=HK), 1, calendar_for("XHKG")),
                         datetime(2026, 9, 2, 10, 30, tzinfo=HK))

    def test_weekend_and_closure_are_skipped(self):
        cal = calendar_for("XHKG")
        self.assertEqual(shift_trading_hours(datetime(2026, 9, 4, 16, tzinfo=HK), 1, cal),
                         datetime(2026, 9, 7, 10, 30, tzinfo=HK))
        self.assertEqual(shift_trading_hours(datetime(2023, 8, 31, 16, tzinfo=HK), 1, cal),
                         datetime(2023, 9, 4, 10, 30, tzinfo=HK)) # Sep 1 typhoon closure, then weekend

    def test_publication_before_open_and_lunch_use_prior_completed_bar(self):
        ticker = "0883.HK"
        provider = SyntheticProvider([bar(ticker, "2026-09-01T11:30:00+08:00", 1),
                                      bar(ticker, "2026-09-01T12:30:00+08:00", 2),
                                      bar(ticker, "2026-09-01T15:30:00+08:00", 3)])
        before = query_event(provider, resolve_asset(ticker), datetime(2026, 9, 2, 9, 0, tzinfo=HK),
                             as_of=datetime(2026, 9, 2, 16, tzinfo=HK), mode="hourly")
        self.assertEqual(before.baseline.observed_at, datetime(2026, 9, 1, 16, tzinfo=HK))
        lunch = query_event(provider, resolve_asset(ticker), datetime(2026, 9, 1, 12, 30, tzinfo=HK),
                            as_of=datetime(2026, 9, 2, 16, tzinfo=HK), mode="hourly")
        self.assertEqual(lunch.baseline.observed_at, datetime(2026, 9, 1, 12, tzinfo=HK))

    def test_pending_and_old_coverage_are_missing_without_zero(self):
        ticker = "0883.HK"
        provider = SyntheticProvider([bar(ticker, "2026-09-01T09:30:00+08:00", 0)])
        result = query_event(provider, resolve_asset(ticker), datetime(2026, 9, 1, 10, 0, tzinfo=HK),
                             as_of=datetime(2026, 9, 1, 10, 30, tzinfo=HK), mode="hourly")
        self.assertTrue(all(w.status == "pending" or w.price is None for w in result.windows))
        old = query_event(SyntheticProvider([]), resolve_asset(ticker), datetime(2023, 6, 1, 10, tzinfo=HK),
                          as_of=datetime(2026, 9, 1, 10, tzinfo=HK), mode="hourly")
        self.assertIsNone(old.baseline.price)
        self.assertTrue(all(w.flag == "missing_data" and w.price is None for w in old.windows))

    def test_nymex_session_is_overnight(self):
        ny = ZoneInfo("America/New_York")
        cal = calendar_for("NYMEX")
        base = datetime(2026, 9, 1, 17, tzinfo=ny)
        self.assertEqual(shift_trading_hours(base, 1, cal), datetime(2026, 9, 1, 19, tzinfo=ny))

    def test_excess_alignment_uses_same_hourly_target(self):
        provider = SyntheticProvider([bar("0883.HK", "2026-09-01T09:30:00+08:00", 0),
                                      bar("0883.HK", "2026-09-01T10:30:00+08:00", 2),
                                      bar("2800.HK", "2026-09-01T09:30:00+08:00", 0),
                                      bar("2800.HK", "2026-09-01T10:30:00+08:00", 1)])
        result = hourly_report(provider, resolve_asset("0883.HK"), datetime(2026, 9, 1, 10, 30, tzinfo=HK),
                               as_of=datetime(2026, 9, 1, 15, tzinfo=HK), benchmarks=["2800.HK"])
        row = result["benchmarks"]["2800.HK"]["windows"]["after_1th"]
        self.assertEqual(row["observed_at"], "2026-09-01T11:30:00+08:00")
        self.assertAlmostEqual(row["excess_return_pct"], (102 / 100 - 1) * 100 - (101 / 100 - 1) * 100)

    def test_nymex_backward_shift_uses_evening_session_before_trading_day(self):
        ny = ZoneInfo("America/New_York")
        cal = calendar_for("NYMEX")
        # Synthetic timestamp: D-1 20:00 is within trading day D's overnight session.
        self.assertEqual(shift_trading_hours(datetime(2026, 9, 1, 20, tzinfo=ny), -3, cal),
                         datetime(2026, 9, 1, 16, tzinfo=ny))

    def test_nymex_forward_shift_skips_friday_close_to_sunday_open(self):
        ny = ZoneInfo("America/New_York")
        # Synthetic timestamp: Friday 16:00 has one hour left before the close.
        self.assertEqual(shift_trading_hours(datetime(2026, 9, 11, 16, tzinfo=ny), 2,
                                             calendar_for("NYMEX")),
                         datetime(2026, 9, 13, 19, tzinfo=ny))

    def test_trading_seconds_skip_hk_lunch(self):
        # Synthetic interval; the calendar lunch closure is 12:00–13:00.
        cal = calendar_for("XHKG")
        seconds = trading_seconds_between(datetime(2026, 9, 1, 11, 30, tzinfo=HK),
                                          datetime(2026, 9, 1, 13, 30, tzinfo=HK), cal)
        self.assertEqual(seconds, 3600)

    def test_hourly_alignment_status_uses_trading_gap(self):
        ticker = "0883.HK"
        # Synthetic bars: baseline ends 10:30; after_1 target is 11:30.
        bars = [bar(ticker, "2026-09-01T09:30:00+08:00", 0),
                bar(ticker, "2026-09-01T10:00:00+08:00", 1)]
        result = query_event(SyntheticProvider(bars), resolve_asset(ticker),
                             datetime(2026, 9, 1, 10, 30, tzinfo=HK),
                             as_of=datetime(2026, 9, 1, 16, tzinfo=HK), mode="hourly")
        on_time = next(w for w in result.windows if w.label == "after_1th")
        self.assertEqual(on_time.status, "on_time")
        self.assertEqual(on_time.delay_seconds, -1800)

        # With the intermediate hourly bar absent, the one-hour gap is deferred.
        result = query_event(SyntheticProvider(bars[:1]), resolve_asset(ticker),
                             datetime(2026, 9, 1, 10, 30, tzinfo=HK),
                             as_of=datetime(2026, 9, 2, 16, tzinfo=HK), mode="hourly")
        deferred = next(w for w in result.windows if w.label == "after_1th")
        self.assertEqual(deferred.status, "deferred")
        self.assertIsNotNone(deferred.price)

        # Synthetic stale coverage more than two in-session hours from after_6.
        missing = next(w for w in result.windows if w.label == "after_6th")
        self.assertEqual(missing.status, "missing")
        self.assertEqual(missing.flag, "missing_data")
        self.assertIsNone(missing.price)

    def test_hk_publication_during_lunch_uses_expected_hourly_grid(self):
        ticker = "0883.HK"
        # Synthetic bars: 11:30 stamp ends at noon; 12:30 stamp ends at 13:30.
        provider = SyntheticProvider([bar(ticker, "2026-09-01T11:30:00+08:00", 0),
                                      bar(ticker, "2026-09-01T12:30:00+08:00", 1)])
        result = query_event(provider, resolve_asset(ticker),
                             datetime(2026, 9, 1, 12, 30, tzinfo=HK),
                             as_of=datetime(2026, 9, 1, 16, tzinfo=HK), mode="hourly")
        baseline = next(w for w in result.windows if w.label == "after_1th")
        self.assertEqual(baseline.target_at, datetime(2026, 9, 1, 14, 0, tzinfo=HK))
        self.assertEqual(baseline.observed_at, datetime(2026, 9, 1, 13, 30, tzinfo=HK))
        self.assertEqual(baseline.status, "on_time")

    def test_hourly_adjusted_basis_uses_daily_factor_and_skips_missing_day(self):
        # Synthetic SQLite rows; none of these prices represent market data.
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceStore(Path(tmp) / "prices.sqlite3")
            run_id = store.start_run("synthetic fixture", None, 1, datetime(2026, 9, 3, tzinfo=HK))
            store.upsert_rows([
                DailyRow("0883.HK", "XHKG", "HKD", "2026-09-01", 100, 90, 1,
                         "synthetic fixture", "2026-09-03", run_id),
                DailyRow("0883.HK", "XHKG", "HKD", "2026-09-02", 100, 100, 1,
                         "synthetic fixture", "2026-09-03", run_id),
            ])
            rows = []
            for day, stamp, close in (("2026-09-01", "2026-09-01T01:30:00Z", 100),
                                      ("2026-09-02", "2026-09-02T01:30:00Z", 110),
                                      ("2026-09-03", "2026-09-03T01:30:00Z", 120)):
                local = datetime.fromisoformat(day + "T09:30:00+08:00")
                rows.append(HourlyRow("0883.HK", "XHKG", "HKD", "1h", stamp,
                                      "Asia/Hong_Kong", local.isoformat(), close, close,
                                      close, close, close, 1, "synthetic fixture", "2026-09-03", run_id))
            store.upsert_hourly_rows(rows)
            provider = LocalDbPriceProvider(store=store, basis="adj")
            bars = provider.bars("0883.HK", datetime(2026, 9, 1, tzinfo=HK),
                                 datetime(2026, 9, 3, 23, tzinfo=HK), "hour")
            self.assertEqual([item.close for item in bars], [90, 110])

    def test_hourly_query_includes_bar_at_exact_end_bound(self):
        # Synthetic exact UTC boundary row stored with the canonical Z suffix.
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceStore(Path(tmp) / "prices.sqlite3")
            run_id = store.start_run("synthetic fixture", None, 1, datetime(2026, 9, 1, tzinfo=HK))
            stamp = "2026-09-01T01:30:00Z"
            store.upsert_hourly_rows([HourlyRow(
                "0883.HK", "XHKG", "HKD", "1h", stamp, "Asia/Hong_Kong",
                "2026-09-01T09:30:00+08:00", 100, 100, 100, 100, 100, 1,
                "synthetic fixture", "2026-09-01", run_id)])
            bars = LocalDbPriceProvider(store=store).bars(
                "0883.HK", datetime(2026, 9, 1, 9, tzinfo=HK),
                datetime(2026, 9, 1, 9, 30, tzinfo=HK), "hour")
            self.assertEqual(len(bars), 1)


    def test_zero_volume_artifacts_are_not_flagged(self):
        # Synthetic: HK 15:30 bar and NYMEX 18:00 reopen bar often carry volume 0 at Yahoo.
        hk_provider = SyntheticProvider([bar("0883.HK", "2026-09-01T14:30:00+08:00", 1),
                                         bar("0883.HK", "2026-09-01T15:30:00+08:00", 2, volume=0),
                                         bar("0883.HK", "2026-09-02T09:30:00+08:00", 3, volume=0)])
        hk = query_event(hk_provider, resolve_asset("0883.HK"), datetime(2026, 9, 1, 17, tzinfo=HK),
                         as_of=datetime(2026, 9, 3, 17, tzinfo=HK), mode="hourly")
        self.assertIsNone(hk.baseline_flag)  # 15:30 bar, artifact
        self.assertEqual({w.label: w.flag for w in hk.windows}["after_1th"], "zero_volume")  # real zero
        ny = ZoneInfo("America/New_York")
        bz = SyntheticProvider([bar("BZ=F", "2026-09-01T16:00:00-04:00", 1),
                                bar("BZ=F", "2026-09-01T18:00:00-04:00", 2, volume=0)])
        result = query_event(bz, resolve_asset("BZ=F"), datetime(2026, 9, 1, 19, 30, tzinfo=ny),
                             as_of=datetime(2026, 9, 1, 19, 45, tzinfo=ny), mode="hourly")
        self.assertEqual(result.baseline.price, 102)
        self.assertIsNone(result.baseline_flag)

    def test_stale_baseline_is_missing_not_old_price(self):
        # Synthetic: last bar is three trading days before publication (data gap).
        provider = SyntheticProvider([bar("0883.HK", "2026-09-01T09:30:00+08:00", 1)])
        result = query_event(provider, resolve_asset("0883.HK"), datetime(2026, 9, 4, 11, tzinfo=HK),
                             as_of=datetime(2026, 9, 10, tzinfo=HK), mode="hourly")
        self.assertIsNone(result.baseline.price)
        self.assertTrue(all(w.price is None and w.flag == "missing_data" for w in result.windows))

if __name__ == "__main__": unittest.main()
