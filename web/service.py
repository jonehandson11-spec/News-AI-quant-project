"""Internal query service backed by the team's news feed and Futu OpenD."""

from datetime import date, datetime, time as clock_time, timedelta
import io
from math import isfinite
from threading import Lock
import time
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from dataclasses import replace
from pathlib import Path

from news_price_query.assets import ASSETS, RESEARCH_ASSETS, resolve_asset, market_of_ticker, _futu_code, Asset
from news_price_query.cli import _result_dict
from news_price_query.daily_report import benchmarks_for, daily_report, hourly_report
from news_price_query.gvs_quality import read_quality
from news_price_query.intraday_sync import IntradayUnavailable, MINUTE_INTERVALS, minute_bar_end, sync_intraday
from news_price_query.market_calendars import calendar_for
from news_price_query.news import find_direct_match, parse_news, read_news
from news_price_query.query import query_event
from news_price_query.windows import hourly_bar_end


HK = ZoneInfo("Asia/Hong_Kong")
ALLOWED = {asset.ticker: asset for asset in ASSETS}
BENCHMARKS = {asset.ticker for asset in ASSETS if asset.role == "benchmark"}
OIL_TICKERS = {asset.ticker for asset in RESEARCH_ASSETS if asset.role != "benchmark"}
PRICE_INTERVALS = (*MINUTE_INTERVALS, "1h", "1d")
NEWS_CSV_LINK = "https://github.com/jonehandson11-spec/News-AI-quant-project/blob/main/data/news.csv"
DEFAULT_NEWS_URL = (
    "https://raw.githubusercontent.com/jonehandson11-spec/"
    "News-AI-quant-project/main/data/news.csv"
)


class RemoteNewsStore:
    """Refresh public team news periodically; retain the last good snapshot."""

    def __init__(self, url=DEFAULT_NEWS_URL, *, opener=urlopen,
                 monotonic=time.monotonic, refresh_seconds=900):
        self.url = url
        self.opener = opener
        self.monotonic = monotonic
        self.refresh_seconds = refresh_seconds
        self._items = None
        self._next_refresh = 0
        self._lock = Lock()
        self.stale = False
        self.source_info = {"label": "GitHub 新闻 CSV", "url": NEWS_CSV_LINK if url == DEFAULT_NEWS_URL else None}

    def __call__(self):
        with self._lock:
            now = self.monotonic()
            if self._items is not None and now < self._next_refresh:
                return self._items
            self._next_refresh = now + self.refresh_seconds
            try:
                with self.opener(self.url, timeout=15) as response:
                    content = response.read(32_000_001)
                if len(content) > 32_000_000:
                    raise ValueError("新闻数据超过 32 MB 上限")
                items = parse_news(io.StringIO(content.decode("utf-8-sig")))
            except (OSError, UnicodeError, ValueError):
                self.stale = True
                if self._items is None:
                    raise
                return self._items
            self._items = items
            self.stale = False
            return items


class LocalNewsStore:
    """Read the team news CSV from a local path; re-read when the file changes."""

    def __init__(self, path, *, repository_snapshot=False):
        self.path = Path(path)
        self._items = None
        self._mtime = None
        self._lock = Lock()
        self.stale = False
        self.source_info = {
            "label": "仓库 data/news.csv" if repository_snapshot else "本地新闻 CSV",
            "url": NEWS_CSV_LINK if repository_snapshot else None,
        }

    def __call__(self):
        with self._lock:
            mtime = self.path.stat().st_mtime
            if self._items is None or mtime != self._mtime:
                self._items = read_news(self.path)
                self._mtime = mtime
            return self._items


class QueryService:
    def __init__(self, provider_factory, *, news_source=None, now=None, mode="hourly",
                 instruments=None, price_source="富途 OpenD", price_source_en=None, data_version=None,
                 yahoo_ondemand=False, ondemand_ensure=None, gvs_quality_source=None):
        """instruments: optional callable returning price_store.Instrument rows
        (local price database); they are added to the seven registered assets.
        yahoo_ondemand: if a ticker is missing from the local store, fetch it
        from Yahoo and cache it. Off by default so a local PC stays local-only.
        ondemand_ensure: optional hook used in tests in place of ensure_ticker.
        gvs_quality_source: optional read-only adapter for versioned GVS quality assessments.
        """
        if mode not in ("minute", "daily", "hourly"):
            raise ValueError(f"Unknown query mode: {mode}")
        self.mode = mode
        self.provider_factory = provider_factory
        self.news_source = news_source or RemoteNewsStore()
        self.now = now or (lambda: datetime.now(HK))
        self.instruments = instruments
        self.price_source = price_source
        self.price_source_en = price_source_en
        self.data_version = data_version or (lambda: None)
        self.yahoo_ondemand = bool(yahoo_ondemand)
        self.ondemand_ensure = ondemand_ensure
        self.gvs_quality_source = gvs_quality_source

    def _allowed(self):
        allowed = dict(ALLOWED)
        if self.instruments is not None:
            for item in RESEARCH_ASSETS:
                if item.role in ("equity", "futures"):
                    allowed.setdefault(item.ticker, item)
            for item in self.instruments():
                if item.role not in ("equity", "futures"):
                    continue
                if item.ticker in allowed:
                    base = allowed[item.ticker]
                    extra = tuple(a for a in (item.name, *item.aliases) if a and a not in base.aliases)
                    allowed[item.ticker] = replace(base, aliases=base.aliases + extra)
                    continue
                base = resolve_asset(item.ticker)
                names = tuple(name for name in (item.name, *item.aliases) if name)
                allowed[item.ticker] = replace(base, aliases=names + base.aliases)
        return allowed

    def _resolve_any(self, value):
        """Resolve a registered name, an HK code, or any ticker whose market is known."""
        try:
            return resolve_asset(value)
        except ValueError:
            pass
        from news_price_query.price_update import normalize_ticker
        ticker = normalize_ticker(str(value))
        try:
            market, currency = market_of_ticker(ticker)
        except NotImplementedError as exc:
            raise ValueError(f"无法识别标的 {value}：{exc}") from exc
        return Asset(ticker, _futu_code(ticker), "equity", (ticker,), market=market, currency=currency)

    def _asset(self, value):
        allowed = self._allowed()
        key = "".join(str(value).casefold().split())
        for candidate in allowed.values():
            aliases = {"".join(alias.casefold().split()) for alias in candidate.aliases}
            if key in aliases or key == "".join(candidate.ticker.casefold().split()):
                return candidate
        try:
            asset = self._resolve_any(value) if self.yahoo_ondemand else resolve_asset(value)
        except ValueError:
            asset = None
        if asset is not None and asset.ticker in allowed:
            return allowed[asset.ticker]
        if self.yahoo_ondemand and asset is not None:
            return asset
        if self.instruments is None:
            raise ValueError("首版仅支持七个已登记标的")
        raise ValueError("只支持已登记标的和本地价格库中的标的；新股票请先加入股票池并运行 update_prices.py")

    def catalog(self):
        return [{"ticker": asset.ticker, "asset_id": asset.asset_id,
                 "name": asset.name_zh or asset.aliases[0], "name_en": asset.name_en,
                 "role": asset.role,
                 "stage": asset.stage, "market": asset.market, "currency": asset.currency,
                 "asset_type": asset.asset_type, "unit": asset.unit, "queryable": asset.queryable,
                 "data_status": asset.data_status, "oil_research": asset.ticker in OIL_TICKERS}
                for asset in self._allowed().values()]

    def _ensure_prices(self, *tickers):
        """If on-demand is on, fetch any ticker that is not yet in the local store."""
        if not self.yahoo_ondemand:
            return
        needed = [code for code in dict.fromkeys(tickers) if code]
        if not needed:
            return
        provider = self.provider_factory()
        try:
            store = getattr(provider, "store", None)
            if store is None:
                return
            ensure = self.ondemand_ensure
            if ensure is None:
                from news_price_query.price_update import ensure_ticker
                ensure = ensure_ticker
            for ticker in needed:
                ensure(store, ticker, now=self.now())
        finally:
            close = getattr(provider, "close", None)
            if close:
                close()

    def bars(self, asset_name, interval, start, end):
        """OHLC bars for the K-line chart; requires the local price database."""
        from news_price_query.chart_bars import chart_bars, normalize_interval

        asset = self._asset(asset_name)
        interval = normalize_interval(interval)
        if interval not in MINUTE_INTERVALS:
            self._ensure_prices(asset.ticker)
        provider = self.provider_factory()
        try:
            store = getattr(provider, "store", None)
            if store is None:
                raise ValueError("K线图只支持本地价格库")
            return chart_bars(store, asset, interval, start, end, now=self.now(), sync_minutes=self.yahoo_ondemand)
        finally:
            close = getattr(provider, "close", None)
            if close:
                close()

    def source_info(self):
        return {"price_source": self.price_source, "price_source_en": self.price_source_en, "mode": self.mode,
                "data_version": self.data_version(), "yahoo_ondemand": self.yahoo_ondemand}

    def price(self, asset_name, interval, on_date=None, *, market_state=False, as_of=None):
        """Look up a stored close without reading the news feed."""
        if interval != "auto" and interval not in PRICE_INTERVALS:
            raise ValueError("Choose Auto, Daily, Hourly, or a supported minute interval.")
        try:
            asset = self._asset(asset_name)
        except ValueError:
            raise ValueError("Symbol unavailable. Check the code or select a listed asset.") from None
        if not asset.queryable:
            raise ValueError(f"No free price source is registered for {asset.ticker}.")
        calendar = calendar_for(asset.market)
        provider_now = self.now().astimezone(calendar.tz)
        now = provider_now
        if as_of:
            explicit = datetime.fromisoformat(as_of)
            if explicit.utcoffset() is None:
                raise ValueError("The cutoff must include a timezone.")
            now = min(now, explicit.astimezone(calendar.tz))
        requested = date.fromisoformat(on_date) if on_date else now.date()
        if on_date and requested.isoformat() != on_date:
            raise ValueError("Use YYYY-MM-DD for the date.")
        if requested > now.date():
            raise ValueError("Choose a date at or before the exchange-local cutoff date.")
        cutoff = min(datetime.combine(requested, clock_time.max, calendar.tz), now)
        start = datetime.combine(max(calendar.first, requested - timedelta(days=14)),
                                 clock_time.min, calendar.tz)

        provider = self.provider_factory()
        try:
            store = getattr(provider, "store", None)
            if store is None:
                raise ValueError("Direct price lookup requires the local price database.")
            fetch_failed = False
            automatic = interval == "auto"
            allowed = PRICE_INTERVALS if automatic else (interval,)
            if self.yahoo_ondemand and (automatic or interval in MINUTE_INTERVALS):
                try:
                    self._sync_minute_prices(provider, asset, allowed, start, cutoff, provider_now, calendar,
                                             automatic=automatic)
                except IntradayUnavailable:
                    fetch_failed = True

            resolved, observation = self._best_price(provider, asset, allowed, start, cutoff, calendar)
            needs_baseline = observation is None or (automatic and observation[0].date() < cutoff.date())
            if needs_baseline and self.yahoo_ondemand and (automatic or interval not in MINUTE_INTERVALS):
                try:
                    self._ensure_prices(asset.ticker)
                except (ValueError, RuntimeError, OSError):
                    # The downloader can successfully persist day closes even if no hourly data exists.
                    fetch_failed = True
                resolved, observation = self._best_price(provider, asset, allowed, start, cutoff, calendar)

            observed, price, source = observation or (None, None, None)
            status = "missing" if observed is None else (
                "observed" if observed.date() == requested else "prior_session")
            result = {"symbol": asset.ticker, "asset_id": asset.asset_id,
                      "name": asset.name_zh or asset.aliases[0], "name_en": asset.name_en,
                      "market": asset.market, "exchange_timezone": str(calendar.tz),
                      "currency": asset.currency, "unit": asset.unit, "interval": resolved,
                      "requested_interval": interval,
                      "requested_date": requested.isoformat(), "cutoff_at": cutoff.isoformat(),
                      "observed_at": observed.isoformat() if observed else None, "price": price,
                      "source": source, "price_status": status}
            if observation is None:
                result["message"] = ("The provider returned no usable prices." if fetch_failed
                                     else "No completed prices for this interval in the selected 14-day lookback.")
            if market_state and self.yahoo_ondemand:
                from news_price_query.market_state import reference_tickers_for_market_state
                for ticker in reference_tickers_for_market_state(asset):
                    if ticker != asset.ticker and not store.last_date(ticker):
                        try:
                            self._ensure_prices(ticker)
                        except (ValueError, RuntimeError, OSError):
                            # A missing reference cannot invalidate the asset's quoted price.
                            pass
                if not store.last_date(asset.ticker) and (automatic or interval in MINUTE_INTERVALS):
                    try:
                        self._ensure_prices(asset.ticker)
                    except (ValueError, RuntimeError, OSError):
                        pass
            return self._attach_market_state(result, provider, asset, cutoff, now, market_state)
        finally:
            close = getattr(provider, "close", None)
            if close:
                close()

    def _sync_minute_prices(self, provider, asset, allowed, start, cutoff, now, calendar, *, automatic):
        # Fetch only the requested session and its nearby history. Old relative dates and
        # unavailable provider windows are skipped by the synchronizer.
        recent_start = max(start, datetime.combine(cutoff.date() - timedelta(days=2), clock_time.min, calendar.tz))
        for kind in allowed:
            if kind not in MINUTE_INTERVALS:
                break
            existing = self._price_observation(provider, asset, kind, recent_start, cutoff, calendar)
            if automatic or existing is None:
                sync_intraday(provider.store, asset, kind, recent_start, cutoff, now=now)
            if self._price_observation(provider, asset, kind, recent_start, cutoff, calendar) is not None:
                break

    @staticmethod
    def _best_price(provider, asset, allowed, start, cutoff, calendar):
        observations = ((kind, QueryService._price_observation(provider, asset, kind, start, cutoff, calendar))
                        for kind in allowed)
        # The finest native bar wins when two completed observations have the same timestamp.
        return max(((kind, item) for kind, item in observations if item is not None),
                   key=lambda pair: pair[1][0], default=(allowed[-1], None))

    @staticmethod
    def _price_observation(provider, asset, interval, start, cutoff, calendar):
        if start > cutoff:
            return None
        if interval in MINUTE_INTERVALS:
            utc = ZoneInfo("UTC")
            first = start.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            last = cutoff.astimezone(utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = provider.store.hourly_rows(asset.ticker, first, last, interval=interval)
            for row in reversed(rows):
                end = minute_bar_end(datetime.fromisoformat(row.bar_start_local), calendar, interval)
                if end is not None and end <= cutoff and isfinite(row.close):
                    return end, row.close, row.source
            return None
        if interval == "1h":
            completed = []
            for bar in provider.bars(asset.ticker, start, cutoff, "hour"):
                if not calendar.first <= bar.timestamp.astimezone(calendar.tz).date() <= calendar.last:
                    continue
                end = hourly_bar_end(bar, calendar)
                if end is not None and end <= cutoff and bar.close is not None and isfinite(bar.close):
                    completed.append((end, bar.close, bar.source))
            return max(completed, key=lambda row: row[0], default=None)

        rows = provider.store.rows(asset.ticker, start.date().isoformat(), cutoff.date().isoformat())
        for row in reversed(rows):
            row_day = date.fromisoformat(row.date)
            if not calendar.first <= row_day <= calendar.last:
                continue
            day = calendar.day(row_day)
            if day.is_trading and day.close_at() <= cutoff and row.close is not None and isfinite(row.close):
                return day.close_at(), row.close, row.source
        return None

    def news(self, asset_name, start=None, end=None):
        asset = self._asset(asset_name)
        first = date.fromisoformat(start) if start else date.min
        last = date.fromisoformat(end) if end else date.max
        if first > last:
            raise ValueError("开始日期不能晚于结束日期")
        found = []
        seen = set()
        if asset.role not in ("equity", "futures"):
            return found
        for item in self.news_source():
            published_date = item.published_at.astimezone(HK).date()
            if not first <= published_date <= last or item.article_id in seen:
                continue
            match = find_direct_match(item, asset)
            if match is None:
                continue
            seen.add(item.article_id)
            found.append({
                "article_id": item.article_id,
                "ticker": asset.ticker,
                "title": item.title,
                "source": item.source,
                "url": item.url,
                "published_at": item.published_at.isoformat(),
                "match_type": match.kind,
                "evidence": match.evidence,
                "verified_by_human": False,
            })
        return sorted(found, key=lambda item: item["published_at"], reverse=True)

    def _attach_market_state(self, payload, provider, asset, event_time, as_of, enabled):
        if not enabled:
            return payload
        from news_price_query.market_state import PROFILE_ID, market_state as calculate_market_state
        store = getattr(provider, "store", None)
        if store is None:
            payload["market_state"] = {"profile_id": PROFILE_ID, "error": "需要本地价格库"}
            return payload
        try:
            state = calculate_market_state(store, asset, event_time, as_of=as_of)
        except (ValueError, RuntimeError) as exc:
            payload["market_state"] = {"profile_id": PROFILE_ID, "error": str(exc)}
        else:
            state["quality"] = read_quality(self.gvs_quality_source, asset,
                                           as_of=min(event_time, as_of), profile_id=state["profile_id"])
            payload["market_state"] = state
        return payload

    def query(self, asset_name, published_at, *, benchmarks=(), market_state=False):
        asset = self._asset(asset_name)
        if not asset.queryable:
            raise ValueError(f"{asset.ticker} 无免费数据：{asset.notes or ''}")
        if not isinstance(published_at, str):
            raise ValueError("新闻发布时间必须包含时区")
        event_time = datetime.fromisoformat(published_at)
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError("新闻发布时间必须包含时区")
        if not isinstance(benchmarks, (list, tuple)):
            raise ValueError("benchmarks 必须是标的代码列表")
        selected = list(dict.fromkeys(benchmarks))
        if self.mode in ("daily", "hourly"):
            allowed_benchmarks = BENCHMARKS | {"^HSI"} | {a.ticker for a in RESEARCH_ASSETS if a.role == "benchmark"}
            if any(code not in allowed_benchmarks for code in selected):
                raise ValueError("对照标的只支持已登记的对照 ETF 或指数")
        elif any(code not in BENCHMARKS for code in selected):
            raise ValueError("对照标的只支持 2800.HK 和 3033.HK")
        as_of = self.now()
        if self.yahoo_ondemand:
            from news_price_query.market_state import benchmark_for_market_state
            market_benchmark = benchmark_for_market_state(asset)
            needed = [asset.ticker, *benchmarks_for(asset), *selected, "2800.HK", "^HSI"]
            if market_state and market_benchmark:
                needed.append(market_benchmark)
            self._ensure_prices(*needed)
        provider = self.provider_factory()
        if self.mode == "hourly":
            try:
                payload = hourly_report(provider, asset, event_time, as_of=as_of,
                                        benchmarks=(*benchmarks_for(asset), *selected))
                return self._attach_market_state(payload, provider, asset, event_time, as_of, market_state)
            finally:
                close = getattr(provider, "close", None)
                if close: close()
        if self.mode == "daily":
            try:
                payload = daily_report(provider, asset, event_time, as_of=as_of,
                                       benchmarks=(*benchmarks_for(asset), *selected))
                return self._attach_market_state(payload, provider, asset, event_time, as_of, market_state)
            finally:
                close = getattr(provider, "close", None)
                if close:
                    close()
        try:
            main = query_event(provider, asset, event_time, as_of=as_of, mode=self.mode)
            payload = _result_dict(main)
            self._attach_market_state(payload, provider, asset, event_time, as_of, market_state)
            for label, window in payload["windows"].items():
                if window["price"] is not None:
                    window["price_change"] = (
                        main.baseline.price - window["price"] if label.startswith("before_")
                        else window["price"] - main.baseline.price
                    )
                else:
                    window["price_change"] = None
            comparisons = {}
            for code in selected:
                if code == asset.ticker:
                    continue
                try:
                    other = query_event(provider, ALLOWED[code], event_time, as_of=as_of, mode=self.mode)
                except (ValueError, RuntimeError) as exc:
                    comparisons[code] = {"error": str(exc)}
                    continue
                aligned_baseline = (
                    abs((main.baseline.observed_at - other.baseline.observed_at).total_seconds()) <= 60
                    and main.baseline.basis == other.baseline.basis
                    and main.baseline_status == other.baseline_status
                    and main.baseline_status in ("recent", "on_date")
                )
                other_windows = {}
                for own_window, other_window in zip(main.windows, other.windows):
                    aligned_window = (
                        aligned_baseline
                        and own_window.observed_at is not None
                        and other_window.observed_at is not None
                        and (own_window.observed_at.date() == other_window.observed_at.date()
                             if own_window.label.startswith("before_") else
                             abs((own_window.observed_at - other_window.observed_at).total_seconds()) <= 60)
                    )
                    other_windows[own_window.label] = {
                        "observed_at": other_window.observed_at.isoformat() if other_window.observed_at else None,
                        "return_pct": other_window.return_pct,
                        "status": other_window.status,
                        "excess_return_pct": (
                            own_window.return_pct - other_window.return_pct
                            if aligned_window and own_window.return_pct is not None
                            and other_window.return_pct is not None else None
                        ),
                    }
                comparisons[code] = {"windows": other_windows}
            payload["benchmarks"] = comparisons
            return payload
        finally:
            close = getattr(provider, "close", None)
            if close is not None:
                close()
