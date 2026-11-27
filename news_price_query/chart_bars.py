"""OHLC bars for the K-line chart, read from the local price database.

Hourly candles come from ``hourly_prices``. Daily candles are aggregated from
those hourly bars (open of the first bar, high/low extremes, close of the last
bar, sum of volume). The ``daily_prices`` table only stores close, so a day
without hourly coverage is omitted rather than turned into a fake candle.
Missing sessions are returned as gaps; bars are never invented.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from news_price_query.intraday_sync import IntradayUnavailable, MINUTE_INTERVALS, minute_bar_end, sync_intraday
from news_price_query.market_calendars import MARKETS, calendar_for
from news_price_query.price_store import HourlyRow, PriceStore


UTC = ZoneInfo("UTC")

INTERVALS = {
    **{kind: kind for kind in MINUTE_INTERVALS},
    "1h": "1h",
    "hour": "1h",
    "hourly": "1h",
    "60m": "1h",
    "1d": "1d",
    "day": "1d",
    "daily": "1d",
    "d": "1d",
}

MAX_BARS = 8000
# Cap short-interval requests even for nearly 24-hour futures sessions.
MAX_SPAN = {
    **{kind: timedelta(days=min(60, MAX_BARS * minutes // 1440)) for kind, minutes in MINUTE_INTERVALS.items()},
    "1h": timedelta(days=800), "1d": timedelta(days=4000),
}

TZ_LABEL = {
    "Asia/Hong_Kong": "HKT",
    "America/New_York": "纽约时间",
    "America/Toronto": "多伦多时间",
    "Asia/Riyadh": "利雅得时间",
    "Europe/London": "伦敦时间",
}
TZ_LABEL_EN = {
    "Asia/Hong_Kong": "HKT",
    "America/New_York": "New York time",
    "America/Toronto": "Toronto time",
    "Asia/Riyadh": "Riyadh time",
    "Europe/London": "London time",
}


def normalize_interval(value: str) -> str:
    key = "".join(str(value).casefold().split())
    try:
        return INTERVALS[key]
    except KeyError:
        raise ValueError("K线周期只支持已知分钟周期、1小时（1h）或日线（1d）") from None


def exchange_timezone(market: str) -> ZoneInfo:
    try:
        name = MARKETS[market][0]
    except KeyError:
        name = "Asia/Hong_Kong"
    return ZoneInfo(name)


def parse_bound(value: str, *, end: bool, tz: ZoneInfo) -> datetime:
    text = str(value).strip()
    if not text:
        raise ValueError("开始和结束时间不能为空")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        day = date.fromisoformat(text)
        clock = time(23, 59, 59) if end else time(0, 0, 0)
        return datetime.combine(day, clock, tz)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("时间格式无效，请使用 YYYY-MM-DD 或带时区的 ISO 时间") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(tz)


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hourly_payload(row: HourlyRow) -> dict:
    return {
        "time": row.bar_start_local,
        "open": row.open,
        "high": row.high,
        "low": row.low,
        "close": row.close,
        "volume": row.volume,
    }


def _aggregate_daily(rows: list[HourlyRow]) -> list[dict]:
    grouped: dict[str, list[HourlyRow]] = {}
    for row in rows:
        day = datetime.fromisoformat(row.bar_start_local).date().isoformat()
        grouped.setdefault(day, []).append(row)
    bars = []
    for day, items in grouped.items():
        volumes = [item.volume for item in items if item.volume is not None]
        bars.append({
            "time": day,
            "open": items[0].open,
            "high": max(item.high for item in items),
            "low": min(item.low for item in items),
            "close": items[-1].close,
            "volume": sum(volumes) if volumes else None,
            "hourly_bars": len(items),
        })
    return bars


def chart_bars(store: PriceStore, asset, interval: str, start: str, end: str, *,
               now: datetime | None = None, sync_minutes: bool = False) -> dict:
    """Return OHLC bars for ``asset`` between ``start`` and ``end`` inclusive."""
    kind = normalize_interval(interval)
    tz = exchange_timezone(getattr(asset, "market", None) or "XHKG")
    first = parse_bound(start, end=False, tz=tz)
    last = parse_bound(end, end=True, tz=tz)
    if last < first:
        raise ValueError("开始时间不能晚于结束时间")
    if last - first > MAX_SPAN[kind]:
        days = MAX_SPAN[kind].days
        label = {"1h": "1小时", "1d": "日线"}.get(kind, kind)
        raise ValueError(f"{label}最多查询 {days} 天，请缩小时间范围")

    ticker = asset.ticker
    minute = kind in MINUTE_INTERVALS
    stored_kind = "1h" if kind == "1d" else kind
    if minute:
        now = now or datetime.now(tz)
        minute_deadline = min(last, now)
        calendar = calendar_for(asset.market)
    if minute and sync_minutes:
        try:
            sync_intraday(store, asset, kind, first, last, now=now)
        except IntradayUnavailable:
            # An upstream failure does not hide the stored observations.
            pass
    rows = store.hourly_rows(ticker, _utc_stamp(first), _utc_stamp(last), interval=stored_kind)
    # hourly_rows uses inclusive UTC string comparison on bar_start_utc. A
    # date-only end of 23:59:59 local may land before a bar that starts that
    # minute; that is intended. Bars starting after `last` are dropped below
    # as a belt-and-suspenders check using the exchange-local timestamp.
    kept = []
    for row in rows:
        local = datetime.fromisoformat(row.bar_start_local)
        if not first <= local <= last:
            continue
        if minute:
            bar_end = minute_bar_end(local, calendar, kind)
            if bar_end is None or bar_end > minute_deadline:
                continue
        kept.append(row)

    if kind != "1d":
        bars = [_hourly_payload(row) for row in kept]
        source = "intraday_prices" if minute else "hourly_prices"
    else:
        bars = _aggregate_daily(kept)
        source = "hourly_aggregate"

    if len(bars) > MAX_BARS:
        raise ValueError(f"结果超过 {MAX_BARS} 根K线上限，请缩小时间范围")

    coverage = store.hourly_coverage(ticker, interval=stored_kind)
    missing = len(bars) == 0
    message_en = None
    if minute:
        message = message_en = None
        if missing:
            message = f"该区间没有 {kind} 分钟线"
            message_en = f"No {kind} prices in this range. Try a different date or a longer interval."
            if coverage["stored_rows"]:
                message_en += f" Stored coverage: {coverage['earliest_bar']} to {coverage['latest_bar']}."
    elif missing:
        message = "缺数据"
        if coverage["stored_rows"]:
            earliest = coverage["earliest_bar"]
            latest = coverage["latest_bar"]
            message = (
                f"缺数据：该区间没有可用的小时线"
                f"（本地小时线约 {earliest} 至 {latest}）。"
                "日K线由小时线合成，不会用仅有收盘价的日线补造开高低。"
            )
            message_en = f"No hourly prices in this range. Stored coverage: {earliest} to {latest}."
        else:
            message = "缺数据：本地价格库没有该标的的小时线"
            message_en = "No hourly prices are stored for this symbol."
    else:
        message = None
        if kind == "1d":
            message = "日K线由本地小时线合成；没有小时线的交易日留空，不补造。"
            message_en = "Daily candles use stored hourly data. Sessions with no hourly data are omitted."

    tz_name = str(tz)
    return {
        "symbol": ticker,
        "interval": kind,
        "exchange_timezone": tz_name,
        "time_label": TZ_LABEL.get(tz_name, tz_name),
        "time_label_en": TZ_LABEL_EN.get(tz_name, tz_name),
        "start": first.isoformat(),
        "end": last.isoformat(),
        "source": source,
        "price_sources": sorted({row.source for row in kept}),
        "bars": bars,
        "bar_count": len(bars),
        "missing": missing,
        "message": message,
        "message_en": message_en,
        "coverage": ({"interval": kind, **coverage} if minute else {
            "hourly_from": coverage["earliest_bar"],
            "hourly_to": coverage["latest_bar"],
            "hourly_rows": coverage["stored_rows"],
        }),
        "limits": {
            "max_span_days_1h": MAX_SPAN["1h"].days,
            "max_span_days_1d": MAX_SPAN["1d"].days,
            "max_bars": MAX_BARS,
        },
    }
