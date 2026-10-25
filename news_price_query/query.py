"""Match a publication time to price observations without silent time filling."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .assets import Asset
from .prices import PriceBar
from .windows import (DEFAULT_TRADING_WINDOWS, TradingWindowConfig, targets, trading_day_targets,
                      HOURLY_OFFSETS, hourly_label, hourly_bar_end, shift_trading_hours,
                      trading_seconds_between)


HK = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    price: float
    source: str
    basis: str
    volume: float | None = None


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
    flag: str | None = None  # daily mode: missing_data (trading day without a bar) or zero_volume


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
    baseline_flag: str | None = None


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
    if mode == "hourly":
        return query_event_hourly(provider, asset, published_at, as_of=as_of, calendar=calendar)
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


def query_event_hourly(provider, asset: Asset, published_at: datetime, *, as_of: datetime,
                       calendar=None) -> EventResult:
    """Trading-hour windows using exchange session time and local hourly bars."""
    from .market_calendars import calendar_for
    from .windows import hourly_label_zh
    if published_at.tzinfo is None or as_of.tzinfo is None:
        raise ValueError("News timestamp and as_of require timezones")
    calendar = calendar or calendar_for(getattr(asset, "market", "XHKG"))
    event = published_at.astimezone(calendar.tz)
    cutoff = as_of.astimezone(calendar.tz)
    if event > cutoff:
        raise ValueError("News publication time is after as_of")
    # Two-year feed coverage is supplied by Yahoo; query a broad calendar range and
    # report a uniform missing result when there is no usable baseline.
    try:
        bars = provider.bars(asset.ticker, event - timedelta(days=760), cutoff, "hour")
    except (ValueError, RuntimeError):
        bars = []
    eligible = []
    for bar in bars:
        end = hourly_bar_end(bar, calendar)
        if end is not None and end <= cutoff:
            eligible.append((end, bar))
    eligible.sort(key=lambda pair: pair[0])
    base = next(((end, bar) for end, bar in reversed(eligible) if end <= event), None)
    if base is None:
        empty = tuple(WindowResult(hourly_label(h, d), None, None, None, None,
                                   "missing", None, None, None, "missing_data")
                      for d, values in (("before", (6, 3, 1)), ("after", (1, 3, 6))) for h in values)
        baseline = Observation(event, None, "", "raw")
        return EventResult(asset, event, baseline, 0, "missing", empty, mode="hourly",
                           session=calendar.session_of(event), baseline_flag="missing_data")
    base_end, base_bar = base
    if trading_seconds_between(base_end, event, calendar) > 2 * 3600:
        base = None  # stale baseline (data gap or suspension): report missing, never a stale price
    if base is None:
        empty = tuple(WindowResult(hourly_label(h, d), None, None, None, None,
                                   "missing", None, None, None, "missing_data")
                      for d, values in (("before", (6, 3, 1)), ("after", (1, 3, 6))) for h in values)
        return EventResult(asset, event, Observation(event, None, "", "raw"), 0, "missing", empty,
                           mode="hourly", session=calendar.session_of(event), baseline_flag="missing_data")
    baseline = Observation(base_end, base_bar.close, base_bar.source, base_bar.basis, base_bar.volume)
    windows = []
    for direction, values in (("before", (6, 3, 1)), ("after", (1, 3, 6))):
        for hours in values:
            label = hourly_label(hours, direction)
            target = shift_trading_hours(base_end, -hours if direction == "before" else hours, calendar)
            if direction == "after" and target > cutoff:
                windows.append(WindowResult(label, target, None, None, None, "pending", None, None, None))
                continue
            found = next(((end, bar) for end, bar in reversed(eligible) if end <= target), None)
            if found is None:
                windows.append(WindowResult(label, target, None, None, None, "missing", None, None, None,
                                            "missing_data"))
                continue
            end, bar = found
            gap = trading_seconds_between(end, target, calendar) if end <= target else 0
            if gap > 2 * 3600:
                windows.append(WindowResult(label, target, None, None, None, "missing", None, None, None,
                                            "missing_data"))
                continue
            status = ("on_time" if gap < 3600 else
                      ("deferred_back" if direction == "before" else "deferred"))
            value = ((baseline.price / bar.close - 1) if direction == "before"
                     else (bar.close / baseline.price - 1)) * 100
            flag = "zero_volume" if _hourly_zero_volume(asset, bar, end, calendar) else None
            windows.append(WindowResult(label, target, end, bar.close, value, status,
                                        int((end-target).total_seconds()), bar.source, bar.basis, flag))
    session = calendar.session_of(event)
    if calendar.market == "NYMEX":
        # Hourly futures use the Globex session, not the 14:30 settlement close.
        session = ("regular_session" if trading_seconds_between(event, event + timedelta(seconds=1), calendar)
                   else "after_close")
    return EventResult(asset, event, baseline, int((event-base_end).total_seconds()), "on_time",
                       tuple(windows), mode="hourly", session=session,
                       baseline_flag=("zero_volume" if _hourly_zero_volume(asset, base_bar, base_end, calendar) else None))


def _hourly_zero_volume(asset: Asset, bar: PriceBar, end: datetime, calendar) -> bool:
    """Zero-volume flag for an hourly bar, skipping known Yahoo artifacts.

    Indices have no volume. Yahoo's HK 15:30 bar (ends 16:00) and the NYMEX
    Globex reopen bar (18:00 ET stamp, ends 19:00) often report volume 0 even
    on normal days, so those bars are not flagged; half days are skipped too.
    """
    if bar.volume != 0 or asset.ticker.startswith("^"):
        return False
    try:
        if calendar.day(end.astimezone(calendar.tz).date()).half_day:
            return False
    except ValueError:
        pass
    local_end = end.astimezone(calendar.tz)
    if calendar.market == "XHKG" and (local_end.hour, local_end.minute) == (16, 0):
        return False
    if calendar.market == "NYMEX" and bar.timestamp.astimezone(calendar.tz).hour == 18:
        return False
    return True


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
    from .market_calendars import calendar_for

    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("News timestamp requires a timezone")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of requires a timezone")
    calendar = calendar or calendar_for(getattr(asset, "market", "XHKG"))
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
        # Daily bars are date-only; providers stamp them on their trading date.
        day = bar.timestamp.date()
        try:
            info = calendar.day(day)
        except ValueError:
            continue
        if not info.is_trading or info.close_at() > as_of:
            continue
        closes[day] = Observation(info.close_at(), bar.close, bar.source, bar.basis,
                                  getattr(bar, "volume", None))

    def _flag(observation, gap_days):
        if observation is None or gap_days:
            return "missing_data"  # trading day without a bar: suspension or data gap
        info = calendar.day(observation.observed_at.date())
        # Index volume from free feeds is unreliable, so only securities get this flag.
        if observation.volume == 0 and not info.half_day and not asset.ticker.startswith("^"):
            return "zero_volume"
        return None

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
    baseline_flag = _flag(baseline, baseline_gap_days) or (
        "nonpositive_price" if baseline.price <= 0 else None)

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
                                        status, None, None, None,
                                        "missing_data" if status == "missing" else None))
            continue
        if match.basis != baseline.basis:
            raise ValueError(f"Mixed price basis for {asset.ticker}: {baseline.basis} and {match.basis}")
        nonpositive = baseline.price <= 0 or match.price <= 0
        if target.direction == "before":
            value = None if nonpositive else (baseline.price - match.price) / match.price * 100
            status = "on_date" if gap_days == 0 else "deferred_back"
        else:
            value = None if nonpositive else (match.price - baseline.price) / baseline.price * 100
            status = "on_date" if gap_days == 0 else "deferred"
        delay = int((match.observed_at - target.target).total_seconds())
        flag = _flag(match, gap_days) or ("nonpositive_price" if nonpositive else None)
        results.append(WindowResult(
            target.label, target.target, match.observed_at, match.price, value,
            status, delay, match.source, match.basis, flag,
        ))
    return EventResult(
        asset=asset, published_at=event_time, baseline=baseline,
        baseline_gap_seconds=baseline_gap, baseline_status=baseline_status,
        windows=tuple(results), mode="daily", session=session, t0=t0,
        baseline_flag=baseline_flag,
    )
