import http.client
import json
from datetime import datetime
from threading import Thread
import unittest
from zoneinfo import ZoneInfo

from news_price_query.news import NewsItem
from news_price_query.prices import PriceBar
from web.server import create_server
from web.service import QueryService


HK = ZoneInfo("Asia/Hong_Kong")


class Provider:
    def bars(self, ticker, start, end, interval):
        rows = [
            PriceBar("0700.HK", "minute", datetime(2026, 9, 28, 9, 59, tzinfo=HK), 100, "fixture"),
            PriceBar("0700.HK", "minute", datetime(2026, 9, 28, 11, tzinfo=HK), 102, "fixture"),
        ]
        return [row for row in rows if row.ticker == ticker and row.interval == interval
                and start <= row.timestamp <= end]


class InternalHttpTests(unittest.TestCase):
    def setUp(self):
        item = NewsItem("one", "BBC", "腾讯发布新游戏", "", datetime(2026, 9, 28, 10, tzinfo=HK),
                        "https://example.com/one", "zh")
        service = QueryService(Provider, news_source=lambda: [item],
                               now=lambda: datetime(2026, 9, 28, 12, tzinfo=HK), mode="minute")
        self.server = create_server(service, "127.0.0.1", 0)
        self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def request_json(self, method, path, body=None):
        headers = {"Content-Type": "application/json"} if body is not None else {}
        self.connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read())

    def test_catalog_news_and_automatic_price_query_are_served_over_http(self):
        status, assets = self.request_json("GET", "/api/assets")
        self.assertEqual(status, 200)
        self.assertEqual(len(assets["assets"]), 7)
        status, news = self.request_json("GET", "/api/news?asset=0700.HK&start=2026-09-28&end=2026-09-28")
        self.assertEqual(status, 200)
        self.assertEqual([row["article_id"] for row in news["matches"]], ["one"])
        status, quote = self.request_json("POST", "/api/query", {
            "asset": "0700.HK", "published_at": "2026-09-28T10:00:00+08:00", "benchmarks": [],
        })
        self.assertEqual(status, 200)
        self.assertEqual(quote["windows"]["after_1h"]["price"], 102)

    def test_unsupported_asset_returns_a_clear_client_error(self):
        status, body = self.request_json("POST", "/api/query", {
            "asset": "2318.HK", "published_at": "2026-09-28T10:00:00+08:00", "benchmarks": [],
        })
        self.assertEqual(status, 400)
        self.assertIn("首版仅支持", body["error"])

    def test_internal_page_is_served_without_exposing_python_source(self):
        self.connection.request("GET", "/")
        page = self.connection.getresponse()
        self.assertEqual(page.status, 200)
        self.assertIn("自动获取价格", page.read().decode("utf-8"))
        self.connection.request("GET", "/service.py")
        source = self.connection.getresponse()
        self.assertEqual(source.status, 404)
        source.read()


if __name__ == "__main__":
    unittest.main()
