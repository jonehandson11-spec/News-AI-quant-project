"""Regenerate data/hkex_calendar.csv from XHKG plus manual overrides.

Maintenance tool only: needs `pandas_market_calendars`, which the query code
does not import. Run: python -m news_price_query.build_calendar
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


def _hhmm(value) -> str:
    if value is None or str(value) == "NaT":
        return ""
    return value.tz_convert("Asia/Hong_Kong").strftime("%H:%M")


def build(start: date = START, end: date = END) -> list[dict]:
    import pandas as pd
    import pandas_market_calendars as mcal

    calendar = mcal.get_calendar("XHKG")
    source = f"pandas_market_calendars {mcal.__version__} XHKG"
    schedule = calendar.schedule(start.isoformat(), end.isoformat())
    sessions = {index.date(): row for index, row in schedule.iterrows()}
    names = {
        stamp.date(): name for stamp, name in calendar.regular_holidays.holidays(
            start.isoformat(), end.isoformat(), return_name=True).items()
    }
    rows = []
    day = start
    while day <= end:
        session = sessions.get(day)
        if session is not None:
            close = _hhmm(session["market_close"])
            break_start = _hhmm(session.get("break_start"))
            break_end = _hhmm(session.get("break_end"))
            half = close < "16:00"
            note = ""
            historical = _historical_break(day)
            if historical and not half:
                break_start, break_end = historical
                note = f"lunch break corrected from XHKG 12:00-13:00 ({BREAK_SOURCE})"
            rows.append({
                "date": day.isoformat(), "is_trading": "1", "half_day": "1" if half else "0",
                "open": _hhmm(session["market_open"]),
                "break_start": "" if half else break_start,
                "break_end": "" if half else break_end,
                "close": close, "closure_reason": "", "source_url": source, "note": note,
            })
        else:
            if day.weekday() >= 5:
                reason = "weekend"
            elif day in names:
                reason = f"holiday: {names[day]}"
            else:
                reason = "holiday or ad-hoc closure (XHKG ad-hoc list; reason not named)"
            rows.append({
                "date": day.isoformat(), "is_trading": "0", "half_day": "0", "open": "",
                "break_start": "", "break_end": "", "close": "", "closure_reason": reason,
                "source_url": source, "note": "",
            })
        day += timedelta(days=1)
    with (DATA / "hkex_calendar_overrides.csv").open(encoding="utf-8", newline="") as handle:
        overrides = {row["date"]: row for row in csv.DictReader(handle)}
    for row in rows:
        if row["date"] in overrides:
            row.update(overrides[row["date"]])
    return rows


def main() -> None:
    rows = build()
    with (DATA / "hkex_calendar.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} rows")


if __name__ == "__main__":
    main()
