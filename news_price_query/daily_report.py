"""Daily (trading-day) event report: raw and adjusted returns plus excess
returns against benchmarks, with a source on every price."""

from dataclasses import replace
from datetime import datetime

from .assets import Asset, resolve_asset
from .futures import (FIXED_METHOD, METHOD as FUTURES_METHOD, contract_expiry, contract_ticker,
                      front_contract, next_contract, roll_dates, root_for_ticker)
from .query import query_event
from .windows import DEFAULT_TRADING_WINDOWS, TradingWindowConfig, window_label_zh


DEFAULT_BENCHMARKS = ("2800.HK", "^HSI")
BENCHMARKS_BY_MARKET = {
    "XHKG": ("2800.HK", "^HSI"),
    "XNYS": ("SPY", "XLE"),
    "XTSE": ("XIU.TO", "^GSPTSE"),
    # Yahoo's ^TASI.SR has only the latest day (no history) and Tadawul ETFs are
    # not on the free feed, so Saudi results have no benchmark for now.
    "XSAU": (),
    "NYMEX": (),
}


def benchmarks_for(asset: Asset) -> tuple[str, ...]:
    return BENCHMARKS_BY_MARKET.get(getattr(asset, "market", "XHKG"), ())

METHOD = (
    "t0 = news day if published before that day's close, else next trading day on the asset's exchange. "
    "Baseline = close of the trading day before t0 (last close before publication). "
    "before_Ntd: return from the close N trading days before the baseline day to the baseline. "
    "after_tK: return from the baseline to the close K trading days after t0. "
    "raw = close as delivered (split-adjusted only); adj = adjusted close (dividends and splits). "
    "excess = stock return - benchmark return on the same dates (simple difference, not a market model). "
    "Index benchmarks (^HSI, ^GSPTSE) are price indexes, so their adj return equals raw. "
    "Returns are in the asset's local currency; benchmarks are from the same market. "
    "Missing values are never zero."
)


def _iso(value):
    return value.isoformat() if value is not None else None


def _with_basis(provider, basis):
    if hasattr(provider, "with_basis"):
        return provider.with_basis(basis)
    return provider if basis == "raw" else None


def _run(provider, asset, published_at, as_of, config, calendar):
    return query_event(provider, asset, published_at, as_of=as_of, mode="daily",
                       config=config, calendar=calendar)


def _pair(provider, asset, published_at, as_of, config, calendar):
    raw = _run(_with_basis(provider, "raw"), asset, published_at, as_of, config, calendar)
    adj_provider = _with_basis(provider, "adj")
    adj, adj_error = None, None
    if adj_provider is None:
        adj_error = "This price source has no adjusted prices"
    else:
        try:
            adj = _run(adj_provider, asset, published_at, as_of, config, calendar)
        except ValueError as exc:
            adj_error = str(exc)
    return raw, adj, adj_error


def _aligned(own, other, own_base, other_base):
    return (
        own.return_pct is not None and other is not None and other.return_pct is not None
        and own.observed_at is not None and other.observed_at is not None
        and own.observed_at.date() == other.observed_at.date()
        and own_base.observed_at.date() == other_base.observed_at.date()
    )


def _window_rolls(root, label, baseline, observation):
    if root is None or observation is None or baseline is None:
        return []
    first = observation.date() if label.startswith("before_") else baseline.date()
    last = baseline.date() if label.startswith("before_") else observation.date()
    if first >= last:
        return []
    return [{"roll_date": roll.roll_date.isoformat(),
             "from_contract": roll.from_contract, "to_contract": roll.to_contract}
            for roll in roll_dates(root, first, last) if first < roll.roll_date <= last]


# Fixed-contract choice: if fewer than this many NYMEX trading days remain after the
# baseline day up to and including the front contract's last trade date, use the next
# contract instead ("临近到期，改用 …"). Configurable per call (min_days_to_expiry).
DEFAULT_MIN_DAYS_TO_EXPIRY = 5


def _days_to_expiry(calendar, base_day, expiry) -> int:
    """Trading days d with base_day < d <= expiry (0 if the baseline day is the last trade date)."""
    count, day = 0, base_day
    while True:
        day = calendar.shift_trading_days(day, 1)
        if day > expiry:
            return count
        count += 1


def _fixed_contract(provider, asset, root, continuous, published_at, as_of, config, calendar,
                    min_days_to_expiry=DEFAULT_MIN_DAYS_TO_EXPIRY):
    """Re-run the windows on the single contract that was the front month on the
    continuous baseline day (or the next one if the front is about to expire).
    Windows after the chosen contract's last trade date are 'expired'."""
    from .market_calendars import calendar_for

    base_day = continuous.baseline.observed_at.date()
    front = front_contract(root, base_day)
    remaining = _days_to_expiry(calendar or calendar_for(asset.market), base_day, contract_expiry(front))
    code, switched = front, remaining < min_days_to_expiry
    if switched:
        code = next_contract(front)
    expiry = contract_expiry(code)
    info = {"contract": code, "ticker": contract_ticker(code), "last_trade_date": expiry.isoformat(),
            "front_contract": front, "front_last_trade_date": contract_expiry(front).isoformat(),
            "front_days_to_expiry": remaining, "min_days_to_expiry": min_days_to_expiry,
            "switched": switched,
            "note": (f"临近到期，改用 {code}（{front} 在基准日后仅剩 {remaining} 个交易日，"
                     f"阈值 {min_days_to_expiry}）") if switched else None,
            "error": None, "baseline": None}
    contract_asset = replace(asset, ticker=contract_ticker(code), aliases=(code,),
                             data_ticker=contract_ticker(code))
    try:
        fixed = _run(_with_basis(provider, "raw"), contract_asset, published_at, as_of, config, calendar)
    except ValueError as exc:
        fixed = None
        info["error"] = f"{code}: {exc}"
    if fixed is not None:
        info["baseline"] = {"observed_at": fixed.baseline.observed_at.isoformat(),
                            "price": fixed.baseline.price, "source": fixed.baseline.source,
                            "status": fixed.baseline_status, "flag": fixed.baseline_flag}
    fixed_windows = {item.label: item for item in fixed.windows} if fixed else {}
    rows = {}
    for item in continuous.windows:
        target_day = item.target_at.date() if item.target_at is not None else None
        own = fixed_windows.get(item.label)
        if target_day is not None and target_day > expiry:
            rows[item.label] = {"target_at": _iso(item.target_at), "observed_at": None, "price": None,
                                "return_pct": None, "status": "expired", "flag": None, "source": None}
        elif own is None:
            rows[item.label] = {"target_at": _iso(item.target_at), "observed_at": None, "price": None,
                                "return_pct": None, "status": "missing", "flag": "missing_data",
                                "source": None}
        else:
            rows[item.label] = {"target_at": _iso(own.target_at), "observed_at": _iso(own.observed_at),
                                "price": own.price, "return_pct": own.return_pct, "status": own.status,
                                "flag": own.flag, "source": own.source}
    return info, rows


def daily_report(provider, asset: Asset, published_at: datetime, *, as_of: datetime,
                 benchmarks=None,
                 config: TradingWindowConfig = DEFAULT_TRADING_WINDOWS, calendar=None,
                 min_days_to_expiry: int = DEFAULT_MIN_DAYS_TO_EXPIRY) -> dict:
    """benchmarks=None uses the asset's market defaults (BENCHMARKS_BY_MARKET);
    extra benchmarks from another market are reported as skipped."""
    if benchmarks is None:
        benchmarks = benchmarks_for(asset)
    raw, adj, adj_error = _pair(provider, asset, published_at, as_of, config, calendar)
    market = getattr(asset, "market", "XHKG")
    from .market_calendars import calendar_for

    local_tz = (calendar or calendar_for(market)).tz
    adj_windows = {item.label: item for item in adj.windows} if adj else {}
    root = root_for_ticker(asset.ticker) if asset.asset_type == "futures" else None
    windows = {}
    for item in raw.windows:
        other = adj_windows.get(item.label)
        change = None
        if item.price is not None:
            change = (raw.baseline.price - item.price if item.label.startswith("before_")
                      else item.price - raw.baseline.price)
        rolls = _window_rolls(root, item.label, raw.baseline.observed_at, item.observed_at)
        adj_rolls = _window_rolls(root, item.label, adj.baseline.observed_at if adj else None,
                                  other.observed_at if other else None)
        windows[item.label] = {
            "label_zh": window_label_zh(item.label),
            "target_at": _iso(item.target_at),
            "observed_at": _iso(item.observed_at),
            "price": item.price,
            "adj_price": other.price if other else None,
            "price_change": change,
            "return_pct": item.return_pct,
            "adj_return_pct": other.return_pct if other else None,
            "status": item.status,
            "flag": item.flag or (other.flag if other else None),
            "delay_seconds": item.delay_seconds,
            "source": item.source,
            "basis": "raw",
            "crosses_roll": bool(rolls),
            "rolls": rolls,
            "adj_crosses_roll": bool(adj_rolls),
            "adj_rolls": adj_rolls,
        }
    contract = None
    if root:
        contract, fixed_rows = _fixed_contract(provider, asset, root, raw, published_at, as_of,
                                               config, calendar, min_days_to_expiry)
        for label, window in windows.items():
            fixed = fixed_rows[label]
            window["continuous"] = {key: window[key] for key in (
                "observed_at", "price", "return_pct", "status", "flag", "source",
                "crosses_roll", "rolls")}
            window.update({
                "observed_at": fixed["observed_at"], "price": fixed["price"],
                "adj_price": fixed["price"], "return_pct": fixed["return_pct"],
                "adj_return_pct": fixed["return_pct"], "status": fixed["status"],
                "flag": fixed["flag"], "source": fixed["source"], "price_change": None,
                "contract": contract["contract"],
                # A single contract never rolls; roll marks live on the continuous reference.
                "crosses_roll": False, "rolls": [], "adj_crosses_roll": False, "adj_rolls": [],
            })
            if fixed["price"] is not None and contract["baseline"]:
                base = contract["baseline"]["price"]
                window["price_change"] = (base - fixed["price"] if label.startswith("before_")
                                          else fixed["price"] - base)
    payload = {
        "ticker": asset.ticker,
        "asset_role": asset.role,
        "published_at": raw.published_at.isoformat(),
        "published_at_local": published_at.astimezone(local_tz).isoformat(),
        "market": market,
        "exchange_timezone": str(local_tz),
        "currency": getattr(asset, "currency", "HKD"),
        "unit": asset.unit,
        "asset_type": asset.asset_type,
        "stage": getattr(asset, "stage", None),
        "fx": None,  # interface only: see fx.py; returns are in local currency
        "as_of": as_of.isoformat(),
        "mode": "daily",
        "session": raw.session,
        "t0": raw.t0.isoformat(),
        "baseline": {
            "observed_at": raw.baseline.observed_at.isoformat(),
            "price": raw.baseline.price,
            "adj_price": adj.baseline.price if adj else None,
            "source": raw.baseline.source,
            "basis": "raw",
            "gap_seconds": raw.baseline_gap_seconds,
            "status": raw.baseline_status,
            "flag": raw.baseline_flag,
        },
        "windows": windows,
        "contract": contract,
        "continuous_baseline": None,
        "adj_error": adj_error,
        "method": METHOD,
        "contract_method": (FIXED_METHOD + " " + FUTURES_METHOD) if root else None,
        "data_version": provider.data_version() if hasattr(provider, "data_version") else None,
    }
    if contract is not None:
        payload["continuous_baseline"] = payload["baseline"]
        fixed_base = contract["baseline"] or {}
        payload["baseline"] = {
            "observed_at": fixed_base.get("observed_at"), "price": fixed_base.get("price"),
            "adj_price": fixed_base.get("price"), "source": fixed_base.get("source"), "basis": "raw",
            "gap_seconds": None, "status": fixed_base.get("status", "missing"),
            "flag": fixed_base.get("flag"),
        }
    comparisons = {}
    for code in dict.fromkeys(benchmarks):
        bench_asset = resolve_asset(code)
        if bench_asset.ticker == asset.ticker:
            continue
        if getattr(bench_asset, "market", "XHKG") != market:
            continue  # cross-market comparisons need FX and calendar alignment: not done
        try:
            b_raw, b_adj, b_adj_error = _pair(provider, bench_asset, published_at, as_of, config, calendar)
        except (ValueError, RuntimeError) as exc:
            comparisons[bench_asset.ticker] = {"error": str(exc)}
            continue
        b_adj_windows = {item.label: item for item in b_adj.windows} if b_adj else {}
        rows = {}
        for item in raw.windows:
            other = next(w for w in b_raw.windows if w.label == item.label)
            other_adj = b_adj_windows.get(item.label)
            own_adj = adj_windows.get(item.label)
            excess = (item.return_pct - other.return_pct
                      if _aligned(item, other, raw.baseline, b_raw.baseline) else None)
            adj_excess = None
            if own_adj is not None and other_adj is not None and adj and b_adj \
                    and _aligned(own_adj, other_adj, adj.baseline, b_adj.baseline):
                adj_excess = own_adj.return_pct - other_adj.return_pct
            rows[item.label] = {
                "observed_at": _iso(other.observed_at),
                "price": other.price,
                "return_pct": other.return_pct,
                "adj_return_pct": other_adj.return_pct if other_adj else None,
                "status": other.status,
                "flag": other.flag or (other_adj.flag if other_adj else None),
                "source": other.source,
                "excess_return_pct": excess,
                "adj_excess_return_pct": adj_excess,
            }
        comparisons[bench_asset.ticker] = {
            "baseline": {"observed_at": b_raw.baseline.observed_at.isoformat(),
                         "price": b_raw.baseline.price, "source": b_raw.baseline.source},
            "adj_error": b_adj_error,
            "windows": rows,
        }
    payload["benchmarks"] = comparisons
    return payload
