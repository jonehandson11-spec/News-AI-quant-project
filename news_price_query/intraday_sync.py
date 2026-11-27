"""Read-through cache for recent native-resolution Yahoo Finance candles."""

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import isfinite
from threading import BoundedSemaphore, Lock
from time import monotonic

from .market_calendars import calendar_for
from .price_store import HourlyRow
from .windows import session_segments


MINUTE_INTERVALS = {"1m": 1, "2m": 2, "5m": 5, "15m": 15, "30m": 30}
MINUTE_LOOKBACK_DAYS = {"1m": 30, "2m": 60, "5m": 60, "15m": 60, "30m": 60}
ONE_MINUTE_REQUEST_SPAN = timedelta(days=7)
SOURCE = "yfinance-ondemand"
_SOURCE_30M = "yfinance-ondemand:15m-resampled"
# Bound concurrent downloads and suppress repeated requests for the same window.
_DOWNLOAD_SLOTS = BoundedSemaphore(4)
_FETCH_LOCKS = tuple(Lock() for _ in range(64))
_RECENT_FETCHES = OrderedDict()
_RECENT_GUARD = Lock()


class IntradayUnavailable(RuntimeError):
    """Yahoo could not answer the current request."""


def intraday_query_window(interval, start, cutoff, *, now):
    """Return the part of the requested window that Yahoo can still provide."""
    if interval not in MINUTE_INTERVALS:
        raise ValueError("Unsupported minute interval.")
    if any(value.utcoffset() is None for value in (start, cutoff, now)):
        raise ValueError("Minute price queries require timezone-aware timestamps.")
    # Leave a minute of headroom at the retention boundary for processing time.
    latest = now.astimezone(timezone.utc)
    lookback = timedelta(days=MINUTE_LOOKBACK_DAYS[interval], minutes=-1)
    first = max(start.astimezone(timezone.utc), latest - lookback)
    last = min(cutoff.astimezone(timezone.utc), latest)
    first_minute = first.replace(second=0, microsecond=0)
    if first > first_minute:
        first_minute += timedelta(minutes=1)
    last_minute = last.replace(second=0, microsecond=0)
    if last_minute - first_minute < timedelta(minutes=MINUTE_INTERVALS[interval]):
        return None
    return first_minute, last_minute


def minute_bar_end(timestamp, calendar, interval):
    """Resolve a bar's end within the exchange session, including early closes."""
    start = timestamp.astimezone(calendar.tz)
    trade_date = start.date()
    days = (trade_date, trade_date + timedelta(days=1)) if calendar.market == "NYMEX" else (trade_date,)
    for day in days:
        if calendar.first <= day <= calendar.last:
            for opens, closes in session_segments(day, calendar):
                if opens <= start < closes:
                    return min(start + timedelta(minutes=MINUTE_INTERVALS[interval]), closes)
    return None


def _has_session(first, last, calendar, interval):
    local_start, local_end = first.astimezone(calendar.tz), last.astimezone(calendar.tz)
    day = max(calendar.first, local_start.date())
    end_day = local_end.date() + timedelta(days=calendar.market == "NYMEX")
    duration = timedelta(minutes=MINUTE_INTERVALS[interval])
    while day <= min(calendar.last, end_day):
        for opens, closes in session_segments(day, calendar):
            if min(local_end, closes) - max(local_start, opens) >= duration:
                return True
        day += timedelta(days=1)
    return False


def _download(ticker, interval, start, end):
    import yfinance as yf
    from yfinance.exceptions import YFPricesMissingError

    try:
        frame = yf.Ticker(ticker).history(interval=interval, start=start, end=end,
                                          auto_adjust=False, actions=False, prepost=False,
                                          timeout=8, raise_errors=True)
    except YFPricesMissingError:
        return []
    if frame is None or frame.empty:
        return []
    return [{"timestamp": stamp, "open": row["Open"], "high": row["High"],
             "low": row["Low"], "close": row["Close"],
             "adj_close": row.get("Adj Close"), "volume": row.get("Volume")}
            for stamp, row in frame.iterrows()]


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _to_row(item, asset, calendar, interval, first, last, now):
    stamp = item.get("timestamp")
    if hasattr(stamp, "to_pydatetime"):
        stamp = stamp.to_pydatetime()
    if not isinstance(stamp, datetime):
        return None
    if stamp.utcoffset() is None:
        stamp = stamp.replace(tzinfo=calendar.tz)
    if not first <= stamp < last:
        return None
    bar_end = minute_bar_end(stamp, calendar, interval)
    if bar_end is None or bar_end > last:
        return None
    open_, high, low, close = (_number(item.get(key)) for key in ("open", "high", "low", "close"))
    if None in (open_, high, low, close) or not low <= min(open_, close) <= max(open_, close) <= high:
        return None
    volume = _number(item.get("volume"))
    return HourlyRow(asset.ticker, asset.market, asset.currency, interval,
                     stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     calendar.tz.key, stamp.astimezone(calendar.tz).isoformat(),
                     open_, high, low, close, _number(item.get("adj_close")),
                     volume if volume is not None and volume >= 0 else None,
                     _SOURCE_30M if interval == "30m" else SOURCE, now.isoformat(), "")


def sync_intraday(store, asset, interval, start, cutoff, *, now, downloader=None):
    """Cache completed native candles in an exact requested window.

    ``downloader(ticker, interval, start, end)`` returns timezone-aware OHLCV
    items. A missing or stale window returns zero. Connector failure raises
    ``IntradayUnavailable`` so callers can fall back to persistent data.
    """
    window = intraday_query_window(interval, start, cutoff, now=now)
    if window is None:
        return 0
    first, last = window
    calendar = calendar_for(asset.market)
    if not _has_session(first, last, calendar, interval):
        return 0

    ticker = asset.ticker
    lock = _FETCH_LOCKS[hash((ticker, interval)) % len(_FETCH_LOCKS)]
    if not lock.acquire(timeout=9):
        raise IntradayUnavailable("Minute price service is busy.")
    try:
        key = (store.path.resolve(), ticker, interval, first, last)
        if downloader is None:
            with _RECENT_GUARD:
                current = monotonic()
                if any(previous[:3] == key[:3] and previous[3] <= first and last <= previous[4]
                       and expires > current for previous, expires in _RECENT_FETCHES.items()):
                    return 0
        if not _DOWNLOAD_SLOTS.acquire(timeout=1):
            raise IntradayUnavailable("Minute price service is busy.")
        try:
            items = []
            chunk_start = first
            while chunk_start < last:
                chunk_end = min(chunk_start + ONE_MINUTE_REQUEST_SPAN, last) if interval == "1m" else last
                if _has_session(chunk_start, chunk_end, calendar, interval):
                    items.extend((downloader or _download)(asset.data_ticker or ticker, interval,
                                                           chunk_start, chunk_end))
                chunk_start = chunk_end
        except Exception as error:
            raise IntradayUnavailable("Yahoo minute prices are temporarily unavailable.") from error
        finally:
            _DOWNLOAD_SLOTS.release()

        rows = {}
        for item in items:
            row = _to_row(item, asset, calendar, interval, first, last, now)
            if row is not None:
                rows[row.bar_start_utc] = row
        count = 0
        if rows:
            # A supplement from Yahoo must not replace bars from another feed.
            for existing in store.hourly_rows(ticker, min(rows), max(rows), interval=interval):
                if existing.source not in (SOURCE, _SOURCE_30M):
                    rows.pop(existing.bar_start_utc, None)
            if rows:
                run_id = store.start_run(SOURCE, f"intraday:{ticker}:{interval}", 1, now)
                try:
                    count = store.upsert_hourly_rows([replace(row, run_id=run_id) for row in rows.values()])
                except Exception:
                    store.finish_run(run_id, datetime.now(timezone.utc), 0, "failed")
                    raise
                store.finish_run(run_id, datetime.now(timezone.utc), count)
        if downloader is None:
            with _RECENT_GUARD:
                _RECENT_FETCHES[key] = monotonic() + 30
                _RECENT_FETCHES.move_to_end(key)
                if len(_RECENT_FETCHES) > 128:
                    _RECENT_FETCHES.popitem(last=False)
        return count
    finally:
        lock.release()
