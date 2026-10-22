"""Daily (trading-day) event report: raw and adjusted returns plus excess
returns against benchmarks, with a source on every price."""

from datetime import datetime

from .assets import Asset, resolve_asset
from .query import query_event
from .windows import DEFAULT_TRADING_WINDOWS, TradingWindowConfig, window_label_zh


DEFAULT_BENCHMARKS = ("2800.HK", "^HSI")

METHOD = (
    "t0 = news day if published before that day's close, else next HKEX trading day. "
    "Baseline = close of the trading day before t0 (last close before publication). "
    "before_Ntd: return from the close N trading days before the baseline day to the baseline. "
    "after_tK: return from the baseline to the close K trading days after t0. "
    "raw = close as delivered (split-adjusted only); adj = adjusted close (dividends and splits). "
    "excess = stock return - benchmark return on the same dates (simple difference, not a market model). "
    "^HSI is a price index, so its adj return equals its raw return. Missing values are never zero."
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


def daily_report(provider, asset: Asset, published_at: datetime, *, as_of: datetime,
                 benchmarks=DEFAULT_BENCHMARKS,
                 config: TradingWindowConfig = DEFAULT_TRADING_WINDOWS, calendar=None) -> dict:
    raw, adj, adj_error = _pair(provider, asset, published_at, as_of, config, calendar)
    adj_windows = {item.label: item for item in adj.windows} if adj else {}
    windows = {}
    for item in raw.windows:
        other = adj_windows.get(item.label)
        change = None
        if item.price is not None:
            change = (raw.baseline.price - item.price if item.label.startswith("before_")
                      else item.price - raw.baseline.price)
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
            "flag": item.flag,
            "delay_seconds": item.delay_seconds,
            "source": item.source,
            "basis": "raw",
        }
    payload = {
        "ticker": asset.ticker,
        "asset_role": asset.role,
        "published_at": raw.published_at.isoformat(),
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
        "adj_error": adj_error,
        "method": METHOD,
        "data_version": provider.data_version() if hasattr(provider, "data_version") else None,
    }
    comparisons = {}
    for code in dict.fromkeys(benchmarks):
        bench_asset = resolve_asset(code)
        if bench_asset.ticker == asset.ticker:
            continue
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
                "flag": other.flag,
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
