"""Pick the trading calendar for a market (ISO 10383 MIC)."""

from functools import lru_cache
from pathlib import Path

from .hkex_calendar import DEFAULT_PATH, HkexCalendar


CALENDAR_DIR = Path(__file__).resolve().parent / "data" / "calendars"

# market -> (exchange timezone, calendar CSV, regular close used to mark early closes)
MARKETS = {
    "XHKG": ("Asia/Hong_Kong", DEFAULT_PATH, "16:00"),
    "XNYS": ("America/New_York", CALENDAR_DIR / "XNYS.csv", "16:00"),   # NYSE; also used for Nasdaq listings
    "XTSE": ("America/Toronto", CALENDAR_DIR / "XTSE.csv", "16:00"),
    "XSAU": ("Asia/Riyadh", CALENDAR_DIR / "XSAU.csv", "15:00"),
}


@lru_cache(maxsize=None)
def calendar_for(market: str) -> HkexCalendar:
    try:
        tz, path, _ = MARKETS[market]
    except KeyError:
        raise NotImplementedError(f"No trading calendar for market {market}") from None
    return HkexCalendar(path, tz=tz, market=market)
