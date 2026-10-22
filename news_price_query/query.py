"""Match a publication time to price observations without silent time filling."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .assets import Asset
from .prices import PriceBar
from .windows import DEFAULT_TRADING_WINDOWS, TradingWindowConfig, targets, trading_day_targets


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
    target_at: datetime | None
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
    mode: str = "minute"
    session: str | None = None
    t0: object = None  # datetime.date in daily mode


def _observation(bar: PriceBar) -> Observation:
    return Observation(bar.timestamp, bar.close, bar.source, bar.basis)


def query_event(provider, asset: Asset, published_at: datetime, *, as_of: datetime,
                mode: str = "minute", calendar=None,
                config: TradingWindowConfig = DEFAULT_TRADING_WINDOWS) -> EventResult:
    """Return raw price changes around a news timestamp using HK market time.

    mode="minute" keeps the original nine clock-time windows (needs minute bars).
    mode="daily" uses HKEX trading days and daily closes only.
    """
    if mode == "daily":
        return query_event_daily(provider, asset, published_at, as_of=as_of,
                                 calendar=calendar, config=config)
    if mode != "minute":
        raise ValueError(f"Unknown query mode: {mode}")
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


def query_event_daily(provider, asset: Asset, published_at: datetime, *, as_of: datetime,
                      calendar=None,
                      config: TradingWindowConfig = DEFAULT_TRADING_WINDOWS) -> EventResult:
    """Trading-day event windows from daily closes.

    Baseline = close of the last trading day before t0 (the last close before
    publication). before_Ntd = close N trading days before the baseline day;
    after_tK = close K trading days after t0. Bars dated on non-trading days
    (e.g. holiday filler rows) are ignored. A missing bar may be filled from up
    to config.max_gap_days neighbouring trading days (deferred/deferred_back);
    otherwise the window is missing. Missing values are never treated as zero.
    """
    from .hkex_calendar import default_calendar

    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("News timestamp requires a timezone")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of requires a timezone")
    calendar = calendar or default_calendar()
    event_time = published_at.astimezone(HK)
    as_of = as_of.astimezone(HK)
    if event_time > as_of:
        raise ValueError("News publication time is after as_of")

    session = calendar.session_of(event_time)
    t0, baseline_day, window_targets = trading_day_targets(event_time, calendar, config)
    days_needed = [baseline_day] + [item.day for item in window_targets if item.day is not None]
    start = min(days_needed) - timedelta(days=3 * config.max_gap_days + 7)
    end = max(days_needed) + timedelta(days=3 * config.max_gap_days + 7)
    fetch_start = datetime.combine(start, datetime.min.time(), HK)
    fetch_end = min(as_of, datetime.combine(end, datetime.max.time(), HK))

    closes: dict = {}
    for bar in provider.bars(asset.ticker, fetch_start, fetch_end, "day"):
        day = bar.timestamp.astimezone(HK).date()
        try:
            info = calendar.day(day)
        except ValueError:
            continue
        if not info.is_trading or info.close_at() > as_of:
            continue
        closes[day] = Observation(info.close_at(), bar.close, bar.source, bar.basis)

    def _nearest(day, step):
        """Exact bar, else up to max_gap_days trading days in `step` direction."""
        if day in closes:
            return closes[day], 0
        current = day
        for gap in range(1, config.max_gap_days + 1):
            try:
                current = calendar.shift_trading_days(current, step)
            except ValueError:
                return None, None
            if step > 0 and calendar.day(current).close_at() > as_of:
                return None, None
            if current in closes:
                return closes[current], gap
        return None, None

    baseline, baseline_gap_days = _nearest(baseline_day, -1)
    if baseline is None:
        raise ValueError(
            f"No pre-news daily close for {asset.ticker} on or within "
            f"{config.max_gap_days} trading days before {baseline_day.isoformat()}")
    baseline_gap = int((event_time - baseline.observed_at).total_seconds())
    baseline_status = "on_date" if baseline_gap_days == 0 else "stale"

    results: list[WindowResult] = []
    for target in window_targets:
        if target.day is None:
            results.append(WindowResult(target.label, None, None, None, None,
                                        "missing", None, None, None))
            continue
        if target.direction == "after" and target.target > as_of:
            results.append(WindowResult(target.label, target.target, None, None, None,
                                        "pending", None, None, None))
            continue
        step = -1 if target.direction == "before" else 1
        match, gap_days = _nearest(target.day, step)
        if match is None:
            # A forward fill that could still arrive later is pending, not missing.
            status = "missing"
            if target.direction == "after":
                try:
                    last_allowed = calendar.shift_trading_days(target.day, config.max_gap_days)
                    if calendar.day(last_allowed).close_at() > as_of:
                        status = "pending"
                except ValueError:
                    pass
            results.append(WindowResult(target.label, target.target, None, None, None,
                                        status, None, None, None))
            continue
        if match.basis != baseline.basis:
            raise ValueError(f"Mixed price basis for {asset.ticker}: {baseline.basis} and {match.basis}")
        if target.direction == "before":
            value = (baseline.price - match.price) / match.price * 100
            status = "on_date" if gap_days == 0 else "deferred_back"
        else:
            value = (match.price - baseline.price) / baseline.price * 100
            status = "on_date" if gap_days == 0 else "deferred"
        delay = int((match.observed_at - target.target).total_seconds())
        results.append(WindowResult(
            target.label, target.target, match.observed_at, match.price, value,
            status, delay, match.source, match.basis,
        ))
    return EventResult(
        asset=asset, published_at=event_time, baseline=baseline,
        baseline_gap_seconds=baseline_gap, baseline_status=baseline_status,
        windows=tuple(results), mode="daily", session=session, t0=t0,
    )
