"""Regenerate trading-calendar CSVs from pandas_market_calendars plus manual
overrides: data/hkex_calendar.csv (XHKG) and data/calendars/<MIC>.csv.

Maintenance tool only: needs `pandas_market_calendars`, which the query code
does not import. Run: python -m news_price_query.build_calendar [XHKG XNYS XTSE XSAU NYMEX XLON]
"""

import csv
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
FIELDS = ("date", "is_trading", "half_day", "open", "break_start", "break_end",
          "close", "closure_reason", "source_url", "note")
START = date(2000, 1, 1)
END = date(2026, 12, 31)
# XHKG applies today's 12:00-13:00 lunch break to every year. HKEX trading-hour
# extension: phase 1 from 2011-03-07 (lunch 12:00-13:30), phase 2 from 2012-03-05
# (lunch 12:00-13:00). Before 2011-03-07 the lunch break was 12:30-14:30.
HISTORICAL_BREAKS = (
    (date(2011, 3, 7), ("12:30", "14:30")),
    (date(2012, 3, 5), ("12:00", "13:30")),
)
BREAK_SOURCE = "https://www.bbc.co.uk/news/business-17254695"


def _historical_break(day: date):
    for until, value in HISTORICAL_BREAKS:
        if day < until:
            return value
    return None


def _hhmm(value, tz="Asia/Hong_Kong") -> str:
    if value is None or str(value) == "NaT":
        return ""
    return value.tz_convert(tz).strftime("%H:%M")


def build(start: date = START, end: date = END, market: str = "XHKG") -> list[dict]:
    import pandas_market_calendars as mcal
    from .market_calendars import MARKETS

    tz, _, regular_close = MARKETS[market]
    calendar = mcal.get_calendar("CMEGlobex_CL" if market == "NYMEX" else market)
    source = (f"pandas_market_calendars {mcal.__version__} CMEGlobex_CL ∩ XNYS; "
              "close = 14:30 ET settlement" if market == "NYMEX"
              else f"pandas_market_calendars {mcal.__version__} {market}")
    schedule = calendar.schedule(start.isoformat(), end.isoformat())
    sessions = {index.date(): row for index, row in schedule.iterrows()}
    if market == "NYMEX":
        nyse = mcal.get_calendar("XNYS")
        nyse_days = {stamp.date() for stamp in nyse.schedule(start.isoformat(), end.isoformat()).index}
        sessions = {day: row for day, row in sessions.items() if day in nyse_days}
    weekmask = str(getattr(calendar, "weekmask", "Mon Tue Wed Thu Fri"))
    names = {
        stamp.date(): name for stamp, name in calendar.regular_holidays.holidays(
            start.isoformat(), end.isoformat(), return_name=True).items()
    }
    rows = []
    day = start
    while day <= end:
        session = sessions.get(day)
        if session is not None:
            close = "14:30" if market == "NYMEX" else _hhmm(session["market_close"], tz)
            break_start = "" if market == "NYMEX" else _hhmm(session.get("break_start"), tz)
            break_end = "" if market == "NYMEX" else _hhmm(session.get("break_end"), tz)
            half = market != "NYMEX" and close < regular_close
            note = ""
            historical = _historical_break(day) if market == "XHKG" else None
            if historical and not half:
                break_start, break_end = historical
                note = f"lunch break corrected from XHKG 12:00-13:00 ({BREAK_SOURCE})"
            rows.append({
                "date": day.isoformat(), "is_trading": "1", "half_day": "1" if half else "0",
                "open": "09:00" if market == "NYMEX" else _hhmm(session["market_open"], tz),
                "break_start": "" if half else break_start,
                "break_end": "" if half else break_end,
                "close": close, "closure_reason": "", "source_url": source, "note": note,
            })
        else:
            if day.strftime("%a") not in weekmask:
                reason = "weekend"
            elif day in names:
                reason = f"holiday: {names[day]}"
            else:
                reason = f"holiday or ad-hoc closure ({market} ad-hoc list; reason not named)"
            rows.append({
                "date": day.isoformat(), "is_trading": "0", "half_day": "0", "open": "",
                "break_start": "", "break_end": "", "close": "", "closure_reason": reason,
                "source_url": source, "note": "",
            })
        day += timedelta(days=1)
    override_path = (DATA / "hkex_calendar_overrides.csv" if market == "XHKG"
                     else DATA / "calendars" / f"{market}_overrides.csv")
    overrides = {}
    if override_path.exists():
        with override_path.open(encoding="utf-8", newline="") as handle:
            overrides = {row["date"]: row for row in csv.DictReader(handle)}
    for row in rows:
        if row["date"] in overrides:
            row.update(overrides[row["date"]])
            if market == "NYMEX" and not row["source_url"]:
                row["source_url"] = source
    return rows


def main() -> None:
    import sys
    from .market_calendars import MARKETS

    for market in sys.argv[1:] or ["XHKG"]:
        rows = build(market=market)
        path = MARKETS[market][1]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        print(f"{market}: wrote {len(rows)} rows to {path}")


if __name__ == "__main__":
    main()
