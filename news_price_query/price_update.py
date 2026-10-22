"""Incremental daily-price update into the local store.

Only Hong Kong (XHKG) is implemented. market/currency are stored per ticker and
calendar_for(market) picks the calendar, so other markets can be added later.
"""

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .hkex_calendar import default_calendar
from .price_store import DailyRow, Instrument, PriceStore


HK = ZoneInfo("Asia/Hong_Kong")
ALWAYS_INCLUDED = (("2800.HK", "盈富基金", ("Tracker Fund of Hong Kong",), "benchmark"),
                   ("^HSI", "恒生指数", ("Hang Seng Index", "恒指"), "benchmark"))


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    name: str | None = None
    aliases: tuple[str, ...] = ()
    role: str = "equity"


@dataclass
class TickerSummary:
    ticker: str
    market: str = ""
    rows_written: int = 0
    first_date: str | None = None
    last_date: str | None = None
    stored_rows: int = 0
    calendar_trading_days: int = 0
    missing_trading_days: list = field(default_factory=list)
    dropped_non_trading: list = field(default_factory=list)
    dropped_unfinalized: list = field(default_factory=list)
    rebased: bool = False
    error: str | None = None


def market_of(ticker: str) -> tuple[str, str]:
    if ticker.upper().endswith(".HK") or ticker.upper() in ("^HSI",):
        return "XHKG", "HKD"
    raise NotImplementedError(f"{ticker}: only Hong Kong tickers are implemented so far")


def calendar_for(market: str):
    if market == "XHKG":
        return default_calendar()
    raise NotImplementedError(f"No trading calendar for market {market}")


def _normalize(code: str) -> str:
    text = code.strip().upper()
    if re.fullmatch(r"\d{1,5}", text):
        return f"{int(text):04d}.HK"
    match = re.fullmatch(r"(\d{1,5})\.HK", text)
    if match:
        return f"{int(match.group(1)):04d}.HK"
    return text


def read_universe(path: str | Path) -> list[UniverseEntry]:
    """Read tickers from a CSV. Accepts a `ticker` or `yfinance代码` column, or a
    `代码` column of HK stock numbers. Optional name/alias columns are kept."""
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        code_field = next((f for f in ("ticker", "yfinance代码", "yf_ticker", "代码") if f in fields), None)
        if code_field is None:
            raise ValueError(f"{path}: needs a ticker, yfinance代码 or 代码 column")
        entries = []
        for row in reader:
            code = (row.get(code_field) or "").strip()
            if not code:
                continue
            aliases = []
            for key in ("英文名", "常用别名", "aliases"):
                aliases += [a.strip() for a in re.split(r"[;|；]", row.get(key) or "") if a.strip()]
            name = (row.get("中文名") or row.get("name") or "").strip() or None
            entries.append(UniverseEntry(_normalize(code), name, tuple(dict.fromkeys(aliases))))
    return entries


def _clean(raw_rows, calendar, today: date, summary: TickerSummary):
    kept = []
    for item in raw_rows:
        day = date.fromisoformat(item["date"])
        close = item.get("close")
        if close is None or not isfinite(close) or close <= 0:
            continue
        if day >= today:
            summary.dropped_unfinalized.append(item["date"])
            continue
        try:
            trading = calendar.is_trading_day(day)
        except ValueError:
            trading = False
        if not trading:
            summary.dropped_non_trading.append(item["date"])
            continue
        adj = item.get("adj_close")
        volume = item.get("volume")
        kept.append({
            "date": item["date"], "close": float(close),
            "adj_close": float(adj) if adj is not None and isfinite(adj) else None,
            "volume": float(volume) if volume is not None and isfinite(volume) else None,
        })
    return kept


def _rebased(store: PriceStore, ticker: str, rows: list[dict]) -> bool:
    """True if the adj/close ratio changed on overlapping dates (new dividend or
    split moves the whole adjusted history, so it must be refetched)."""
    if not rows:
        return False
    stored = {row.date: row for row in store.rows(ticker, rows[0]["date"], rows[-1]["date"])}
    for item in rows:
        old = stored.get(item["date"])
        if old is None or old.adj_close is None or item["adj_close"] is None:
            continue
        if abs(old.adj_close / old.close - item["adj_close"] / item["close"]) > 1e-6:
            return True
    return False


def update_prices(store: PriceStore, universe: list[UniverseEntry], downloader, *, now: datetime,
                  start: str = "2000-01-01", overlap_days: int = 10, source: str = "yfinance",
                  universe_label: str | None = None) -> dict:
    """downloader(ticker, start_date_iso, end_date_iso_exclusive) -> list of
    {date, close, adj_close, volume}."""
    entries = list(universe)
    known = {entry.ticker for entry in entries}
    for ticker, name, aliases, role in ALWAYS_INCLUDED:
        if ticker not in known:
            entries.append(UniverseEntry(ticker, name, aliases, role))
    today = now.astimezone(HK).date()
    run_id = store.start_run(source, universe_label, len(entries), now)
    fetched_at = now.isoformat()
    summaries = []
    written = 0
    for entry in entries:
        summary = TickerSummary(entry.ticker)
        summaries.append(summary)
        try:
            market, currency = market_of(entry.ticker)
            summary.market = market
            calendar = calendar_for(market)
            store.upsert_instrument(Instrument(entry.ticker, market, currency, entry.name,
                                               entry.aliases, entry.role))
            last = store.last_date(entry.ticker)
            fetch_from = start
            if last is not None:
                fetch_from = calendar.shift_trading_days(date.fromisoformat(last), -overlap_days).isoformat()
            rows = _clean(downloader(entry.ticker, fetch_from, today.isoformat()), calendar, today, summary)
            if last is not None and _rebased(store, entry.ticker, rows):
                summary.rebased = True
                summary.dropped_non_trading.clear()
                summary.dropped_unfinalized.clear()
                rows = _clean(downloader(entry.ticker, start, today.isoformat()), calendar, today, summary)
                store.delete_ticker(entry.ticker)
            daily = [DailyRow(entry.ticker, market, currency, item["date"], item["close"],
                              item["adj_close"], item["volume"], source, fetched_at, run_id)
                     for item in rows]
            summary.rows_written = store.upsert_rows(daily)
            written += summary.rows_written
            stored = store.rows(entry.ticker)
            summary.stored_rows = len(stored)
            if stored:
                summary.first_date, summary.last_date = stored[0].date, stored[-1].date
                have = {row.date for row in stored}
                day = date.fromisoformat(summary.first_date)
                end = date.fromisoformat(summary.last_date)
                expected = []
                while True:
                    if calendar.is_trading_day(day):
                        expected.append(day.isoformat())
                    if day >= end:
                        break
                    day = calendar.shift_trading_days(day, 1)
                summary.calendar_trading_days = len(expected)
                summary.missing_trading_days = [d for d in expected if d not in have]
        except (NotImplementedError, ValueError, RuntimeError, OSError) as exc:
            summary.error = str(exc)
    failures = [s for s in summaries if s.error]
    status = "ok" if not failures else ("partial" if len(failures) < len(summaries) else "failed")
    store.finish_run(run_id, datetime.now(HK), written, status,
                     f"{len(failures)} ticker(s) failed" if failures else None)
    return {"run_id": run_id, "status": status, "rows_written": written,
            "db": str(store.path), "tickers": summaries}


def yfinance_downloader(ticker: str, start: str, end: str) -> list[dict]:
    """Fetch daily bars with yfinance (personal/internal research use only).
    `end` is exclusive. Close is split-adjusted only; Adj Close adds dividends."""
    import yfinance as yf

    frame = yf.download(ticker, start=start, end=end, auto_adjust=False, actions=False,
                        progress=False, multi_level_index=False, threads=False)
    if frame is None or frame.empty:
        return []
    output = []
    for stamp, row in frame.iterrows():
        output.append({
            "date": stamp.date().isoformat(),
            # yfinance returns float32-like noise (23.399999618); HK prices have <= 3 decimals.
            "close": round(float(row["Close"]), 4),
            "adj_close": round(float(row["Adj Close"]), 6) if "Adj Close" in row else None,
            "volume": float(row["Volume"]) if "Volume" in row else None,
        })
    return output
