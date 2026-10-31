"""小时线覆盖完整性检查；用的都是合成数据，不是真实行情。"""

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.hourly_quality import day_coverage, scan, summarize
from news_price_query.market_calendars import calendar_for
from news_price_query.prices import PriceBar
from news_price_query.query import query_event

HK = ZoneInfo("Asia/Hong_Kong")
NY = ZoneInfo("America/New_York")

HALF_DAY = date(2024, 12, 24)      # 港股平安夜，12:00 收盘
FULL_DAY = date(2026, 10, 28)      # 港股正常交易日


def hk(hour, minute, day=FULL_DAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=HK)


class DayCoverageTests(unittest.TestCase):
    def test_half_day_missing_last_bar_is_flagged(self):
        """平安夜只存到 11:30，收盘 12:00，应报缺 30 分钟。"""
        cal = calendar_for("XHKG")
        coverage = day_coverage("0883.HK", "XHKG", HALF_DAY,
                                [hk(9, 30, HALF_DAY), hk(10, 30, HALF_DAY)], cal)
        self.assertIsNotNone(coverage)
        self.assertFalse(coverage.ok)
        self.assertEqual(coverage.uncovered_minutes, 30)
        self.assertEqual(coverage.session_close, hk(12, 0, HALF_DAY))

    def test_half_day_with_last_bar_is_clean(self):
        cal = calendar_for("XHKG")
        coverage = day_coverage("0883.HK", "XHKG", HALF_DAY,
                                [hk(9, 30, HALF_DAY), hk(10, 30, HALF_DAY), hk(11, 30, HALF_DAY)], cal)
        self.assertTrue(coverage.ok)
        self.assertEqual(coverage.bars, 3)

    def test_full_day_seven_bars_is_clean(self):
        """港股正常日 Yahoo 给 7 根（含 12:30 那根半小时），覆盖到 16:00 即完整。"""
        cal = calendar_for("XHKG")
        starts = [hk(9, 30), hk(10, 30), hk(11, 30), hk(12, 30), hk(13, 30), hk(14, 30), hk(15, 30)]
        coverage = day_coverage("0883.HK", "XHKG", FULL_DAY, starts, cal)
        self.assertTrue(coverage.ok)
        self.assertEqual(coverage.bars, 7)
        self.assertEqual(coverage.last_end, hk(16, 0))

    def test_missing_afternoon_is_flagged_in_minutes(self):
        cal = calendar_for("XHKG")
        coverage = day_coverage("0883.HK", "XHKG", FULL_DAY,
                                [hk(9, 30), hk(10, 30), hk(11, 30)], cal)
        self.assertFalse(coverage.ok)
        self.assertEqual(coverage.uncovered_minutes, 240)  # 12:00 → 16:00

    def test_non_trading_day_returns_none(self):
        cal = calendar_for("XHKG")
        self.assertIsNone(day_coverage("0883.HK", "XHKG", date(2026, 10, 25), [], cal))

    def test_us_early_close_is_measured_against_1300(self):
        day = date(2024, 11, 29)  # 感恩节次日，13:00 收盘
        cal = calendar_for("XNYS")
        starts = [datetime(2024, 11, 29, h, 30, tzinfo=NY) for h in (9, 10, 11)]
        coverage = day_coverage("XOM", "XNYS", day, starts, cal)
        self.assertFalse(coverage.ok)
        self.assertEqual(coverage.uncovered_minutes, 30)  # 12:30 → 13:00


class _Instrument:
    def __init__(self, ticker, market):
        self.ticker, self.market = ticker, market


class _Row:
    def __init__(self, local):
        self.bar_start_local = local


class _Store:
    """只实现 scan() 用到的两个接口，避免为单测建库。"""

    def __init__(self, data):
        self._data = data

    def instruments(self):
        return [_Instrument(t, "XHKG") for t in self._data]

    def hourly_rows(self, ticker):
        return [_Row(v) for v in self._data[ticker]]


class ScanTests(unittest.TestCase):
    def test_scan_reports_only_incomplete_days(self):
        store = _Store({"0883.HK": ["2024-12-24T09:30:00+08:00", "2024-12-24T10:30:00+08:00",
                                    "2026-10-28T09:30:00+08:00", "2026-10-28T10:30:00+08:00",
                                    "2026-10-28T11:30:00+08:00", "2026-10-28T12:30:00+08:00",
                                    "2026-10-28T13:30:00+08:00", "2026-10-28T14:30:00+08:00",
                                    "2026-10-28T15:30:00+08:00"]})
        summary = summarize(scan(store, ["0883.HK"]))
        self.assertEqual(summary.checked_days, 2)
        self.assertEqual(len(summary.incomplete_days), 1)
        self.assertEqual(summary.incomplete_days[0].day, HALF_DAY)
        self.assertEqual(summary.worst_uncovered_minutes, 30)
        self.assertEqual(summary.as_dict()["incomplete_count"], 1)


class _Provider:
    def __init__(self, bars):
        self.bars_data = bars

    def bars(self, ticker, start, end, interval):
        return [b for b in self.bars_data
                if b.ticker == ticker and b.interval == interval and start <= b.timestamp <= end]


def _bar(stamp, price):
    return PriceBar("0883.HK", "hour", stamp, price, "synthetic fixture (not real prices)", "raw", 100)


class IncompleteSessionFlagTests(unittest.TestCase):
    def test_flag_set_when_day_closed_but_last_bar_missing(self):
        """平安夜 14:00 的新闻：当天已收盘，但库里最后只到 11:30 → 基准价不可信。"""
        provider = _Provider([_bar(datetime(2024, 12, 24, 9, 30, tzinfo=HK), 18.40),
                              _bar(datetime(2024, 12, 24, 10, 30, tzinfo=HK), 18.72)])
        result = query_event(provider, resolve_asset("0883.HK"),
                             datetime(2024, 12, 24, 14, 0, tzinfo=HK),
                             as_of=datetime(2024, 12, 26, 12, 0, tzinfo=HK), mode="hourly")
        self.assertEqual(result.baseline.observed_at, datetime(2024, 12, 24, 11, 30, tzinfo=HK))
        self.assertEqual(result.baseline_flag, "incomplete_session")

    def test_no_flag_before_close(self):
        """同一天但新闻在收盘前，不能因为"还没到收盘"就报警。"""
        provider = _Provider([_bar(datetime(2024, 12, 24, 9, 30, tzinfo=HK), 18.40),
                              _bar(datetime(2024, 12, 24, 10, 30, tzinfo=HK), 18.72)])
        result = query_event(provider, resolve_asset("0883.HK"),
                             datetime(2024, 12, 24, 11, 0, tzinfo=HK),
                             as_of=datetime(2024, 12, 24, 11, 30, tzinfo=HK), mode="hourly")
        self.assertIsNone(result.baseline_flag)

    def test_no_flag_when_day_is_complete(self):
        starts = [(9, 30), (10, 30), (11, 30), (12, 30), (13, 30), (14, 30), (15, 30)]
        provider = _Provider([_bar(hk(h, m), 24.0 + i) for i, (h, m) in enumerate(starts)])
        result = query_event(provider, resolve_asset("0883.HK"),
                             datetime(2026, 10, 28, 20, 0, tzinfo=HK),
                             as_of=datetime(2026, 10, 29, 12, 0, tzinfo=HK), mode="hourly")
        self.assertEqual(result.baseline.observed_at, datetime(2026, 10, 28, 16, 0, tzinfo=HK))
        self.assertIsNone(result.baseline_flag)


if __name__ == "__main__":
    unittest.main()
