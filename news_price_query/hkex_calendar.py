"""Exchange trading calendars from CSV (HKEX: data/hkex_calendar.csv; other
markets: data/calendars/<MIC>.csv, see market_calendars.py).

Times are Hong Kong time. The CSV is XHKG-based with manual weather overrides;
see build_calendar.py. Standard library only.
"""

import csv
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


HK = ZoneInfo("Asia/Hong_Kong")
DEFAULT_PATH = Path(__file__).resolve().parent / "data" / "hkex_calendar.csv"

PRE_OPEN = "pre_open"
MORNING = "morning"
LUNCH_BREAK = "lunch_break"
AFTERNOON = "afternoon"
AFTER_CLOSE = "after_close"
NON_TRADING_DAY = "non_trading_day"
REGULAR = "regular_session"  # markets without a lunch break (e.g. NYSE, TSX, Tadawul)


class CalendarRangeError(ValueError):
    """A date outside the calendar's coverage was requested."""


@dataclass(frozen=True)
class TradingDay:
    day: date
    is_trading: bool
    half_day: bool
    open: time | None
    break_start: time | None
    break_end: time | None
    close: time | None
    closure_reason: str
    source_url: str
    note: str
    tz: ZoneInfo = HK

    def close_at(self) -> datetime:
        if not self.is_trading or self.close is None:
            raise ValueError(f"{self.day.isoformat()} is not a trading day")
        return datetime.combine(self.day, self.close, self.tz)


def _time(value: str) -> time | None:
    return time.fromisoformat(value) if value else None


class HkexCalendar:
    """Trading calendar for one market. Default: HKEX. Session times in the CSV
    are local exchange time (`tz`)."""

    def __init__(self, path: str | Path = DEFAULT_PATH, *, tz: str | ZoneInfo = HK, market: str = "XHKG"):
        self.path = Path(path)
        self.tz = tz if isinstance(tz, ZoneInfo) else ZoneInfo(tz)
        self.market = market
        self._days: dict[date, TradingDay] = {}
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                item = TradingDay(
                    day=date.fromisoformat(row["date"]),
                    is_trading=row["is_trading"] == "1",
                    half_day=row["half_day"] == "1",
                    open=_time(row["open"]), break_start=_time(row["break_start"]),
                    break_end=_time(row["break_end"]), close=_time(row["close"]),
                    closure_reason=row["closure_reason"], source_url=row["source_url"],
                    note=row.get("note", ""), tz=self.tz,
                )
                if item.is_trading and (item.open is None or item.close is None):
                    raise ValueError(f"Trading day without session times: {row['date']}")
                self._days[item.day] = item
        if not self._days:
            raise ValueError(f"Empty trading calendar: {self.path}")
        self.first = min(self._days)
        self.last = max(self._days)
        self._trading = sorted(day for day, item in self._days.items() if item.is_trading)

    def _check(self, day: date) -> None:
        if not self.first <= day <= self.last:
            raise CalendarRangeError(
                f"{day.isoformat()} is outside the {self.market} calendar "
                f"({self.first.isoformat()} to {self.last.isoformat()})")

    def day(self, value: date) -> TradingDay:
        self._check(value)
        return self._days[value]

    def is_trading_day(self, value: date) -> bool:
        return self.day(value).is_trading

    def session_of(self, timestamp: datetime) -> str:
        """Classify a publication time: pre_open, morning, lunch_break, afternoon,
        after_close, or non_trading_day (weekend, holiday, weather closure)."""
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Timestamp requires a timezone")
        local = timestamp.astimezone(self.tz)
        info = self.day(local.date())
        if not info.is_trading:
            return NON_TRADING_DAY
        clock = local.timetz().replace(tzinfo=None)
        if clock < info.open:
            return PRE_OPEN
        if clock >= info.close:
            return AFTER_CLOSE
        if info.break_start and info.break_end:
            if clock < info.break_start:
                return MORNING
            if clock < info.break_end:
                return LUNCH_BREAK
            return AFTERNOON
        if self.market != "XHKG":
            return REGULAR
        # HKEX half day (no break) or a weather-shortened day.
        return MORNING if info.close <= time(12, 30) else AFTERNOON

    def t0(self, timestamp: datetime) -> date:
        """Event day: the publication day if it is a trading day and the news
        arrives before that day's close; otherwise the next trading day."""
        local = timestamp.astimezone(self.tz)
        session = self.session_of(local)
        if session in (PRE_OPEN, MORNING, LUNCH_BREAK, AFTERNOON, REGULAR):
            return local.date()
        return self.shift_trading_days(local.date(), 1)

    def shift_trading_days(self, start: date, count: int) -> date:
        """Move `count` trading days from `start`. count=0 needs a trading day;
        positive counts trading days strictly after, negative strictly before."""
        self._check(start)
        if count == 0:
            if not self.is_trading_day(start):
                raise ValueError(f"{start.isoformat()} is not a trading day")
            return start
        if count > 0:
            index = bisect_right(self._trading, start) + count - 1
        else:
            index = bisect_left(self._trading, start) + count
        if index < 0 or index >= len(self._trading):
            raise CalendarRangeError(
                f"Shifting {start.isoformat()} by {count} trading days leaves the {self.market} calendar "
                f"({self.first.isoformat()} to {self.last.isoformat()})")
        return self._trading[index]

    def baseline_day(self, timestamp: datetime) -> date:
        """Last trading day whose close is strictly before the publication."""
        return self.shift_trading_days(self.t0(timestamp), -1)


@lru_cache(maxsize=1)
def default_calendar() -> HkexCalendar:
    return HkexCalendar()


TradingCalendar = HkexCalendar
