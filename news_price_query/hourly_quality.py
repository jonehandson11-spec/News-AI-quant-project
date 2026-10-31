"""小时线覆盖完整性检查：每个交易日最后一根 bar 是否覆盖到了收盘。

为什么不用"每天应该有 7 根"来判断：不同数据源的 bar 对齐网格不同。
Yahoo 港股按 UTC 整点切，本地时间落在 :30（09:30 / 10:30 / 11:30 / 12:30 …），
一天 7 根；东方财富按交易时段切，只有 6 根。硬编码根数会把正常数据误判成缺失。

改用"最后一根的结束时间是否等于收盘时间"来判断：这条对所有市场都成立，
而且正好抓住真实故障——半日市、提前收盘日少存最后一根时，收盘价会被错取。

数据来源仍是本地价格库，不需要联网，可复现、可审计。
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Iterator

from .market_calendars import calendar_for
from .price_store import PriceStore
from .windows import hourly_bar_end, session_segments


class _Stamp:
    """让 hourly_bar_end 直接吃一个 datetime，复用生产环境的同一套对齐逻辑。"""

    __slots__ = ("timestamp",)

    def __init__(self, timestamp: datetime):
        self.timestamp = timestamp


@dataclass(frozen=True)
class DayCoverage:
    """一个交易日的覆盖情况。uncovered_minutes>0 表示收盘价不可信。"""

    ticker: str
    market: str
    day: date
    session_open: datetime
    session_close: datetime
    bars: int
    first_start: datetime | None
    last_end: datetime | None
    uncovered_minutes: int
    extra_bars: int = 0

    @property
    def ok(self) -> bool:
        return self.uncovered_minutes == 0

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "market": self.market,
            "date": self.day.isoformat(),
            "session_open": self.session_open.isoformat(),
            "session_close": self.session_close.isoformat(),
            "bars": self.bars,
            "first_start": self.first_start.isoformat() if self.first_start else None,
            "last_end": self.last_end.isoformat() if self.last_end else None,
            "uncovered_minutes": self.uncovered_minutes,
            "extra_bars": self.extra_bars,
            "ok": self.ok,
        }


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def day_coverage(ticker: str, market: str, day: date, starts: Iterable[datetime],
                 calendar) -> DayCoverage | None:
    """算出某只标的某个交易日的覆盖情况；非交易日返回 None。"""
    segments = session_segments(day, calendar)
    if not segments:
        return None
    session_open, session_close = segments[0][0], segments[-1][1]

    inside: list[datetime] = []
    ends: list[datetime] = []
    for start in starts:
        end = hourly_bar_end(_Stamp(start), calendar)
        if end is None:
            continue
        inside.append(start)
        ends.append(end)
    inside.sort()
    ends.sort()

    first_start = inside[0] if inside else None
    last_end = ends[-1] if ends else None
    uncovered = 0
    if last_end is not None and last_end < session_close:
        uncovered = int((session_close - last_end).total_seconds() // 60)
    # 落在交易时段之外的多余 bar（例如把休市时间也算进去），只计数不报错
    sessions_seconds = sum((b - a).total_seconds() for a, b in segments)
    extra = max(0, len(inside) - int(sessions_seconds // 3600))

    return DayCoverage(ticker, market, day, session_open, session_close, len(inside),
                       first_start, last_end, uncovered, extra)


def scan(store: PriceStore, tickers: Iterable[str] | None = None,
         start: date | None = None, end: date | None = None,
         include_contracts: bool = False) -> Iterator[DayCoverage]:
    """逐标的、逐交易日扫描小时线覆盖情况。

    固定月份合约（role=contract）在 Yahoo 上本身就是稀疏行情，一天常常只有一两根，
    默认跳过，否则会淹没真正需要处理的缺口；需要时用 include_contracts=True。
    """
    instruments = list(store.instruments())
    markets = {row.ticker: row.market for row in instruments}
    roles = {row.ticker: getattr(row, "role", None) for row in instruments}
    for ticker in (tickers or sorted(markets)):
        market = markets.get(ticker)
        if market is None:
            continue
        if not include_contracts and roles.get(ticker) == "contract":
            continue
        try:
            calendar = calendar_for(market)
        except NotImplementedError:
            continue
        by_day: dict[date, list[datetime]] = {}
        for row in store.hourly_rows(ticker):
            stamp = _parse(row.bar_start_local)
            local_day = stamp.date()
            if start and local_day < start:
                continue
            if end and local_day > end:
                continue
            by_day.setdefault(local_day, []).append(stamp)
        for day in sorted(by_day):
            coverage = day_coverage(ticker, market, day, by_day[day], calendar)
            if coverage is not None:
                yield coverage


@dataclass
class ScanSummary:
    checked_days: int = 0
    incomplete_days: list[DayCoverage] = field(default_factory=list)
    worst_uncovered_minutes: int = 0

    def as_dict(self) -> dict:
        return {
            "checked_days": self.checked_days,
            "incomplete_count": len(self.incomplete_days),
            "worst_uncovered_minutes": self.worst_uncovered_minutes,
            "incomplete": [item.as_dict() for item in self.incomplete_days],
        }


def summarize(items: Iterable[DayCoverage]) -> ScanSummary:
    summary = ScanSummary()
    for item in items:
        summary.checked_days += 1
        if not item.ok:
            summary.incomplete_days.append(item)
            summary.worst_uncovered_minutes = max(summary.worst_uncovered_minutes,
                                                  item.uncovered_minutes)
    return summary
