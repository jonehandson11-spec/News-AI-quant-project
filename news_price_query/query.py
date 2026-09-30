"""Match a publication time to price observations without silent time filling."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .assets import Asset
from .prices import PriceBar
from .windows import targets


HK = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    price: float
    source: str
    basis: str


@dataclass(frozen=True)
class WindowResult:
    label: str
    target_at: datetime
    observed_at: datetime | None
    price: float | None
    return_pct: float | None
    status: str
    delay_seconds: int | None
    source: str | None
    basis: str | None


@dataclass(frozen=True)
class EventResult:
    asset: Asset
    published_at: datetime
    baseline: Observation
    baseline_gap_seconds: int
    baseline_status: str
    windows: tuple[WindowResult, ...]


def _observation(bar: PriceBar) -> Observation:
    return Observation(bar.timestamp, bar.close, bar.source, bar.basis)


def query_event(provider, asset: Asset, published_at: datetime, *, as_of: datetime) -> EventResult:
    """Return raw price changes around a news timestamp using HK market time."""
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("News timestamp requires a timezone")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of requires a timezone")
    event_time = published_at.astimezone(HK)
    as_of = as_of.astimezone(HK)
    if event_time > as_of:
        raise ValueError("News publication time is after as_of")

    window_targets = targets(event_time)
    minute_start = event_time - timedelta(days=14)
    minute_end = min(as_of, window_targets[-1].target + timedelta(days=14))
    minutes = [
        bar for bar in provider.bars(asset.ticker, minute_start, minute_end, "minute")
        if bar.timestamp + timedelta(minutes=1) <= as_of
    ]
    baseline_bar = max((bar for bar in minutes if bar.timestamp + timedelta(minutes=1) <= event_time),
                       key=lambda bar: bar.timestamp, default=None)
    if baseline_bar is None:
        raise ValueError(f"No pre-news baseline minute price for {asset.ticker} at {event_time.isoformat()}")
    baseline = _observation(baseline_bar)
    baseline_gap = int((event_time - (baseline.observed_at + timedelta(minutes=1))).total_seconds())
    baseline_status = "recent" if baseline_gap < 60 else "stale"

    earliest_day = window_targets[2].target - timedelta(days=14)
    days = provider.bars(asset.ticker, earliest_day, event_time, "day")
    results: list[WindowResult] = []
    for target in window_targets:
        if target.direction == "before":
            match = max((bar for bar in days if bar.timestamp.date() <= target.target.date()),
                        key=lambda bar: bar.timestamp, default=None)
            if match is None:
                results.append(WindowResult(target.label, target.target, None, None,
                                            None, "missing", None, None, None))
                continue
            if match.basis != baseline.basis:
                raise ValueError(f"Mixed price basis for {asset.ticker}: {baseline.basis} and {match.basis}")
            value = (baseline.price - match.close) / match.close * 100
            status = "on_date" if match.timestamp.date() == target.target.date() else "deferred_back"
            results.append(WindowResult(
                target.label, target.target, match.timestamp, match.close, value,
                status, None, match.source, match.basis,
            ))
            continue
        if target.target > as_of:
            results.append(WindowResult(target.label, target.target, None, None,
                                        None, "pending", None, None, None))
            continue
        match = next((bar for bar in minutes if target.target <= bar.timestamp <= as_of), None)
        if match is None:
            results.append(WindowResult(target.label, target.target, None, None,
                                        None, "missing", None, None, None))
            continue
        if match.basis != baseline.basis:
            raise ValueError(f"Mixed price basis for {asset.ticker}: {baseline.basis} and {match.basis}")
        delay = int((match.timestamp - target.target).total_seconds())
        status = "on_time" if delay < 60 else "deferred"
        value = (match.close - baseline.price) / baseline.price * 100
        results.append(WindowResult(
            target.label, target.target, match.timestamp, match.close, value,
            status, delay, match.source, match.basis,
        ))
    return EventResult(
        asset=asset, published_at=event_time, baseline=baseline,
        baseline_gap_seconds=baseline_gap, baseline_status=baseline_status,
        windows=tuple(results),
    )
