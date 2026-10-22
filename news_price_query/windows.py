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
