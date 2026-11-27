"""Update the local daily-price database (see docs/price-store.md).

Example:
    python scripts/update_prices.py --universe news_price_query/data/universe_example_hk.csv
"""

import argparse
from datetime import date, datetime, time
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from news_price_query.assets import Asset, market_of_ticker, resolve_asset  # noqa: E402
from news_price_query.intraday_sync import IntradayUnavailable, MINUTE_INTERVALS, sync_intraday  # noqa: E402
from news_price_query.market_calendars import calendar_for  # noqa: E402
from news_price_query.price_store import PriceStore, default_db_path  # noqa: E402
from news_price_query.price_update import (read_universe, update_prices, yfinance_downloader,
                                           yfinance_hourly_downloader)  # noqa: E402


def sync_minute_universe(store, entries, intervals, first_day, now):
    summaries = []
    for entry in entries:
        try:
            market, currency = market_of_ticker(entry.ticker)
        except NotImplementedError as error:
            summaries.append({"ticker": entry.ticker, "skipped": str(error)})
            continue
        try:
            asset = resolve_asset(entry.ticker)
        except ValueError:
            asset = None
        if asset is None or asset.ticker != entry.ticker:
            asset = Asset(entry.ticker, "", entry.role, (entry.ticker,), market=market, currency=currency)
        first = datetime.combine(first_day, time.min, calendar_for(market).tz)
        for interval in intervals:
            summary = {"ticker": entry.ticker, "interval": interval}
            try:
                written = sync_intraday(store, asset, interval, first, now, now=now)
            except IntradayUnavailable as error:
                summary["error"] = str(error)
            else:
                summary.update(rows_written=written, **store.hourly_coverage(entry.ticker, interval=interval))
            summaries.append(summary)
    return summaries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Incrementally update local prices from yfinance.")
    parser.add_argument("--universe", action="append", required=True,
                        help="Stock-pool CSV (ticker / yfinance代码 / 代码 column). Repeatable.")
    parser.add_argument("--db", default=None, help=f"SQLite path (default {default_db_path()})")
    parser.add_argument("--start", default="2000-01-01", help="First date for tickers not yet stored")
    parser.add_argument("--overlap-days", type=int, default=10,
                        help="Re-fetch this many stored trading days to pick up revisions and dividends")
    parser.add_argument("--refetch", action="append", default=[], metavar="TICKER",
                        help="Delete stored prices and rolls for TICKER, then fetch from --start. Repeatable.")
    parser.add_argument("--json", action="store_true", help="Print the full summary as JSON")
    parser.add_argument("--no-hourly", action="store_true", help="Skip one-hour bar updates")
    parser.add_argument("--hourly-only", action="store_true", help="Skip daily updates")
    parser.add_argument("--minute-interval", action="append", choices=MINUTE_INTERVALS,
                        help="Also sync this resolution for the selected tickers within Yahoo's available history. Repeatable.")
    args = parser.parse_args(argv)
    minute_start = date.fromisoformat(args.start) if args.minute_interval else None

    entries, seen = [], set()
    for path in args.universe:
        for entry in read_universe(path):
            if entry.ticker not in seen:
                seen.add(entry.ticker)
                entries.append(entry)
    store = PriceStore(args.db)
    now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
    result = update_prices(store, entries, yfinance_downloader if not args.hourly_only else None,
                           now=now,
                           start=args.start, overlap_days=args.overlap_days,
                           universe_label=";".join(args.universe), refetch=args.refetch,
                           hourly_downloader=None if args.no_hourly else yfinance_hourly_downloader,
                           update_daily=not args.hourly_only)
    minute_summaries = []
    if args.minute_interval:
        minute_summaries = sync_minute_universe(store, entries, dict.fromkeys(args.minute_interval), minute_start, now)
        result["minute_tickers"] = minute_summaries
    if args.json:
        print(json.dumps({**result, "tickers": [vars(t) for t in result["tickers"]],
                          "hourly_tickers": [vars(t) for t in result.get("hourly_tickers", [])]},
                         ensure_ascii=False, indent=2))
    else:
        print(f"run {result['run_id']} status={result['status']} rows_written={result['rows_written']} db={result['db']}")
        for t in result["tickers"]:
            if t.error:
                print(f"  {t.ticker:9s} ERROR {t.error}")
                continue
            if t.skipped:
                print(f"  {t.ticker:9s} SKIPPED {t.skipped}")
                continue
            print(f"  {t.ticker:9s} stored={t.stored_rows:5d} {t.first_date}..{t.last_date} "
                  f"calendar_days={t.calendar_trading_days:5d} missing={len(t.missing_trading_days):3d} "
                  f"dropped_non_trading={len(t.dropped_non_trading):3d} "
                  f"dropped_unfinalized={len(t.dropped_unfinalized)}"
                  f"{' rolls=' + str(t.rolls) if t.market == 'NYMEX' else ''}"
                  f"{' REBASED' if t.rebased else ''}")
        for t in result.get("hourly_tickers", []):
            if t.error:
                print(f"  {t.ticker:9s} HOURLY ERROR {t.error}")
            else:
                print(f"  {t.ticker:9s} HOURLY rows_written={t.rows_written:5d} stored={t.stored_rows:6d} "
                      f"{t.earliest_bar}..{t.latest_bar}")
        for summary in minute_summaries:
            print(f"  {summary['ticker']:9s} {summary.get('interval', 'MINUTE'):>6s} "
                  f"{summary.get('error') or summary.get('skipped') or summary}")
    return 0 if result["status"] == "ok" and not any(item.get("error") for item in minute_summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
