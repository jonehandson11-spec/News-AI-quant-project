import io
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from news_price_query.news import NewsItem
from news_price_query.prices import PriceBar
from web.service import QueryService, RemoteNewsStore


HK = ZoneInfo("Asia/Hong_Kong")


class FakeProvider:
    def __init__(self, bars):
        self._bars = bars
        self.closed = False

    def bars(self, ticker, start, end, interval):
        return [bar for bar in self._bars if bar.ticker == ticker and bar.interval == interval
                and start <= bar.timestamp <= end]

    def close(self):
        self.closed = True


def minute(ticker, time, close):
    return PriceBar(ticker, "minute", datetime.fromisoformat(time), close, "test-feed")


class InternalServiceTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            minute("0700.HK", "2026-09-28T09:59:00+08:00", 100),
            minute("0700.HK", "2026-09-28T11:00:00+08:00", 102),
            minute("2800.HK", "2026-09-28T09:59:00+08:00", 50),
            minute("2800.HK", "2026-09-28T11:00:00+08:00", 51),
            minute("3033.HK", "2026-09-28T09:59:00+08:00", 20),
            minute("3033.HK", "2026-09-28T11:00:00+08:00", 20.2),
        ]
        self.providers = []

        def make_provider():
            provider = FakeProvider(self.bars)
            self.providers.append(provider)
            return provider

        self.service = QueryService(make_provider, news_source=lambda: [],
                                    now=lambda: datetime(2026, 9, 28, 12, tzinfo=HK))

    def test_catalog_and_queries_are_limited_to_seven_assets(self):
        self.assertEqual([item["ticker"] for item in self.service.catalog()], [
            "1211.HK", "0700.HK", "9988.HK", "0981.HK", "0005.HK", "2800.HK", "3033.HK",
        ])
        with self.assertRaisesRegex(ValueError, "首版仅支持"):
            self.service.query("2318.HK", "2026-09-28T10:00:00+08:00")

    def test_query_fetches_stock_and_both_benchmarks_from_provider(self):
        result = self.service.query("腾讯", "2026-09-28T10:00:00+08:00",
                                    benchmarks=["2800.HK", "3033.HK"])
        self.assertEqual(result["baseline"]["source"], "test-feed")
        self.assertEqual(result["windows"]["after_1h"]["price_change"], 2)
        self.assertAlmostEqual(result["benchmarks"]["2800.HK"]["windows"]["after_1h"]["excess_return_pct"], 0)
        self.assertAlmostEqual(result["benchmarks"]["3033.HK"]["windows"]["after_1h"]["excess_return_pct"], 1)
        self.assertTrue(self.providers[0].closed)

    def test_unavailable_tech_benchmark_does_not_hide_stock_result(self):
        class LimitedProvider(FakeProvider):
            def bars(self, ticker, start, end, interval):
                if ticker == "3033.HK":
                    raise RuntimeError("没有该 ETF 的行情权限")
                return super().bars(ticker, start, end, interval)

        service = QueryService(lambda: LimitedProvider(self.bars), news_source=lambda: [],
                               now=lambda: datetime(2026, 9, 28, 12, tzinfo=HK))
        result = service.query("腾讯", "2026-09-28T10:00:00+08:00",
                               benchmarks=["2800.HK", "3033.HK"])
        self.assertEqual(result["windows"]["after_1h"]["price"], 102)
        self.assertIn("行情权限", result["benchmarks"]["3033.HK"]["error"])

    def test_news_search_uses_date_range_and_deduplicates_articles(self):
        news = [
            NewsItem("a", "BBC", "腾讯发布游戏", "", datetime(2026, 9, 27, 9, tzinfo=HK), "https://example.com/a", "zh"),
            NewsItem("a", "BBC", "腾讯发布游戏", "", datetime(2026, 9, 27, 9, tzinfo=HK), "https://example.com/a", "zh"),
            NewsItem("b", "BBC", "腾讯财报", "", datetime(2026, 9, 20, 9, tzinfo=HK), "https://example.com/b", "zh"),
            NewsItem("c", "BBC", "比亚迪新车", "", datetime(2026, 9, 27, 9, tzinfo=HK), "https://example.com/c", "zh"),
        ]
        service = QueryService(lambda: FakeProvider([]), news_source=lambda: news)
        result = service.news("0700.HK", "2026-09-25", "2026-09-30")
        self.assertEqual([item["article_id"] for item in result], ["a"])
        self.assertEqual(result[0]["match_type"], "title_mention")

    def test_remote_news_is_cached_and_keeps_last_snapshot_on_fetch_error(self):
        payload = ("article_id,source,title,content,publish_time,crawl_time,url,language\n"
                   "a,BBC,腾讯财报,,2026-09-28T09:00:00+08:00,2026-09-28T10:00:00+08:00,https://example.com/a,zh\n").encode()
        calls = []
        times = iter([0, 10, 100])

        def opener(url, timeout):
            calls.append(url)
            if len(calls) > 1:
                raise OSError("offline")
            return io.BytesIO(payload)

        store = RemoteNewsStore("https://example.com/news.csv", opener=opener,
                                monotonic=lambda: next(times), refresh_seconds=60)
        self.assertEqual(len(store()), 1)
        self.assertEqual(len(store()), 1)
        self.assertEqual(len(store()), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(store.stale)


if __name__ == "__main__":
    unittest.main()
