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

from news_price_query.market_calendars import MARKETS
from news_price_query.price_store import HourlyRow, PriceStore


UTC = ZoneInfo("UTC")

INTERVALS = {
    "1h": "1h",
    "hour": "1h",
    "hourly": "1h",
    "60m": "1h",
    "1d": "1d",
    "day": "1d",
    "daily": "1d",
    "d": "1d",
}

# About two years of 1-hour bars (the local store's typical hourly coverage).
MAX_SPAN = {"1h": timedelta(days=800), "1d": timedelta(days=4000)}
MAX_BARS = 8000

TZ_LABEL = {
    "Asia/Hong_Kong": "HKT",
    "America/New_York": "纽约时间",
    "America/Toronto": "多伦多时间",
    "Asia/Riyadh": "利雅得时间",
    "Europe/London": "伦敦时间",
}


def normalize_interval(value: str) -> str:
    key = "".join(str(value).casefold().split())
    try:
        return INTERVALS[key]
    except KeyError:
        raise ValueError("K线周期只支持 1小时（1h）或 日线（1d）") from None


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


def chart_bars(store: PriceStore, asset, interval: str, start: str, end: str) -> dict:
    """Return OHLC bars for ``asset`` between ``start`` and ``end`` inclusive."""
    kind = normalize_interval(interval)
    tz = exchange_timezone(getattr(asset, "market", None) or "XHKG")
    first = parse_bound(start, end=False, tz=tz)
    last = parse_bound(end, end=True, tz=tz)
    if last < first:
        raise ValueError("开始时间不能晚于结束时间")
    if last - first > MAX_SPAN[kind]:
        days = MAX_SPAN[kind].days
        label = "1小时" if kind == "1h" else "日线"
        raise ValueError(f"{label}最多查询 {days} 天，请缩小时间范围")

    ticker = asset.ticker
    rows = store.hourly_rows(ticker, _utc_stamp(first), _utc_stamp(last))
    # hourly_rows uses inclusive UTC string comparison on bar_start_utc. A
    # date-only end of 23:59:59 local may land before a bar that starts that
    # minute; that is intended. Bars starting after `last` are dropped below
    # as a belt-and-suspenders check using the exchange-local timestamp.
    kept = []
    for row in rows:
        local = datetime.fromisoformat(row.bar_start_local)
        if first <= local <= last:
            kept.append(row)

    if kind == "1h":
        bars = [_hourly_payload(row) for row in kept]
        source = "hourly_prices"
    else:
        bars = _aggregate_daily(kept)
        source = "hourly_aggregate"

    if len(bars) > MAX_BARS:
        raise ValueError(f"结果超过 {MAX_BARS} 根K线上限，请缩小时间范围")

    coverage = store.hourly_coverage(ticker)
    missing = len(bars) == 0
    if missing:
        message = "缺数据"
        if coverage["stored_rows"]:
            earliest = coverage["earliest_bar"]
            latest = coverage["latest_bar"]
            message = (
                f"缺数据：该区间没有可用的小时线"
                f"（本地小时线约 {earliest} 至 {latest}）。"
                "日K线由小时线合成，不会用仅有收盘价的日线补造开高低。"
            )
        else:
            message = "缺数据：本地价格库没有该标的的小时线"
    else:
        message = None
        if kind == "1d":
            message = "日K线由本地小时线合成；没有小时线的交易日留空，不补造。"

    tz_name = str(tz)
    return {
        "symbol": ticker,
        "interval": kind,
        "exchange_timezone": tz_name,
        "time_label": TZ_LABEL.get(tz_name, tz_name),
        "start": first.isoformat(),
        "end": last.isoformat(),
        "source": source,
        "bars": bars,
        "bar_count": len(bars),
        "missing": missing,
        "message": message,
        "coverage": {
            "hourly_from": coverage["earliest_bar"],
            "hourly_to": coverage["latest_bar"],
            "hourly_rows": coverage["stored_rows"],
        },
        "limits": {
            "max_span_days_1h": MAX_SPAN["1h"].days,
            "max_span_days_1d": MAX_SPAN["1d"].days,
            "max_bars": MAX_BARS,
        },
    }
