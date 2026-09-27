from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from crawler import sina
from crawler.sina_impl.parser import parse_article

START = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
END = START + timedelta(days=2)
CONTENT = "这是一段完整的财经新闻正文，用于验证文章日期与正文提取。" * 3


def article_url(index: int) -> str:
    return f"https://finance.sina.com.cn/stock/2026-09-27/doc-test{index}.shtml"


def html(published: datetime | None = END, *, direct: bool = False) -> str:
    meta = f'<meta property="article:published_time" content="{published.isoformat()}">' if published else ""
    body = CONTENT if direct else f"<p>{CONTENT}</p><p>海量资讯、精准解读，尽在新浪财经APP</p>"
    return f"<html><head>{meta}</head><body><h1>财经新闻标题</h1><div id='artibody'>{body}</div></body></html>"


def item(index: int, published: datetime | None = END) -> dict:
    return {"url": article_url(index), "title": "测试财经新闻", "ctime": int(published.timestamp()) if published else None}


def response(*, text: str = "", payload=None, status: int = 200, headers=None):
    def parse_json():
        if isinstance(payload, Exception):
            raise payload
        return payload
    return SimpleNamespace(status_code=status, text=text, headers=headers or {}, encoding="utf-8", apparent_encoding="utf-8", json=parse_json)


class FakeSession:
    def __init__(self, pages=None, articles=None, robots=None):
        self.pages = pages or {}
        self.articles = articles or {}
        self.robots = robots or {}
        self.calls = []
        self.headers = {}
        self.closed = False

    def mount(self, *args, **kwargs):
        pass

    def close(self):
        self.closed = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            result = self.robots.get(parts.netloc, response(text="User-agent: *\nAllow: /"))
        elif parts.netloc == "feed.mix.sina.com.cn":
            page = int(parse_qs(parts.query)["page"][0])
            entries = self.pages.get(page, [])
            result = entries if isinstance(entries, (Exception, SimpleNamespace)) else response(payload={"result": {"data": entries}})
        else:
            result = self.articles.get(url, response(text=html()))
        if isinstance(result, Exception):
            raise result
        return result


class SinaCollectionTests(unittest.TestCase):
    def run_collect(self, session, *, limit=10, known=None):
        report = {}
        with patch.object(sina.requests, "Session", return_value=session), patch.object(sina.time, "sleep"):
            rows = list(sina.collect(start=START, end=END, known_urls=known or set(), limit=limit, workdir=Path("unused"), report=report))
        self.assertTrue(session.closed)
        return rows, report

    def test_zero_limit_never_creates_session(self):
        report = {}
        with patch.object(sina.requests, "Session") as factory:
            self.assertEqual([], list(sina.collect(start=START, end=END, known_urls=set(), limit=0, workdir=Path("unused"), report=report)))
        factory.assert_not_called()
        self.assertEqual("complete", report["status"])

    def test_limit_caps_yields_and_article_requests(self):
        session = FakeSession(pages={1: [item(1), item(2), item(3)]})
        rows, report = self.run_collect(session, limit=2)
        self.assertEqual(2, len(rows))
        self.assertEqual(2, report["counts"]["yielded"])
        self.assertNotIn(article_url(3), [url for url, _ in session.calls])
        self.assertEqual(set(rows[0]), {"article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"})
        self.assertTrue(all(datetime.fromisoformat(row["publish_time"]).tzinfo for row in rows))

    def test_known_urls_are_skipped_before_fetch_and_canonicalized(self):
        session = FakeSession(pages={1: [item(1), item(2)]})
        rows, report = self.run_collect(session, known={article_url(1).replace("https:", "http:") + "?share=1"})
        self.assertEqual([article_url(2)], [row["url"] for row in rows])
        self.assertNotIn(article_url(1), [url for url, _ in session.calls])
        self.assertEqual(1, report["counts"]["skipped_known"])

    def test_only_original_article_date_can_admit_or_reject(self):
        session = FakeSession(pages={1: [item(1), item(2), item(3, None)]}, articles={
            article_url(1): response(text=html(START - timedelta(seconds=1))),
            article_url(2): response(text=html(None)),
            article_url(3): response(text=html(START)),
        })
        rows, report = self.run_collect(session)
        self.assertEqual([article_url(3)], [row["url"] for row in rows])
        self.assertEqual(1, report["counts"]["skipped_window"])
        self.assertEqual(1, report["counts"]["invalid"])

    def test_both_window_boundaries_are_inclusive(self):
        session = FakeSession(pages={1: [item(1, START), item(2, END)]}, articles={article_url(1): response(text=html(START)), article_url(2): response(text=html(END))})
        rows, report = self.run_collect(session)
        self.assertEqual(2, len(rows))
        self.assertEqual("complete", report["status"])

    def test_two_consecutive_entirely_old_pages_end_pagination(self):
        old = START - timedelta(seconds=1)
        session = FakeSession(pages={1: [item(1, old)], 2: [item(2)], 3: [item(3, old)], 4: [item(4, old)], 5: [item(5)]})
        rows, report = self.run_collect(session)
        self.assertEqual([article_url(2)], [row["url"] for row in rows])
        self.assertEqual(4, report["counts"]["pages"])
        self.assertIn("two consecutive", report["reason"])

    def test_page_limit_is_reported_as_partial(self):
        session = FakeSession(pages={1: [item(1)], 2: [item(2)], 3: [item(3)]})
        with patch.object(sina, "MAX_PAGES", 2):
            rows, report = self.run_collect(session)
        self.assertEqual(2, len(rows))
        self.assertEqual("partial", report["status"])
        self.assertEqual("maximum page count reached", report["reason"])

    def test_blocking_status_stops_source_without_another_article(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                session = FakeSession(pages={1: [item(1), item(2)]}, articles={article_url(1): response(status=status)})
                rows, report = self.run_collect(session)
                self.assertEqual([], rows)
                self.assertEqual("failed", report["status"])
                self.assertNotIn(article_url(2), [url for url, _ in session.calls])

    def test_three_consecutive_network_errors_stop_source(self):
        session = FakeSession(pages={1: [item(1), item(2), item(3), item(4)]}, articles={article_url(i): requests.Timeout("C:/secret/path") for i in range(1, 4)})
        rows, report = self.run_collect(session)
        self.assertEqual([], rows)
        self.assertEqual(3, report["counts"]["network_errors"])
        self.assertEqual("failed", report["status"])
        self.assertNotIn(article_url(4), [url for url, _ in session.calls])
        self.assertNotIn("secret", str(report))

    def test_robots_disallow_prevents_fetch(self):
        session = FakeSession(pages={1: [item(1)]}, robots={"feed.mix.sina.com.cn": response(text="User-agent: *\nDisallow: /")})
        rows, report = self.run_collect(session)
        self.assertEqual([], rows)
        self.assertEqual(1, len(session.calls))
        self.assertEqual("failed", report["status"])

    def test_robots_404_allows_and_access_denial_stops(self):
        session = FakeSession(pages={1: [item(1)]}, robots={"feed.mix.sina.com.cn": response(status=404)})
        rows, _ = self.run_collect(session)
        self.assertEqual(1, len(rows))
        blocked = FakeSession(robots={"feed.mix.sina.com.cn": response(status=403)})
        rows, report = self.run_collect(blocked)
        self.assertEqual([], rows)
        self.assertEqual("failed", report["status"])
        self.assertEqual(1, len(blocked.calls))

    def test_invalid_feed_structure_is_an_error(self):
        session = FakeSession(pages={1: response(payload={"error": "bad response"})})
        _, report = self.run_collect(session)
        self.assertEqual("partial", report["status"])
        self.assertIn("feed JSON", report["errors"][0]["reason"])

    def test_error_details_are_capped(self):
        session = FakeSession(pages={1: [item(i) for i in range(25)]}, articles={article_url(i): response(text=html(None)) for i in range(25)})
        _, report = self.run_collect(session)
        self.assertEqual(25, report["counts"]["errors"])
        self.assertEqual(20, len(report["errors"]))

    def test_discovery_url_duplicates_only_fetch_once(self):
        session = FakeSession(pages={1: [item(1), item(1)], 2: [item(1)]})
        rows, _ = self.run_collect(session)
        self.assertEqual(1, len(rows))
        self.assertEqual(1, [url for url, _ in session.calls].count(article_url(1)))

    def test_redirect_to_non_sina_is_not_followed(self):
        session = FakeSession(pages={1: [item(1)]}, articles={article_url(1): response(status=302, headers={"Location": "https://example.com/article"})})
        rows, report = self.run_collect(session)
        self.assertEqual([], rows)
        self.assertEqual("partial", report["status"])
        self.assertNotIn("https://example.com/article", [url for url, _ in session.calls])

    def test_requests_are_throttled_with_no_automatic_redirect(self):
        report = {"counts": {"requests": 0, "network_errors": 0, "robots_denied": 0, "errors": 0}, "errors": []}
        session = FakeSession()
        with patch.object(sina.requests, "Session", return_value=session), patch.object(sina.time, "monotonic", return_value=10.0), patch.object(sina.time, "sleep") as sleeper:
            client = sina._Client(report)
            client.get(sina.FEED_URL + "?page=1")
            client.close()
        sleeper.assert_called_with(1.0)
        self.assertTrue(all(not kwargs["allow_redirects"] for _, kwargs in session.calls))


class SinaParserTests(unittest.TestCase):
    def test_flash_direct_text_and_promotions(self):
        self.assertEqual(CONTENT, parse_article(html(direct=True), article_url(1))["content"])
        self.assertEqual(CONTENT, parse_article(html(), article_url(1))["content"])

    def test_summary_is_never_used_for_full_text(self):
        page = html().replace(f"<p>{CONTENT}</p>", "").replace("</head>", f'<meta name="description" content="{CONTENT}"></head>')
        with self.assertRaisesRegex(ValueError, "full text"):
            parse_article(page, article_url(1))

    def test_visible_date_with_source_is_parsed(self):
        page = html(None).replace("<body>", "<body><span class='date'>2026年09月27日 20:00 新浪财经</span>")
        self.assertEqual("2026-09-27T20:00:00+08:00", parse_article(page, article_url(1))["publish_time"])

    def test_missing_time_rejected(self):
        with self.assertRaisesRegex(ValueError, "publication time missing"):
            parse_article(html(None), article_url(1))


if __name__ == "__main__":
    unittest.main()
