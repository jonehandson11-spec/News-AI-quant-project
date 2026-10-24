"""Incremental daily-price update into the local store.

Markets: XHKG, XNYS (NYSE/Nasdaq), XTSE, XSAU, NYMEX. market/currency come
from the asset registry or ticker suffix; calendar_for(market) picks the calendar.
"""

import csv
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .assets import market_of_ticker, resolve_asset
from .daily_report import BENCHMARKS_BY_MARKET
from .futures import (METHOD as FUTURES_METHOD, contract_ticker, front_contract,
                      is_contract_ticker, next_contract, roll_dates, root_for_ticker)
from .market_calendars import calendar_for
from .price_store import DailyRow, FuturesRollRow, Instrument, PriceStore


HK = ZoneInfo("Asia/Hong_Kong")
ALWAYS_INCLUDED = (("2800.HK", "盈富基金", ("Tracker Fund of Hong Kong",), "benchmark"),
                   ("^HSI", "恒生指数", ("Hang Seng Index", "恒指"), "benchmark"))


def _benchmark_entry(ticker: str) -> "UniverseEntry":
    try:
        asset = resolve_asset(ticker)
        return UniverseEntry(ticker, asset.name_zh or asset.aliases[0], asset.aliases, "benchmark")
    except ValueError:
        return UniverseEntry(ticker, None, (), "benchmark")


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
    rolls: int = 0
    derived_contract_rows: int = 0
    error: str | None = None
    skipped: str | None = None


def market_of(ticker: str) -> tuple[str, str]:
    return market_of_ticker(ticker)


def _normalize(code: str) -> str:
    text = code.strip().upper()
    if re.fullmatch(r"\d{1,3}|\d{5}|0\d{3}", text):
        return f"{int(text):04d}.HK"  # HK stock numbers such as 00883 / 0883
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
    futures = root_for_ticker(summary.ticker) is not None or is_contract_ticker(summary.ticker)
    for item in raw_rows:
        day = date.fromisoformat(item["date"])
        close = item.get("close")
        if close is None or not isfinite(close) or (close <= 0 and not futures):
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
        if (old is None or old.adj_close is None or item["adj_close"] is None
                or old.close == 0 or item["close"] == 0):
            continue
        if abs(old.adj_close / old.close - item["adj_close"] / item["close"]) > 1e-6:
            return True
    return False


def update_prices(store: PriceStore, universe: list[UniverseEntry], downloader, *, now: datetime,
                  start: str = "2000-01-01", overlap_days: int = 10, source: str = "yfinance",
                  universe_label: str | None = None, refetch=()) -> dict:
    """downloader(ticker, start_date_iso, end_date_iso_exclusive) -> list of
    {date, close, adj_close, volume}."""
    entries = list(universe)
    known = {entry.ticker for entry in entries}
    refetch_tickers = {_normalize(ticker) for ticker in refetch}
    for ticker in sorted(refetch_tickers - known):
        entries.append(UniverseEntry(ticker))
        known.add(ticker)
    for ticker, name, aliases, role in ALWAYS_INCLUDED:
        if ticker not in known:
            entries.append(UniverseEntry(ticker, name, aliases, role))
            known.add(ticker)
    markets = set()
    for entry in entries:
        try:
            markets.add(market_of(entry.ticker)[0])
        except NotImplementedError:
            pass
    for market in sorted(markets):
        for ticker in BENCHMARKS_BY_MARKET.get(market, ()):
            if ticker not in known:
                entries.append(_benchmark_entry(ticker))
                known.add(ticker)
    # Single futures contracts: the current front month and the next one, so their
    # history is kept while Yahoo still lists them (expired contracts disappear there).
    for entry in list(entries):
        root = root_for_ticker(entry.ticker)
        if root is None:
            continue
        try:
            front = front_contract(root, now.astimezone(calendar_for("NYMEX").tz).date())
        except ValueError:
            continue
        try:
            label = resolve_asset(entry.ticker).name_zh or entry.ticker
        except ValueError:
            label = entry.ticker
        for code in (front, next_contract(front)):
            ticker = contract_ticker(code)
            if ticker not in known:
                entries.append(UniverseEntry(ticker, f"{label} {code}", (code,), "contract"))
                known.add(ticker)
    today = now.astimezone(HK).date()
    run_id = store.start_run(source, universe_label, len(entries), now)
    fetched_at = now.isoformat()
    summaries = []
    written = 0
    for entry in entries:
        summary = TickerSummary(entry.ticker)
        summaries.append(summary)
        try:
            try:
                market, currency = market_of(entry.ticker)
            except NotImplementedError as exc:
                summary.skipped = str(exc)
                continue
            summary.market = market
            calendar = calendar_for(market)
            # "Today" in the exchange's own time zone: that session may still be
            # running (or unfinalized), so it is excluded and picked up next run.
            today = now.astimezone(calendar.tz).date()
            role = ("futures" if root_for_ticker(entry.ticker)
                    else "contract" if is_contract_ticker(entry.ticker) else entry.role)
            store.upsert_instrument(Instrument(entry.ticker, market, currency, entry.name,
                                               entry.aliases, role))
            if entry.ticker in refetch_tickers:
                store.delete_ticker(entry.ticker)
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
                root = root_for_ticker(entry.ticker)
                if root:
                    detected = roll_dates(root, date.fromisoformat(summary.first_date),
                                          date.fromisoformat(summary.last_date))
                    summary.rolls = store.upsert_rolls([
                        FuturesRollRow(entry.ticker, item.roll_date.isoformat(),
                                       item.from_contract, item.to_contract,
                                       item.last_trade_date.isoformat(), FUTURES_METHOD, run_id)
                        for item in detected])
        except (NotImplementedError, ValueError, RuntimeError, OSError) as exc:
            summary.error = str(exc)
    for summary in summaries:
        root = root_for_ticker(summary.ticker)
        if root and not summary.error and not summary.skipped:
            summary.derived_contract_rows = _derive_contract_rows(store, summary.ticker, root,
                                                                  fetched_at, run_id)
            written += summary.derived_contract_rows
    failures = [s for s in summaries if s.error]
    status = "ok" if not failures else ("partial" if len(failures) < len(summaries) else "failed")
    store.finish_run(run_id, datetime.now(HK), written, status,
                     f"{len(failures)} ticker(s) failed" if failures else None)
    return {"run_id": run_id, "status": status, "rows_written": written,
            "db": str(store.path), "tickers": summaries}


def _derive_contract_rows(store: PriceStore, ticker: str, root: str, fetched_at: str, run_id: str) -> int:
    """On days the roll rule says contract C was the front month, the continuous
    series (e.g. BZ=F) is C's price. Store those as C's rows where no direct
    single-contract row exists (direct Yahoo rows always win)."""
    by_contract: dict[str, list] = {}
    for row in store.rows(ticker):
        try:
            code = front_contract(root, date.fromisoformat(row.date))
        except ValueError:
            continue
        by_contract.setdefault(code, []).append(row)
    new_rows = []
    for code, rows in by_contract.items():
        target = contract_ticker(code)
        have = {row.date for row in store.rows(target)}
        missing = [row for row in rows if row.date not in have]
        if not missing:
            continue
        if not have:
            store.upsert_instrument(Instrument(target, "NYMEX", "USD", code, (code,), "contract"))
        new_rows.extend(DailyRow(target, "NYMEX", "USD", row.date, row.close, row.close, row.volume,
                                 f"derived:{ticker} front-month", fetched_at, run_id)
                        for row in missing)
    return store.upsert_rows(new_rows) if new_rows else 0


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
