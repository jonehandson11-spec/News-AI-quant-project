"""News-relative target timestamps."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class WindowTarget:
    label: str
    direction: str
    target: datetime


def _months_before(value: datetime, count: int) -> datetime:
    total = value.year * 12 + (value.month - 1) - count
    year, zero_based_month = divmod(total, 12)
    month = zero_based_month + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def targets(published_at: datetime) -> list[WindowTarget]:
    """Use calendar months before and elapsed clock time after publication."""
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("News timestamp requires a timezone")
    before = [
        WindowTarget(f"before_{months}m", "before", _months_before(published_at, months))
        for months in (1, 3, 6)
    ]
    after = [
        WindowTarget(label, "after", published_at + duration)
        for label, duration in (
            ("after_1h", timedelta(hours=1)),
            ("after_3h", timedelta(hours=3)),
            ("after_12h", timedelta(hours=12)),
            ("after_24h", timedelta(hours=24)),
            ("after_3d", timedelta(days=3)),
            ("after_1w", timedelta(days=7)),
        )
    ]
    return before + after


# --- Trading-day windows (10.11 daily design) ---------------------------------

@dataclass(frozen=True)
class TradingWindowConfig:
    """Window lengths in HKEX trading days.

    before_days: lookback from the baseline day (last close before the news),
    defaults 5/21/63/126 for about 1 week, 1 month, 1 quarter, 6 months.
    after_days: t+k counts k trading days after t0 (the event day).
    max_gap_days: how many further trading days a missing bar may be filled from
    (status deferred / deferred_back); beyond that the window is missing.
    """

    before_days: tuple[int, ...] = (5, 21, 63, 126)
    after_days: tuple[int, ...] = (1, 3, 5)
    max_gap_days: int = 5

    def __post_init__(self):
        if any(value <= 0 for value in self.before_days):
            raise ValueError("before_days must be positive")
        if any(value < 0 for value in self.after_days):
            raise ValueError("after_days must be zero or positive")
        if self.max_gap_days < 0:
            raise ValueError("max_gap_days must be zero or positive")


DEFAULT_TRADING_WINDOWS = TradingWindowConfig()


@dataclass(frozen=True)
class TradingWindowTarget:
    label: str
    direction: str
    offset: int
    day: object  # datetime.date of the target trading day, or None if outside calendar
    target: datetime | None


def before_label(days: int) -> str:
    return f"before_{days}td"


def after_label(days: int) -> str:
    return f"after_t{days}"


def window_label_zh(label: str) -> str:
    """Chinese display name for trading-day and legacy labels."""
    legacy = {
        "before_1m": "新闻前 1 个月", "before_3m": "新闻前 1 季度", "before_6m": "新闻前半年",
        "after_1h": "新闻后 1 小时", "after_3h": "新闻后 3 小时", "after_12h": "新闻后 12 小时",
        "after_24h": "新闻后 24 小时", "after_3d": "新闻后 3 天", "after_1w": "新闻后 1 周",
    }
    if label in legacy:
        return legacy[label]
    if label.startswith(("before_", "after_")) and label.endswith("th"):
        direction, value = label.split("_", 1)
        return f"新闻{'前' if direction == 'before' else '后'} {value[:-2]} 个交易小时"
    named = {5: "约 1 周", 21: "约 1 个月", 63: "约 1 季度", 126: "约半年"}
    if label.startswith("before_") and label.endswith("td"):
        days = int(label[len("before_"):-2])
        hint = f"（{named[days]}）" if days in named else ""
        return f"新闻前 {days} 个交易日{hint}"
    if label.startswith("after_t"):
        days = int(label[len("after_t"):])
        return "事件日 t0 收盘" if days == 0 else f"事件后第 {days} 个交易日（t+{days}）"
    return label


def trading_day_targets(published_at: datetime, calendar,
                        config: TradingWindowConfig = DEFAULT_TRADING_WINDOWS):
    """Return (t0, baseline_day, targets). Before windows count back from the
    baseline day; after windows count forward from t0."""
    from .hkex_calendar import CalendarRangeError

    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("News timestamp requires a timezone")
    t0 = calendar.t0(published_at)
    baseline_day = calendar.shift_trading_days(t0, -1)
    output = []
    for days in config.before_days:
        try:
            day = calendar.shift_trading_days(baseline_day, -days)
            target = calendar.day(day).close_at()
        except CalendarRangeError:
            day, target = None, None
        output.append(TradingWindowTarget(before_label(days), "before", days, day, target))
    for days in config.after_days:
        try:
            day = calendar.shift_trading_days(t0, days)
            target = calendar.day(day).close_at()
        except CalendarRangeError:
            day, target = None, None
        output.append(TradingWindowTarget(after_label(days), "after", days, day, target))
    return t0, baseline_day, output


HOURLY_OFFSETS = (6, 3, 1, 1, 3, 6)


def hourly_label(hours: int, direction: str) -> str:
    return f"{direction}_{hours}th"


def hourly_label_zh(label: str) -> str:
    direction, value = label.split("_", 1)
    hours = value[:-2]
    return f"新闻{'前' if direction == 'before' else '后'} {hours} 个交易小时"


def _hourly_intervals(day, calendar):
    """Return (start,end) session segments; NYMEX uses the Globex session approximation.

    Yahoo hourly futures bars span 18:00 ET on the prior calendar date through
    17:00 ET on the NYMEX trading date; the CSV 14:30 close remains daily settlement.
    """
    from datetime import time
    from datetime import timedelta
    from .market_calendars import MARKETS

    info = calendar.day(day)
    if not info.is_trading:
        return []
    if calendar.market == "NYMEX":
        start = datetime.combine(day - timedelta(days=1), time(18), calendar.tz)
        end = datetime.combine(day, time(17), calendar.tz)
        return [(start, end)]
    segments = []
    if info.break_start and info.break_end:
        segments = [(info.open, info.break_start), (info.break_end, info.close)]
    else:
        segments = [(info.open, info.close)]
    return [(datetime.combine(day, a, calendar.tz), datetime.combine(day, b, calendar.tz))
            for a, b in segments]


def hourly_bar_end(bar, calendar):
    """Map Yahoo's HK 12:30 stamp to 13:00 effective start; cap at segment close."""
    from datetime import timedelta, time
    stamp = bar.timestamp.astimezone(calendar.tz)
    if calendar.market == "XHKG" and stamp.timetz().replace(tzinfo=None) == time(12, 30):
        # Yahoo labels the short first-afternoon bar 12:30 although it trades 13:00–13:30.
        return stamp.replace(hour=13, minute=30)
    # Select session segment containing the bar start. HK post-lunch stamp aligns at 13:00.
    for start, end in _hourly_intervals(stamp.date(), calendar):
        if start <= stamp < end:
            return min(stamp + timedelta(hours=1), end)
    # NYMEX session can begin the previous date.
    if calendar.market == "NYMEX":
        for start, end in _hourly_intervals(stamp.date() + timedelta(days=1), calendar):
            if start <= stamp < end:
                return min(stamp + timedelta(hours=1), end)
    return None


def shift_trading_hours(value: datetime, hours: int, calendar) -> datetime:
    """Move on the exchange's in-session timeline, skipping breaks and closures."""
    from datetime import timedelta
    if hours == 0:
        return value
    direction = 1 if hours > 0 else -1
    requested = abs(hours) * 3600
    local = value.astimezone(calendar.tz)
    margin = 10
    while True:
        remaining = requested
        first, last = local.date() - timedelta(days=margin), local.date() + timedelta(days=margin)
        covered_all = first <= calendar.first and last >= calendar.last
        segments = []
        day = first
        while day <= last:
            try:
                segments.extend(_hourly_intervals(day, calendar))
            except ValueError:  # CalendarRangeError at either coverage edge.
                pass
            day += timedelta(days=1)
        segments.sort()
        merged = []
        for start, end in segments:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        walk = reversed(merged) if direction < 0 else iter(merged)
        for start, end in walk:
            if direction > 0:
                cursor = max(local, start)
                available = max(0, int((end - cursor).total_seconds()))
                if remaining <= available:
                    return cursor + timedelta(seconds=remaining)
            else:
                cursor = min(local, end)
                available = max(0, int((cursor - start).total_seconds()))
                if remaining <= available:
                    return cursor - timedelta(seconds=remaining)
            remaining -= available
        if covered_all:
            raise ValueError("Trading-hour shift exceeds calendar coverage")
        margin += 10


def trading_seconds_between(a: datetime, b: datetime, calendar) -> int:
    """Count exchange in-session seconds in [a, b], with a <= b."""
    from datetime import timedelta
    if a.tzinfo is None or a.utcoffset() is None or b.tzinfo is None or b.utcoffset() is None:
        raise ValueError("Trading-time endpoints require timezones")
    if b < a:
        raise ValueError("a must be no later than b")
    start_at, end_at = a.astimezone(calendar.tz), b.astimezone(calendar.tz)
    total = 0
    day = start_at.date() - timedelta(days=2)
    last = end_at.date() + timedelta(days=2)
    while day <= last:
        try:
            intervals = _hourly_intervals(day, calendar)
        except ValueError:
            intervals = []
        for start, end in intervals:
            left, right = max(start, start_at), min(end, end_at)
            if right > left:
                total += int((right - left).total_seconds())
        day += timedelta(days=1)
    return total
