import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.hkex_calendar import (
    AFTER_CLOSE, AFTERNOON, LUNCH_BREAK, MORNING, NON_TRADING_DAY, PRE_OPEN,
    CalendarRangeError, HkexCalendar, default_calendar,
)
from news_price_query.windows import TradingWindowConfig, trading_day_targets


HK = ZoneInfo("Asia/Hong_Kong")


def hk(*parts):
    return datetime(*parts, tzinfo=HK)


class CalendarDataTests(unittest.TestCase):
    def setUp(self):
        self.cal = default_calendar()

    def test_coverage_2000_through_2026(self):
        self.assertEqual(self.cal.first, date(2000, 1, 1))
        self.assertEqual(self.cal.last, date(2026, 12, 31))
        with self.assertRaises(CalendarRangeError):
            self.cal.day(date(2027, 1, 4))

    def test_weather_closures_have_hkex_sources(self):
        for day in (date(2023, 9, 1), date(2023, 9, 8)):
            info = self.cal.day(day)
            self.assertFalse(info.is_trading)
            self.assertTrue(info.closure_reason.startswith("weather"))
            self.assertTrue(info.source_url.startswith("https://www.hkex.com.hk/"))

    def test_holidays_and_half_days(self):
        self.assertFalse(self.cal.is_trading_day(date(2026, 10, 19)))  # Chung Yeung substitute
        self.assertTrue(self.cal.is_trading_day(date(2012, 3, 19)))    # yfinance gap, not a closure
        christmas_eve = self.cal.day(date(2025, 12, 24))
        self.assertTrue(christmas_eve.half_day)
        self.assertEqual(christmas_eve.close.isoformat("minutes"), "12:00")
        self.assertEqual(self.cal.day(date(2026, 2, 16)).close.isoformat("minutes"), "12:00")

    def test_historical_lunch_break(self):
        info = self.cal.day(date(2010, 6, 1))
        self.assertEqual((info.break_start.isoformat("minutes"), info.break_end.isoformat("minutes")), ("12:30", "14:30"))
        self.assertEqual(self.cal.session_of(hk(2010, 6, 1, 13, 0)), LUNCH_BREAK)


class SessionAndT0Tests(unittest.TestCase):
    def setUp(self):
        self.cal = default_calendar()

    def check(self, when, session, t0, baseline):
        self.assertEqual(self.cal.session_of(when), session)
        self.assertEqual(self.cal.t0(when), t0)
        self.assertEqual(self.cal.baseline_day(when), baseline)

    def test_pre_open_morning_lunch_afternoon_after_close(self):
        d = date(2026, 9, 28)
        self.check(hk(2026, 9, 28, 9, 0), PRE_OPEN, d, date(2026, 9, 25))
        self.check(hk(2026, 9, 28, 10, 0), MORNING, d, date(2026, 9, 25))
        self.check(hk(2026, 9, 28, 12, 30), LUNCH_BREAK, d, date(2026, 9, 25))
        self.check(hk(2026, 9, 28, 15, 59), AFTERNOON, d, date(2026, 9, 25))
        self.check(hk(2026, 9, 28, 16, 0), AFTER_CLOSE, date(2026, 9, 29), d)

    def test_other_timezone_is_converted_to_hkt(self):
        # 2026-09-28 08:30 UTC = 16:30 HKT, after the close.
        when = datetime(2026, 9, 28, 8, 30, tzinfo=ZoneInfo("UTC"))
        self.check(when, AFTER_CLOSE, date(2026, 9, 29), date(2026, 9, 28))

    def test_weekend_and_holiday(self):
        self.check(hk(2026, 10, 10, 10, 0), NON_TRADING_DAY, date(2026, 10, 12), date(2026, 10, 9))
        # Saturday, then Monday 19 Oct holiday: next trading day is Tuesday.
        self.check(hk(2026, 10, 17, 10, 0), NON_TRADING_DAY, date(2026, 10, 20), date(2026, 10, 16))

    def test_half_day(self):
        self.check(hk(2025, 12, 24, 11, 0), MORNING, date(2025, 12, 24), date(2025, 12, 23))
        self.check(hk(2025, 12, 24, 12, 0), AFTER_CLOSE, date(2025, 12, 29), date(2025, 12, 24))
        self.check(hk(2025, 12, 24, 14, 0), AFTER_CLOSE, date(2025, 12, 29), date(2025, 12, 24))

    def test_typhoon_and_black_rainstorm(self):
        self.check(hk(2023, 8, 31, 17, 0), AFTER_CLOSE, date(2023, 9, 4), date(2023, 8, 31))
        self.assertEqual(self.cal.shift_trading_days(date(2023, 9, 4), 1), date(2023, 9, 5))
        self.check(hk(2023, 9, 1, 10, 0), NON_TRADING_DAY, date(2023, 9, 4), date(2023, 8, 31))
        self.check(hk(2023, 9, 7, 20, 0), AFTER_CLOSE, date(2023, 9, 11), date(2023, 9, 7))
        self.check(hk(2023, 9, 8, 10, 0), NON_TRADING_DAY, date(2023, 9, 11), date(2023, 9, 7))

    def test_weather_delayed_open(self):
        self.check(hk(2023, 10, 9, 10, 0), PRE_OPEN, date(2023, 10, 9), date(2023, 10, 6))
        self.check(hk(2023, 10, 9, 15, 0), AFTERNOON, date(2023, 10, 9), date(2023, 10, 6))

    def test_shift_trading_days(self):
        self.assertEqual(self.cal.shift_trading_days(date(2023, 8, 31), 1), date(2023, 9, 4))
        self.assertEqual(self.cal.shift_trading_days(date(2023, 9, 1), 1), date(2023, 9, 4))
        self.assertEqual(self.cal.shift_trading_days(date(2023, 9, 1), -1), date(2023, 8, 31))
        self.assertEqual(self.cal.shift_trading_days(date(2023, 9, 11), -2), date(2023, 9, 6))
        with self.assertRaises(ValueError):
            self.cal.shift_trading_days(date(2023, 9, 1), 0)
        with self.assertRaises(CalendarRangeError):
            self.cal.shift_trading_days(date(2026, 12, 30), 5)

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            self.cal.session_of(datetime(2026, 9, 28, 10, 0))


class TradingWindowTargetTests(unittest.TestCase):
    def test_default_windows_after_close_before_typhoon(self):
        t0, baseline, targets = trading_day_targets(hk(2023, 8, 31, 17, 0), default_calendar())
        self.assertEqual((t0, baseline), (date(2023, 9, 4), date(2023, 8, 31)))
        days = {item.label: item.day for item in targets}
        self.assertEqual(days["after_t1"], date(2023, 9, 5))
        self.assertEqual(days["after_t3"], date(2023, 9, 7))
        self.assertEqual(days["after_t5"], date(2023, 9, 12))  # skips 8 Sep black rainstorm
        self.assertEqual(days["before_5td"], date(2023, 8, 24))
        self.assertEqual([item.label for item in targets],
                         ["before_5td", "before_21td", "before_63td", "before_126td",
                          "after_t1", "after_t3", "after_t5"])

    def test_configurable_and_out_of_range(self):
        config = TradingWindowConfig(before_days=(2,), after_days=(0, 2))
        _, _, targets = trading_day_targets(hk(2026, 12, 30, 17, 0), default_calendar(), config)
        days = {item.label: item.day for item in targets}
        self.assertEqual(days["before_2td"], date(2026, 12, 28))
        self.assertEqual(days["after_t0"], date(2026, 12, 31))
        self.assertIsNone(days["after_t2"])  # beyond calendar coverage

    def test_invalid_config(self):
        with self.assertRaises(ValueError):
            TradingWindowConfig(before_days=(0,))


class CustomCalendarFileTests(unittest.TestCase):
    def test_loads_small_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.csv"
            path.write_text(
                "date,is_trading,half_day,open,break_start,break_end,close,closure_reason,source_url,note\n"
                "2026-01-02,1,0,09:30,12:00,13:00,16:00,,test,\n"
                "2026-01-03,0,0,,,,,weekend,test,\n", encoding="utf-8")
            cal = HkexCalendar(path)
            self.assertTrue(cal.is_trading_day(date(2026, 1, 2)))
            self.assertFalse(cal.is_trading_day(date(2026, 1, 3)))


if __name__ == "__main__":
    unittest.main()
