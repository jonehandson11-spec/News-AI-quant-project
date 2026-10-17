"""Internal query service backed by the team's news feed and Futu OpenD."""

from datetime import date, datetime
import io
from threading import Lock
import time
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from news_price_query.assets import ASSETS, resolve_asset
from news_price_query.cli import _result_dict
from news_price_query.news import find_direct_match, parse_news
from news_price_query.query import query_event


HK = ZoneInfo("Asia/Hong_Kong")
ALLOWED = {asset.ticker: asset for asset in ASSETS}
BENCHMARKS = {asset.ticker for asset in ASSETS if asset.role == "benchmark"}
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


class QueryService:
    def __init__(self, provider_factory, *, news_source=None, now=None):
        self.provider_factory = provider_factory
        self.news_source = news_source or RemoteNewsStore()
        self.now = now or (lambda: datetime.now(HK))

    @staticmethod
    def _asset(value):
        asset = resolve_asset(value)
        if asset.ticker not in ALLOWED:
            raise ValueError("首版仅支持七个已登记标的")
        return asset

    def catalog(self):
        return [{"ticker": asset.ticker, "name": asset.aliases[0], "role": asset.role}
                for asset in ASSETS]

    def news(self, asset_name, start=None, end=None):
        asset = self._asset(asset_name)
        first = date.fromisoformat(start) if start else date.min
        last = date.fromisoformat(end) if end else date.max
        if first > last:
            raise ValueError("开始日期不能晚于结束日期")
        found = []
        seen = set()
        if asset.role != "equity":
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

    def query(self, asset_name, published_at, *, benchmarks=()):
        asset = self._asset(asset_name)
        if not isinstance(published_at, str):
            raise ValueError("新闻发布时间必须包含时区")
        event_time = datetime.fromisoformat(published_at)
        if event_time.tzinfo is None or event_time.utcoffset() is None:
            raise ValueError("新闻发布时间必须包含时区")
        if not isinstance(benchmarks, (list, tuple)):
            raise ValueError("benchmarks 必须是标的代码列表")
        selected = list(dict.fromkeys(benchmarks))
        if any(code not in BENCHMARKS for code in selected):
            raise ValueError("对照标的只支持 2800.HK 和 3033.HK")
        as_of = self.now()
        provider = self.provider_factory()
        try:
            main = query_event(provider, asset, event_time, as_of=as_of)
            payload = _result_dict(main)
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
                    other = query_event(provider, ALLOWED[code], event_time, as_of=as_of)
                except (ValueError, RuntimeError) as exc:
                    comparisons[code] = {"error": str(exc)}
                    continue
                aligned_baseline = (
                    abs((main.baseline.observed_at - other.baseline.observed_at).total_seconds()) <= 60
                    and main.baseline.basis == other.baseline.basis
                    and main.baseline_status == other.baseline_status == "recent"
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
