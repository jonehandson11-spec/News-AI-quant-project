"""Local SQLite daily/hourly price store and daily query provider.

The database file lives outside the repository (default
~/.news_price_query/prices.sqlite3, override with NEWS_PRICE_DB or --db).
scripts/update_prices.py fills it; LocalDbPriceProvider reads it with the same
`bars()` interface as CsvPriceProvider / FutuPriceProvider.
"""

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time
import os
from pathlib import Path
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from .prices import PriceBar


HK = ZoneInfo("Asia/Hong_Kong")
SCHEMA_VERSION = 1


def default_db_path() -> Path:
    env = os.environ.get("NEWS_PRICE_DB")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".news_price_query" / "prices.sqlite3"


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS instruments (
    ticker TEXT PRIMARY KEY,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    name TEXT,
    aliases TEXT,
    role TEXT NOT NULL DEFAULT 'equity'
);
CREATE TABLE IF NOT EXISTS update_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    universe TEXT,
    tickers INTEGER,
    rows_written INTEGER,
    note TEXT
);
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker TEXT NOT NULL,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    adj_close REAL,
    volume REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES update_runs(run_id),
    PRIMARY KEY (ticker, date)
);
CREATE TABLE IF NOT EXISTS hourly_prices (
    ticker TEXT NOT NULL,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    interval TEXT NOT NULL DEFAULT '1h',
    bar_start_utc TEXT NOT NULL,
    exchange_tz TEXT NOT NULL,
    bar_start_local TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    adj_close REAL,
    volume REAL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES update_runs(run_id),
    PRIMARY KEY (ticker, interval, bar_start_utc)
);
CREATE TABLE IF NOT EXISTS futures_rolls (
    ticker TEXT NOT NULL,
    roll_date TEXT NOT NULL,
    from_contract TEXT NOT NULL,
    to_contract TEXT NOT NULL,
    last_trade_date TEXT NOT NULL,
    method TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES update_runs(run_id),
    PRIMARY KEY (ticker, roll_date)
);
"""


@dataclass(frozen=True)
class DailyRow:
    ticker: str
    market: str
    currency: str
    date: str
    close: float
    adj_close: float | None
    volume: float | None
    source: str
    fetched_at: str
    run_id: str


@dataclass(frozen=True)
class HourlyRow:
    ticker: str
    market: str
    currency: str
    interval: str
    bar_start_utc: str
    exchange_tz: str
    bar_start_local: str
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float | None
    source: str
    fetched_at: str
    run_id: str


@dataclass(frozen=True)
class FuturesRollRow:
    ticker: str
    roll_date: str
    from_contract: str
    to_contract: str
    last_trade_date: str
    method: str
    run_id: str


@dataclass(frozen=True)
class Instrument:
    ticker: str
    market: str
    currency: str
    name: str | None
    aliases: tuple[str, ...]
    role: str


class PriceStore:
    def __init__(self, path: str | Path | None = None, *, create: bool = True):
        self.path = Path(path).expanduser() if path else default_db_path()
        if not create and not self.path.exists():
            raise FileNotFoundError(
                f"Local price database not found: {self.path}. Run scripts/update_prices.py first.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    # --- writes -------------------------------------------------------------
    def start_run(self, source: str, universe: str | None, tickers: int, now: datetime) -> str:
        run_id = f"{now.astimezone(HK):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO update_runs (run_id, started_at, status, source, universe, tickers) "
                "VALUES (?, ?, 'running', ?, ?, ?)",
                (run_id, now.isoformat(), source, universe, tickers))
        return run_id

    def finish_run(self, run_id: str, now: datetime, rows_written: int, status: str = "ok",
                   note: str | None = None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE update_runs SET finished_at=?, status=?, rows_written=?, note=? WHERE run_id=?",
                (now.isoformat(), status, rows_written, note, run_id))

    def upsert_instrument(self, item: Instrument) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET "
                "market=excluded.market, currency=excluded.currency, "
                "name=COALESCE(excluded.name, instruments.name), "
                "aliases=COALESCE(excluded.aliases, instruments.aliases), role=excluded.role",
                (item.ticker, item.market, item.currency, item.name,
                 "|".join(item.aliases) if item.aliases else None, item.role))

    def upsert_rows(self, rows: list[DailyRow]) -> int:
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO daily_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker, date) DO UPDATE SET market=excluded.market, "
                "currency=excluded.currency, close=excluded.close, adj_close=excluded.adj_close, "
                "volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at, "
                "run_id=excluded.run_id",
                [(r.ticker, r.market, r.currency, r.date, r.close, r.adj_close, r.volume,
                  r.source, r.fetched_at, r.run_id) for r in rows])
        return len(rows)

    def upsert_hourly_rows(self, rows: list[HourlyRow]) -> int:
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO hourly_prices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker, interval, bar_start_utc) DO UPDATE SET "
                "market=excluded.market, currency=excluded.currency, exchange_tz=excluded.exchange_tz, "
                "bar_start_local=excluded.bar_start_local, open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, adj_close=excluded.adj_close, "
                "volume=excluded.volume, source=excluded.source, fetched_at=excluded.fetched_at, "
                "run_id=excluded.run_id",
                [(r.ticker, r.market, r.currency, r.interval, r.bar_start_utc, r.exchange_tz,
                  r.bar_start_local, r.open, r.high, r.low, r.close, r.adj_close, r.volume,
                  r.source, r.fetched_at, r.run_id) for r in rows])
        return len(rows)

    def delete_ticker(self, ticker: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM daily_prices WHERE ticker=?", (ticker,))
            conn.execute("DELETE FROM futures_rolls WHERE ticker=?", (ticker,))

    def upsert_rolls(self, rows: list[FuturesRollRow]) -> int:
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT INTO futures_rolls VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticker, roll_date) DO UPDATE SET "
                "from_contract=excluded.from_contract, to_contract=excluded.to_contract, "
                "last_trade_date=excluded.last_trade_date, method=excluded.method, run_id=excluded.run_id",
                [(r.ticker, r.roll_date, r.from_contract, r.to_contract, r.last_trade_date,
                  r.method, r.run_id) for r in rows])
        return len(rows)

    # --- reads --------------------------------------------------------------
    def rows(self, ticker: str, start: str | None = None, end: str | None = None) -> list[DailyRow]:
        query = "SELECT * FROM daily_prices WHERE ticker=?"
        params: list = [ticker]
        if start:
            query += " AND date>=?"
            params.append(start)
        if end:
            query += " AND date<=?"
            params.append(end)
        with closing(self._connect()) as conn:
            return [DailyRow(**dict(row)) for row in conn.execute(query + " ORDER BY date", params)]

    def rolls(self, ticker: str, start: str | None = None, end: str | None = None) -> list[FuturesRollRow]:
        query = "SELECT * FROM futures_rolls WHERE ticker=?"
        params: list = [ticker]
        if start:
            query += " AND roll_date>=?"
            params.append(start)
        if end:
            query += " AND roll_date<=?"
            params.append(end)
        with closing(self._connect()) as conn:
            return [FuturesRollRow(**dict(row)) for row in conn.execute(query + " ORDER BY roll_date", params)]

    def last_date(self, ticker: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(date) FROM daily_prices WHERE ticker=?", (ticker,)).fetchone()
        return row[0]

    def hourly_rows(self, ticker: str, start_utc: str | None = None,
                    end_utc: str | None = None) -> list[HourlyRow]:
        query = "SELECT * FROM hourly_prices WHERE ticker=? AND interval='1h'"
        params: list = [ticker]
        if start_utc:
            query += " AND bar_start_utc>=?"
            params.append(start_utc)
        if end_utc:
            query += " AND bar_start_utc<=?"
            params.append(end_utc)
        with closing(self._connect()) as conn:
            return [HourlyRow(**dict(row)) for row in conn.execute(query + " ORDER BY bar_start_utc", params)]

    def last_hourly_bar(self, ticker: str) -> str | None:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT MAX(bar_start_utc) FROM hourly_prices WHERE ticker=? AND interval='1h'",
                               (ticker,)).fetchone()
        return row[0]

    def hourly_coverage(self, ticker: str) -> dict:
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT COUNT(*), MIN(bar_start_utc), MAX(bar_start_utc) "
                               "FROM hourly_prices WHERE ticker=? AND interval='1h'", (ticker,)).fetchone()
        return {"stored_rows": row[0], "earliest_bar": row[1], "latest_bar": row[2]}

    def instruments(self) -> list[Instrument]:
        with closing(self._connect()) as conn:
            return [Instrument(r["ticker"], r["market"], r["currency"], r["name"],
                               tuple(a for a in (r["aliases"] or "").split("|") if a), r["role"])
                    for r in conn.execute("SELECT * FROM instruments ORDER BY ticker")]

    def latest_run(self) -> dict | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM update_runs WHERE status='ok' ORDER BY finished_at DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def counts(self) -> dict[str, tuple[int, str, str]]:
        with closing(self._connect()) as conn:
            return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                "SELECT ticker, COUNT(*), MIN(date), MAX(date) FROM daily_prices GROUP BY ticker")}


class LocalDbPriceProvider:
    """Daily bars from the local store. basis='raw' uses close (split-adjusted
    only, as delivered by yfinance); basis='adj' uses adj_close (dividends and
    splits). Minute bars are not stored: minute requests return no bars."""

    def __init__(self, path: str | Path | None = None, *, basis: str = "raw", store: PriceStore | None = None):
        if basis not in ("raw", "adj"):
            raise ValueError("basis must be raw or adj")
        self.store = store or PriceStore(path, create=False)
        self.basis = basis

    def with_basis(self, basis: str) -> "LocalDbPriceProvider":
        return LocalDbPriceProvider(basis=basis, store=self.store)

    def data_version(self) -> dict | None:
        run = self.store.latest_run()
        if run is None:
            return None
        return {"run_id": run["run_id"], "finished_at": run["finished_at"],
                "source": run["source"], "db": str(self.store.path)}

    def bars(self, ticker: str, start: datetime, end: datetime, interval: str) -> list[PriceBar]:
        if interval != "day":
            return []
        first = start.astimezone(HK).date().isoformat()
        last = end.astimezone(HK).date().isoformat()
        output = []
        for row in self.store.rows(ticker.upper(), first, last):
            value = row.close if self.basis == "raw" else row.adj_close
            if value is None:
                continue
            # Date-only bar; the query layer uses the calendar's close time.
            timestamp = datetime.combine(datetime.fromisoformat(row.date).date(), time(16, 0), HK)
            output.append(PriceBar(
                ticker=row.ticker, interval="day", timestamp=timestamp, close=value,
                source=f"local-db:{row.source} (fetched {row.fetched_at[:10]}, run {row.run_id})",
                basis=self.basis, volume=row.volume,
            ))
        return output
