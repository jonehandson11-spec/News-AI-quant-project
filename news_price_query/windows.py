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
