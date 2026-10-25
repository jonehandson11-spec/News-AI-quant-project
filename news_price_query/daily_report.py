"""Daily (trading-day) event report: raw and adjusted returns plus excess
returns against benchmarks, with a source on every price."""

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .assets import Asset, resolve_asset
from .futures import (FIXED_METHOD, METHOD as FUTURES_METHOD, contract_expiry, contract_ticker,
                      front_contract, next_contract, roll_dates, root_for_ticker)
from .query import query_event
from .windows import DEFAULT_TRADING_WINDOWS, TradingWindowConfig, window_label_zh


HK = ZoneInfo("Asia/Hong_Kong")
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


CURRENCY_NAMES = {"HKD": "港元", "USD": "美元", "CAD": "加元", "SAR": "里亚尔", "GBP": "英镑"}
UNIT_NAMES = {"USD/bbl": "美元/桶", "USD/gal": "美元/加仑", "USD/t": "美元/吨"}


def price_unit(asset) -> str:
    """Label printed next to every price: 'HKD 港元', '美元/桶', or '点' for an index."""
    if asset.ticker.startswith("^"):
        return "点"
    unit = getattr(asset, "unit", None)
    if unit:
        return UNIT_NAMES.get(unit, unit)
    code = getattr(asset, "currency", None) or "HKD"
    return f"{code} {CURRENCY_NAMES.get(code, '')}".strip()


def _span(label, base_at, base_price, obs_at, obs_price) -> dict:
    """Start/end of a window: before_* runs from the N-days-ago close to the baseline,
    after_* from the baseline to the t+K close. Missing prices stay None."""
    base = (_iso(base_at) if not isinstance(base_at, str) else base_at, base_price)
    obs = (_iso(obs_at) if not isinstance(obs_at, str) else obs_at, obs_price)
    start, end = (obs, base) if label.startswith("before_") else (base, obs)
    return {"start_at": start[0], "start_price": start[1], "end_at": end[0], "end_price": end[1]}


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
            **_span(item.label, raw.baseline.observed_at, raw.baseline.price,
                    item.observed_at, item.price),
            **{f"adj_{key}": value for key, value in _span(
                item.label, adj.baseline.observed_at if adj else None,
                adj.baseline.price if adj else None,
                other.observed_at if other else None, other.price if other else None).items()},
        }
    contract = None
    if root:
        contract, fixed_rows = _fixed_contract(provider, asset, root, raw, published_at, as_of,
                                               config, calendar, min_days_to_expiry)
        for label, window in windows.items():
            fixed = fixed_rows[label]
            window["continuous"] = {key: window[key] for key in (
                "observed_at", "price", "return_pct", "status", "flag", "source",
                "crosses_roll", "rolls", "start_at", "start_price", "end_at", "end_price")}
            fixed_base = contract["baseline"] or {}
            span = _span(label, fixed_base.get("observed_at"), fixed_base.get("price"),
                         fixed["observed_at"], fixed["price"])
            window.update(span)
            window.update({f"adj_{key}": value for key, value in span.items()})
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
        "price_unit": price_unit(asset),
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
                **_span(item.label, b_raw.baseline.observed_at, b_raw.baseline.price,
                        other.observed_at, other.price),
                **{f"adj_{key}": value for key, value in _span(
                    item.label, b_adj.baseline.observed_at if b_adj else None,
                    b_adj.baseline.price if b_adj else None,
                    other_adj.observed_at if other_adj else None,
                    other_adj.price if other_adj else None).items()},
            }
        comparisons[bench_asset.ticker] = {
            "currency": getattr(bench_asset, "currency", None) or "HKD",
            "price_unit": price_unit(bench_asset),
            "baseline": {"observed_at": b_raw.baseline.observed_at.isoformat(),
                         "price": b_raw.baseline.price, "source": b_raw.baseline.source},
            "adj_error": b_adj_error,
            "windows": rows,
        }
    payload["benchmarks"] = comparisons
    return payload


def hourly_report(provider, asset: Asset, published_at: datetime, *, as_of: datetime,
                  benchmarks=None, calendar=None) -> dict:
    """Hourly equivalent of daily_report; adjusted bars and benchmarks use identical targets."""
    from .query import query_event
    from .windows import hourly_label_zh
    from .market_calendars import calendar_for
    if benchmarks is None:
        benchmarks = benchmarks_for(asset)
    calendar = calendar or calendar_for(getattr(asset, "market", "XHKG"))
    raw = query_event(provider, asset, published_at, as_of=as_of, mode="hourly", calendar=calendar)
    # A full calendar trading day with no asset bars but benchmark bars is a suspension.
    suspension_benchmark = next((code for code in benchmarks if code in benchmarks_for(asset)), None)
    if suspension_benchmark:
        from .windows import _hourly_intervals
        marked = []
        for item in raw.windows:
            if item.flag != "missing_data" or item.target_at is None:
                marked.append(item); continue
            day = item.target_at.astimezone(calendar.tz).date()
            try:
                spans = _hourly_intervals(day, calendar)
                own = [bar for start, end in spans for bar in provider.bars(asset.ticker, start, end, "hour")]
                bench = [bar for start, end in spans for bar in provider.bars(suspension_benchmark, start, end, "hour")]
            except (ValueError, RuntimeError):
                own, bench = [None], []
            marked.append(replace(item, status="suspended", flag="suspended") if not own and bench else item)
        raw = replace(raw, windows=tuple(marked))
    adj_provider = _with_basis(provider, "adj")
    try:
        adj = query_event(adj_provider, asset, published_at, as_of=as_of, mode="hourly", calendar=calendar) if adj_provider else None
    except (ValueError, RuntimeError):
        adj = None
    if raw.baseline_flag == "missing_data":
        labels = ["before_6th", "before_3th", "before_1th", "after_1th", "after_3th", "after_6th"]
        return {"ticker": asset.ticker, "asset_role": asset.role, "published_at": published_at.astimezone(HK).isoformat(),
            "published_at_local": published_at.astimezone(calendar.tz).isoformat(), "market": asset.market,
            "exchange_timezone": str(calendar.tz), "currency": asset.currency, "unit": asset.unit,
            "price_unit": price_unit(asset), "asset_type": asset.asset_type, "stage": asset.stage,
            "as_of": as_of.isoformat(), "mode": "hourly", "session": raw.session,
            "baseline": {"observed_at": None, "price": None, "adj_price": None, "source": None,
                         "basis": "raw", "gap_seconds": None, "status": "missing", "flag": "missing_data"},
            "windows": {label: {"label_zh": hourly_label_zh(label), "target_at": None, "observed_at": None,
                "price": None, "adj_price": None, "return_pct": None, "adj_return_pct": None,
                "price_change": None, "status": "missing", "flag": "missing_data", "source": None,
                "start_at": None, "start_price": None, "end_at": None, "end_price": None}
                for label in labels}, "benchmarks": {}, "adj_error": "缺少小时线基准", "method": "缺数据不填零。"}
    adj_map = {w.label: w for w in adj.windows} if adj else {}
    windows = {}
    for item in raw.windows:
        other = adj_map.get(item.label)
        target = item.target_at
        base_at, base_price = raw.baseline.observed_at, raw.baseline.price
        start, end = ((item.observed_at, item.price), (base_at, base_price)) if item.label.startswith("before_") else ((base_at, base_price), (item.observed_at, item.price))
        astart, aend = ((other.observed_at, other.price), (adj.baseline.observed_at, adj.baseline.price)) if other and item.label.startswith("before_") else ((adj.baseline.observed_at, adj.baseline.price), (other.observed_at, other.price)) if other and adj else ((None, None), (None, None))
        windows[item.label] = {
            "label_zh": hourly_label_zh(item.label), "target_at": _iso(target), "observed_at": _iso(item.observed_at),
            "price": item.price, "adj_price": other.price if other else None,
            "return_pct": item.return_pct, "adj_return_pct": other.return_pct if other else None,
            "price_change": ((base_price - item.price) if item.label.startswith("before_") else (item.price - base_price))
                if item.price is not None and base_price is not None else None,
            "status": item.status, "flag": item.flag or (other.flag if other else None),
            "delay_seconds": item.delay_seconds, "source": "yfinance 1 小时线", "source_detail": item.source, "basis": "raw",
            "start_at": _iso(start[0]), "start_price": start[1], "end_at": _iso(end[0]), "end_price": end[1],
            "adj_start_at": _iso(astart[0]), "adj_start_price": astart[1], "adj_end_at": _iso(aend[0]), "adj_end_price": aend[1],
        }
    comparisons = {}
    for code in dict.fromkeys(benchmarks):
        try:
            bench = resolve_asset(code)
            if bench.market != asset.market: continue
            result = query_event(provider, bench, published_at, as_of=as_of, mode="hourly", calendar=calendar)
            try:
                bench_adj_provider = _with_basis(provider, "adj")
                result_adj = query_event(bench_adj_provider, bench, published_at,
                                         as_of=as_of, mode="hourly", calendar=calendar) if bench_adj_provider else None
            except (ValueError, RuntimeError, TypeError):
                result_adj = None
            own_by_label = {w.label: w for w in raw.windows}
            own_adj_by_label = {w.label: w for w in adj.windows} if adj else {}
            bench_adj_by_label = {w.label: w for w in result_adj.windows} if result_adj else {}
            rows = {}
            for w in result.windows:
                own = own_by_label[w.label]
                w_adj = bench_adj_by_label.get(w.label)
                own_adj = own_adj_by_label.get(w.label)
                aligned = (w.return_pct is not None and own.return_pct is not None and
                           result.baseline.observed_at == raw.baseline.observed_at and
                           w.observed_at == own.observed_at)
                aligned_adj = (w_adj is not None and own_adj is not None and
                               result_adj.baseline.observed_at == adj.baseline.observed_at and
                               w_adj.observed_at == own_adj.observed_at)
                bstart, bend = ((w.observed_at, w.price), (result.baseline.observed_at, result.baseline.price)) if w.label.startswith("before_") else ((result.baseline.observed_at, result.baseline.price), (w.observed_at, w.price))
                rows[w.label] = {"observed_at": _iso(w.observed_at), "price": w.price,
                    "return_pct": w.return_pct, "adj_return_pct": w_adj.return_pct if w_adj else None,
                    "status": w.status, "excess_return_pct": own.return_pct - w.return_pct if aligned else None,
                    "adj_excess_return_pct": own_adj.return_pct - w_adj.return_pct if aligned_adj else None,
                    "start_at": _iso(bstart[0]), "start_price": bstart[1],
                    "end_at": _iso(bend[0]), "end_price": bend[1]}
            comparisons[bench.ticker] = {"currency": bench.currency, "price_unit": price_unit(bench),
                "baseline": {"observed_at": result.baseline.observed_at.isoformat(), "price": result.baseline.price},
                "adj_error": None if result_adj else "复权小时线不可用", "windows": rows}
        except (ValueError, RuntimeError):
            comparisons[code] = {"error": "对照标的小时线缺数据", "windows": {}}
    payload = {"ticker": asset.ticker, "asset_role": asset.role, "published_at": published_at.astimezone(HK).isoformat(),
        "published_at_local": published_at.astimezone(calendar.tz).isoformat(), "market": asset.market,
        "exchange_timezone": str(calendar.tz), "currency": asset.currency, "unit": asset.unit,
        "price_unit": price_unit(asset), "asset_type": asset.asset_type, "stage": asset.stage,
        "as_of": as_of.isoformat(), "mode": "hourly", "session": raw.session,
        "baseline": {"observed_at": raw.baseline.observed_at.isoformat(), "price": raw.baseline.price,
            "adj_price": adj.baseline.price if adj else None, "source": "yfinance 1 小时线", "source_detail": raw.baseline.source,
            "basis": "raw", "gap_seconds": raw.baseline_gap_seconds, "status": raw.baseline_status,
            "flag": raw.baseline_flag}, "windows": windows, "benchmarks": comparisons,
        "adj_error": None if adj else "复权小时线不可用", "method": "按交易时段累计小时；休市时间跳过；缺数据不填零。",
        "data_version": provider.data_version() if hasattr(provider, "data_version") else None}
    root = root_for_ticker(asset.ticker) if asset.asset_type == "futures" else None
    if root:
        base_day = raw.baseline.observed_at.date()
        front = front_contract(root, base_day)
        remaining = _days_to_expiry(calendar_for("NYMEX"), base_day, contract_expiry(front))
        code = next_contract(front) if remaining < DEFAULT_MIN_DAYS_TO_EXPIRY else front
        fixed_asset = replace(asset, ticker=contract_ticker(code), aliases=(code,), data_ticker=contract_ticker(code))
        try:
            fixed = query_event(provider, fixed_asset, published_at, as_of=as_of, mode="hourly", calendar=calendar)
        except (ValueError, RuntimeError):
            fixed = None
        payload["contract"] = {"contract": code, "ticker": contract_ticker(code),
            "front_contract": front, "switched": code != front, "last_trade_date": contract_expiry(code).isoformat(),
            "note": f"临近到期，改用 {code}" if code != front else None}
        if fixed and fixed.baseline_flag != "missing_data":
            payload["continuous_baseline"] = payload["baseline"]
            payload["baseline"] = {"observed_at": fixed.baseline.observed_at.isoformat(), "price": fixed.baseline.price,
                "adj_price": fixed.baseline.price, "source": "yfinance 1 小时线", "source_detail": fixed.baseline.source, "basis": "raw",
                "gap_seconds": fixed.baseline_gap_seconds, "status": fixed.baseline_status, "flag": fixed.baseline_flag}
            fixed_map = {w.label: w for w in fixed.windows}
            for label, row in payload["windows"].items():
                row["continuous"] = {key: row.get(key) for key in ("observed_at", "price", "return_pct", "status", "flag", "source", "start_at", "start_price", "end_at", "end_price")}
                w = fixed_map[label]
                row.update({"observed_at": _iso(w.observed_at), "price": w.price, "adj_price": w.price,
                    "return_pct": w.return_pct, "adj_return_pct": w.return_pct, "status": w.status,
                    "flag": w.flag, "source": "yfinance 1 小时线", "source_detail": w.source, "contract": code})
                start, end = ((w.observed_at, w.price), (fixed.baseline.observed_at, fixed.baseline.price)) if label.startswith("before_") else ((fixed.baseline.observed_at, fixed.baseline.price), (w.observed_at, w.price))
                row.update({"start_at": _iso(start[0]), "start_price": start[1], "end_at": _iso(end[0]), "end_price": end[1]})
        else:
            payload["contract"]["error"] = f"{code} 小时线缺数据"
            payload["contract"]["fallback"] = "continuous"
            payload["contract"]["fallback_note"] = "固定合约小时线不可用，结果保留连续合约"
    return payload
