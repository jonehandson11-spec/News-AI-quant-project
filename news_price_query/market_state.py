"""Descriptive daily market state available before a news publication."""

from datetime import date, datetime, timedelta
from math import isfinite
from statistics import mean, stdev

from .market_calendars import calendar_for


PROFILE_ID = "pq-market-state-daily-v1"
BRENT_WTI_PROFILE_ID = "pq-market-state-daily-brent-wti-v1"
PETROLEUM_BASKET_PROFILE_ID = "pq-market-state-daily-petroleum-basket-v1"
LABEL_ZH = "发布前行情描述，非 GVS 校准因子"
_BENCHMARKS = {"XHKG": "2800.HK", "XNYS": "SPY", "XTSE": "XIU.TO"}
_FUTURES_BENCHMARKS = {"BZ=F": "CL=F"}
_PETROLEUM_COMPONENTS = (("RB=F", "RBOB Gasoline", 0.5), ("HO=F", "NY Harbor ULSD", 0.5))
_PETROLEUM_NAME = "Petroleum Basket"
_NOT_APPLICABLE = "NOT_APPLICABLE"


def benchmark_for_market_state(asset):
    if asset.asset_type == "futures":
        return _FUTURES_BENCHMARKS.get(asset.ticker)
    return _BENCHMARKS.get(asset.market)


def reference_tickers_for_market_state(asset):
    benchmark = benchmark_for_market_state(asset)
    if (asset.ticker, benchmark) == ("BZ=F", "CL=F"):
        return (benchmark, *(ticker for ticker, _, _ in _PETROLEUM_COMPONENTS))
    return (benchmark,) if benchmark else ()


def _field(value=None, reason=None, **details):
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is not None and not isfinite(value):
            value = None
            reason = reason or "REFERENCE_MISSING"
    def clean(item):
        if isinstance(item, float) and not isfinite(item):
            return None
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(child) for child in item]
        return item
    cleaned = clean(details)
    if cleaned != details and reason is None:
        reason = "REFERENCE_MISSING"
    return {"value": value, "missing_reason": reason, **cleaned}


def _missing(reason, **details):
    return _field(None, reason, **details)


def _liquidity_missing():
    return _missing("ESTIMATOR_NOT_READY",
                    note="免费数据无买卖价差/盘口；按 GVS 定义不得用成交量代理",
                    note_en="Bid/ask quotes or order-book data are required to estimate liquidity stress.")


def _price(row, raw):
    value = row.close if raw else row.adj_close
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) and value > 0 else None


def _returns(prices):
    return [prices[i][1] / prices[i - 1][1] - 1 for i in range(1, len(prices))]


def _consecutive_closes(prices, cutoff, calendar, limit):
    """Stop at the first missing or invalid trading session."""
    by_date = dict(prices)
    closes = []
    while cutoff >= calendar.first and len(closes) < limit:
        if calendar.is_trading_day(cutoff):
            day = cutoff.isoformat()
            if day not in by_date:
                break
            closes.append((day, by_date[day]))
        cutoff -= timedelta(days=1)
    return closes[::-1]


def _benchmark_beta(own, other, benchmark, *, redundant=False):
    by_date = dict(other)
    common = [(day, price, by_date[day]) for day, price in own if day in by_date][-64:]
    details = {"benchmark": benchmark, "n_pairs": max(0, len(common) - 1)}
    if redundant:
        details["redundant"] = True
    if len(common) < 64:
        return _missing("INSUFFICIENT_HISTORY", **details,
                        first_pair_date=common[0][0] if common else None,
                        last_pair_date=common[-1][0] if common else None)
    xs = [later[2] / earlier[2] - 1 for earlier, later in zip(common, common[1:])]
    ys = [later[1] / earlier[1] - 1 for earlier, later in zip(common, common[1:])]
    details.update(first_pair_date=common[1][0], last_pair_date=common[-1][0], covariance_ddof=1)
    if any(not isfinite(value) for value in (*xs, *ys)):
        return _missing("REFERENCE_MISSING", **details)
    variance = stdev(xs) ** 2
    if variance == 0:
        return _missing("REFERENCE_MISSING", **details)
    mean_x, mean_y = mean(xs), mean(ys)
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / (len(xs) - 1)
    return _field(covariance / variance, **details)


def _petroleum_levels(series, cutoff, calendar):
    """Compound equal-weight, rebalanced daily returns over shared complete sessions."""
    constituents = [_consecutive_closes(series(ticker, True), cutoff, calendar, 274)
                    for ticker, _, _ in _PETROLEUM_COMPONENTS]
    length = min(map(len, constituents))
    if not length:
        return []
    aligned = [closes[-length:] for closes in constituents]
    levels = [(aligned[0][0][0], 1.0)]
    for i in range(1, length):
        day = aligned[0][i][0]
        gross_return = sum(weight * (closes[i][1] / closes[i - 1][1])
                           for (_, _, weight), closes in zip(_PETROLEUM_COMPONENTS, aligned))
        level = levels[-1][1] * gross_return
        if not isfinite(level) or level <= 0:
            levels = [(day, 1.0)]
        else:
            levels.append((day, level))
    return levels


def _empty_petroleum_fields(reason):
    return {"benchmark_beta": _missing(reason, benchmark=_PETROLEUM_NAME),
            "benchmark_momentum_z": _missing(reason, benchmark=_PETROLEUM_NAME,
                                            window_days=21, reference_days=252)}


def _momentum(prices, *, cutoff, benchmark=None, redundant=False):
    extra = {"window_days": 21, "reference_days": 252}
    if benchmark:
        extra["benchmark"] = benchmark
    if redundant:
        extra["redundant"] = True
    if not prices or prices[-1][0] != cutoff:
        return _missing("NOT_OBSERVED", **extra)
    n = len(prices)
    if n < 274:
        return _missing("INSUFFICIENT_HISTORY", **extra)
    current = prices[n - 1][1] / prices[n - 22][1] - 1
    ref = [prices[n - 1 - j][1] / prices[n - 22 - j][1] - 1 for j in range(1, 253)]
    if not isfinite(current) or any(not isfinite(value) for value in ref):
        return _missing("REFERENCE_MISSING", current_return=current, **extra)
    ref_mean = mean(ref)
    ref_std = stdev(ref)
    details = {**extra, "current_return": current, "reference_mean": ref_mean,
               "reference_std": ref_std, "window_start": prices[n - 22][0],
               "window_end": prices[-1][0], "reference_first_end_date": prices[n - 253][0],
               "reference_last_end_date": prices[n - 2][0]}
    if ref_std == 0:
        return _missing("REFERENCE_MISSING", **details)
    return _field((current - ref_mean) / ref_std, **details)


def _blank_fields(reason):
    return {
        "momentum_z": _missing(reason, window_days=21, reference_days=252),
        "realized_volatility": _missing(reason, n_returns=21,
                                         unit="daily decimal return stdev, not annualized"),
        "relative_trading_activity": _missing(reason, window_mean_volume=None,
                                               reference_mean_volume=None,
                                               volume_unit="shares/contracts"),
        "liquidity_stress_z": _liquidity_missing(),
        "benchmark_beta": _missing(reason),
        "benchmark_momentum_z": _missing(reason, window_days=21, reference_days=252),
        "data_coverage": _missing(reason),
    }


def _coverage(calendar, cutoff, observed_dates):
    expected = []
    cursor = cutoff
    while len(expected) < 274:
        if calendar.is_trading_day(cursor):
            expected.append(cursor)
        cursor -= timedelta(days=1)
    expected.reverse()
    observed = {d for d in observed_dates if d in {day.isoformat() for day in expected}}
    missing = [day.isoformat() for day in expected if day.isoformat() not in observed]
    return _field((274-len(missing))/274, expected_days=274, observed_days=274-len(missing),
        first_expected_date=expected[0].isoformat(), last_expected_date=expected[-1].isoformat(),
        missing_dates=missing[:20])


def market_state(store, asset, published_at: datetime, *, as_of: datetime, calendar=None) -> dict:
    """Compute daily pre-publication metrics from a PriceStore without future rows."""
    if published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a timezone")
    calendar = calendar or calendar_for(asset.market)
    benchmark = benchmark_for_market_state(asset)
    brent_wti = (asset.ticker, benchmark) == ("BZ=F", "CL=F")
    raw = asset.ticker.startswith("^") or asset.asset_type == "futures"
    output = {"profile_id": BRENT_WTI_PROFILE_ID if brent_wti else PROFILE_ID, "label_zh": LABEL_ZH,
              "cutoff_date": None, "price_basis": "raw" if raw else "adj",
              "benchmark": benchmark,
              "method": "截止最近已收盘交易日；股票/ETF 用复权收盘，指数/期货用原始收盘，收益按有效日线连续计算。",
              "fields": _blank_fields("OUT_OF_COVERAGE")}
    if brent_wti:
        output["benchmark_name_en"] = "WTI Crude Oil"
        output["industry_benchmark"] = {
            "name_en": _PETROLEUM_NAME,
            "profile_id": PETROLEUM_BASKET_PROFILE_ID,
            "components": [{"ticker": ticker, "name_en": name, "weight": weight}
                           for ticker, name, weight in _PETROLEUM_COMPONENTS],
            "fields": _empty_petroleum_fields("OUT_OF_COVERAGE"),
        }
    deadline = min(published_at, as_of).astimezone(calendar.tz)
    cursor = deadline.date()
    if cursor > calendar.last:
        cursor = calendar.last
    try:
        while True:
            day = calendar.day(cursor)
            if day.is_trading and day.close_at() <= deadline:
                cutoff = cursor
                break
            cursor -= timedelta(days=1)
    except (ValueError, RuntimeError):
        return output
    output["cutoff_date"] = cutoff.isoformat()
    if brent_wti:
        output["industry_benchmark"]["fields"] = _empty_petroleum_fields("NOT_OBSERVED")
    own_rows = store.rows(asset.ticker, end=cutoff.isoformat())
    if not own_rows:
        fields = _blank_fields("NOT_OBSERVED")
        if benchmark is None:
            fields["benchmark_beta"] = _missing(_NOT_APPLICABLE)
            fields["benchmark_momentum_z"] = _missing(_NOT_APPLICABLE,
                window_days=21, reference_days=252)
        try:
            fields["data_coverage"] = _coverage(calendar, cutoff, set())
        except (ValueError, RuntimeError):
            fields["data_coverage"] = _missing("OUT_OF_COVERAGE", expected_days=None,
                observed_days=None, first_expected_date=None, last_expected_date=None, missing_dates=[])
        output["fields"] = fields
        return output

    def series(ticker, raw_basis):
        result = []
        for row in store.rows(ticker, end=cutoff.isoformat()):
            price = _price(row, raw_basis)
            if price is not None:
                result.append((row.date, price))
        return result

    own = series(asset.ticker, raw)
    if own and own[-1][0] != cutoff.isoformat():
        own_on_cutoff = False
    else:
        own_on_cutoff = bool(own)
    fields = {}
    if not own_on_cutoff:
        fields["momentum_z"] = _missing("NOT_OBSERVED", window_days=21, reference_days=252)
        fields["realized_volatility"] = _missing("NOT_OBSERVED", n_returns=21,
            unit="daily decimal return stdev, not annualized")
    else:
        mom_input = list(own)
        fields["momentum_z"] = _momentum(mom_input, cutoff=cutoff.isoformat())
        if len(own) < 22:
            fields["realized_volatility"] = _missing("INSUFFICIENT_HISTORY", n_returns=max(0, len(own)-1),
                unit="daily decimal return stdev, not annualized")
        else:
            returns = _returns(own[-22:])
            if any(not isfinite(value) for value in returns):
                fields["realized_volatility"] = _missing("REFERENCE_MISSING", n_returns=len(returns),
                    first_return_date=own[-21][0], last_return_date=own[-1][0],
                    unit="daily decimal return stdev, not annualized")
            else:
                fields["realized_volatility"] = _field(stdev(returns), n_returns=len(returns),
                    first_return_date=own[-21][0], last_return_date=own[-1][0],
                    unit="daily decimal return stdev, not annualized")

    volume_rows = [row for row in own_rows if row.volume is not None and isfinite(float(row.volume))]
    if asset.ticker.startswith("^"):
        fields["relative_trading_activity"] = _missing(_NOT_APPLICABLE,
            volume_unit="shares/contracts")
    elif len(volume_rows) < 273:
        fields["relative_trading_activity"] = _missing("INSUFFICIENT_HISTORY",
            volume_unit="shares/contracts")
    else:
        current_rows, reference_rows = volume_rows[-21:], volume_rows[-273:-21]
        current_mean, reference_mean = mean(float(r.volume) for r in current_rows), mean(float(r.volume) for r in reference_rows)
        details = {"window_mean_volume": current_mean, "reference_mean_volume": reference_mean,
                   "window_start": current_rows[0].date, "window_end": current_rows[-1].date,
                   "reference_start": reference_rows[0].date, "reference_end": reference_rows[-1].date,
                   "volume_unit": "shares/contracts"}
        fields["relative_trading_activity"] = (_missing("REFERENCE_MISSING", **details)
            if not isfinite(current_mean) or not isfinite(reference_mean) or reference_mean <= 0
                                                else _field(current_mean / reference_mean, **details))
    fields["liquidity_stress_z"] = _liquidity_missing()

    if benchmark is None:
        fields["benchmark_beta"] = _missing(_NOT_APPLICABLE)
        fields["benchmark_momentum_z"] = _missing(_NOT_APPLICABLE, window_days=21, reference_days=252)
    else:
        other = series(benchmark, brent_wti)
        own_beta = own
        if brent_wti:
            # The registered Yahoo proxies share the NYMEX calendar. Daily
            # returns require both ends of an actual adjacent-session pair.
            own_beta = _consecutive_closes(own, cutoff, calendar, 64)
            other = _consecutive_closes(other, cutoff, calendar, 274)
            petroleum = _petroleum_levels(series, cutoff, calendar)
            if own_beta and petroleum:
                output["industry_benchmark"]["fields"] = {
                    "benchmark_beta": _benchmark_beta(own_beta, petroleum, _PETROLEUM_NAME),
                    "benchmark_momentum_z": _momentum(petroleum, cutoff=cutoff.isoformat(),
                                                      benchmark=_PETROLEUM_NAME),
                }
        redundant = asset.ticker == benchmark
        if brent_wti and not other:
            fields["benchmark_beta"] = _missing("NOT_OBSERVED", benchmark=benchmark)
        else:
            fields["benchmark_beta"] = _benchmark_beta(own_beta, other, benchmark, redundant=redundant)
        if len(other) < 1 or other[-1][0] != cutoff.isoformat():
            fields["benchmark_momentum_z"] = _missing("NOT_OBSERVED", benchmark=benchmark,
                window_days=21, reference_days=252, **({"redundant": True, "note": "与 M01 同口径，冗余"} if redundant else {}))
        else:
            value = _momentum(other, cutoff=cutoff.isoformat(), benchmark=benchmark, redundant=redundant)
            if redundant:
                value["note"] = "与 M01 同口径，冗余"
            fields["benchmark_momentum_z"] = value

    if not own_on_cutoff:
        fields["relative_trading_activity"] = _missing("NOT_OBSERVED", volume_unit="shares/contracts")
        if benchmark is not None:
            fields["benchmark_beta"] = _missing("NOT_OBSERVED", benchmark=benchmark)
            fields["benchmark_momentum_z"] = _missing("NOT_OBSERVED", benchmark=benchmark,
                window_days=21, reference_days=252)

    try:
        observed_dates = {row.date for row in own_rows if _price(row, raw) is not None}
        fields["data_coverage"] = _coverage(calendar, cutoff, observed_dates)
    except (ValueError, RuntimeError):
        fields["data_coverage"] = _missing("OUT_OF_COVERAGE", expected_days=None,
            observed_days=None, first_expected_date=None, last_expected_date=None, missing_dates=[])
    output["fields"] = fields
    return output


CSV_METRIC_FIELDS = (
    "momentum_z",
    "realized_volatility",
    "relative_trading_activity",
    "liquidity_stress_z",
    "benchmark_beta",
    "benchmark_momentum_z",
    "data_coverage",
)


def csv_headers() -> tuple[str, ...]:
    """Column names appended to query CSV exports."""
    headers = []
    for key in CSV_METRIC_FIELDS:
        headers.extend((key, f"{key}_missing_reason"))
    headers.extend(("profile_id", "cutoff_date", "benchmark"))
    return tuple(headers)


def csv_values(block) -> dict:
    """Flatten a market_state payload for CSV. Missing values are None, not 0."""
    fields = (block or {}).get("fields") or {}
    values = {}
    for key in CSV_METRIC_FIELDS:
        item = fields.get(key) or {}
        value = item.get("value") if isinstance(item, dict) else None
        reason = item.get("missing_reason") if isinstance(item, dict) else None
        values[key] = value
        values[f"{key}_missing_reason"] = reason or ""
    values["profile_id"] = (block or {}).get("profile_id") or ""
    values["cutoff_date"] = (block or {}).get("cutoff_date") or ""
    values["benchmark"] = (block or {}).get("benchmark") or ""
    return values

