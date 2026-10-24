"""Rule-based expiry and unadjusted continuous-futures roll dates (stdlib only)."""

from dataclasses import dataclass
from datetime import date, timedelta

from .market_calendars import calendar_for


ROOTS = {
    "CL": "WTI",
    "HO": "NY Harbor ULSD",
    "RB": "RBOB",
    "BZ": "Brent Last Day Financial",
}
MONTH_CODES = "FGHJKMNQUVXZ"
METHOD = (
    "Rule-based last trade dates from CME/ICE contract specifications; "
    "the continuous front month rolls on the next NYMEX trade date after expiry. "
    "Prices are not adjusted across rolls. BZ=F follows the NYMEX Brent Last Day "
    "Financial contract; its final settlement follows ICE Brent, but this calendar "
    "is an approximation of the Yahoo continuous series."
)


@dataclass(frozen=True)
class Roll:
    roll_date: date
    from_contract: str
    to_contract: str
    last_trade_date: date


def root_for_ticker(ticker: str) -> str | None:
    root, separator, suffix = ticker.upper().partition("=")
    return root if separator and suffix == "F" and root in ROOTS else None


def _month(year: int, month: int, offset: int) -> tuple[int, int]:
    value = year * 12 + month - 1 + offset
    return divmod(value, 12)[0], divmod(value, 12)[1] + 1


def _contract(root: str, year: int, month: int) -> str:
    return f"{root}{MONTH_CODES[month - 1]}{year % 100:02d}"


def _previous_business_day(calendar, day: date, *, inclusive: bool = False) -> date:
    if inclusive and calendar.is_trading_day(day):
        return day
    return calendar.shift_trading_days(day, -1)


def last_trade_date(root: str, year: int, month: int) -> date:
    """Contract expiry as a trade date.

    Brent's 2016-03 transition month is an assumption to check against data:
    contract months before it use the older 15-day rule.
    """
    if root not in ROOTS:
        raise ValueError(f"Unknown futures root: {root}")
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid contract month: {month}")
    if root == "BZ":
        london = calendar_for("XLON")
        if (year, month) >= (2016, 3):
            expiry_year, expiry_month = _month(year, month, -1)
            return _previous_business_day(london, date(expiry_year, expiry_month, 1))
        threshold = date(year, month, 1) - timedelta(days=15)
        return _previous_business_day(london, threshold)
    nymex = calendar_for("NYMEX")
    previous_year, previous_month = _month(year, month, -1)
    if root in ("HO", "RB"):
        return _previous_business_day(nymex, date(year, month, 1))
    twenty_fifth = date(previous_year, previous_month, 25)
    reference = _previous_business_day(nymex, twenty_fifth, inclusive=True)
    return nymex.shift_trading_days(reference, -3)


def front_contract(root: str, trade_date: date) -> str:
    """First contract whose last trade date has not passed."""
    if root not in ROOTS:
        raise ValueError(f"Unknown futures root: {root}")
    for offset in range(5):
        year, month = _month(trade_date.year, trade_date.month, offset)
        try:
            expiry = last_trade_date(root, year, month)
        except ValueError:
            continue  # Early contracts can expire before calendar coverage.
        if expiry >= trade_date:
            return _contract(root, year, month)
    raise ValueError(f"No front contract for {root} on {trade_date}")


def roll_dates(root: str, start: date, end: date) -> list[Roll]:
    """Roll on the first NYMEX trade date following each contract expiry."""
    if start > end:
        return []
    if root not in ROOTS:
        raise ValueError(f"Unknown futures root: {root}")
    nymex = calendar_for("NYMEX")
    output = []
    first = start.year * 12 + start.month - 1
    last = end.year * 12 + end.month - 1 + 3
    for value in range(first, last + 1):
        year, month = divmod(value, 12)
        month += 1
        try:
            expiry = last_trade_date(root, year, month)
        except ValueError:
            continue  # The first contract may expire before calendar coverage.
        try:
            roll_day = nymex.shift_trading_days(expiry, 1)
        except ValueError:
            continue
        if start <= roll_day <= end:
            next_year, next_month = _month(year, month, 1)
            output.append(Roll(roll_day, _contract(root, year, month),
                               _contract(root, next_year, next_month), expiry))
    return output
