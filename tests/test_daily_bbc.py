from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from crawler import bbc
from crawler.bbc_impl.article import ArticleExtractionError, parse_article
from crawler.bbc_impl.fulltext import ArticleClient, AccessDenied


START = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
END = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
URLS = [f"https://www.bbc.com/news/articles/c12345678{i}" for i in range(30)]
BODY = "This is visible reporting about the current event. " * 8


def feed(urls):
    return ("<rss><channel>" + "".join(
        f"<item><title>Story {i}</title><link>{url}</link>"
        "<pubDate>Sun, 27 Sep 2026 10:00:00 GMT</pubDate>"
        "<description>RSS summary must never be used</description></item>"
        for i, url in enumerate(urls)) + "</channel></rss>").encode()


def metadata(date="2026-09-26T17:00:00+08:00"):
    data = {"@type": "NewsArticle", "datePublished": date}
    if date is None:
        del data["datePublished"]
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


class BBCDailyTests(unittest.TestCase):
    def run_collection(self, urls=URLS[:3], *, limit=30, known=None,
                       dates=None, errors=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page_root = root / "article_pages"
            page_root.mkdir()
            calls = []

            def fetch(url):
                calls.append(url)
                if errors and url in errors:
                    raise errors[url]
                digest = hashlib.sha256(url.encode()).hexdigest()
                date = dates.get(url, "2026-09-26T17:00:00+08:00") if dates else "2026-09-26T17:00:00+08:00"
                (page_root / (digest + ".html")).write_text(metadata(date), encoding="utf-8")
                return BODY, "downloaded", "2026-09-27 19:00:00"

            report = {}
            with patch.object(bbc, "download_feed", return_value=feed(urls)) as download, \
                    patch.object(bbc, "ArticleClient") as client:
                client.return_value.fetch.side_effect = fetch
                result = list(bbc.collect(start=START, end=END, known_urls=known or set(),
                                          limit=limit, workdir=root, report=report))
                downloads = download.call_count
            return result, report, calls, downloads

    def test_zero_limit_does_not_request_feed_or_articles(self):
        for limit in (0, -1):
            rows, report, calls, downloads = self.run_collection(limit=limit)
            self.assertEqual((rows, calls, downloads), ([], [], 0))
            self.assertEqual(report["status"], "complete")

    def test_limit_and_known_urls_apply_before_body_fetch(self):
        rows, report, calls, _ = self.run_collection(limit=1, known={URLS[0]})
        self.assertEqual(calls, [URLS[1]])
        self.assertEqual(report["counts"]["known"], 1)
        self.assertEqual(report["counts"]["success"], 1)
        self.assertEqual(rows[0]["article_id"], hashlib.sha256(URLS[1].encode()).hexdigest()[:32])
        self.assertEqual(set(rows[0]), {"article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"})
        self.assertEqual(rows[0]["content"], BODY)
        self.assertEqual(rows[0]["publish_time"], "2026-09-26T09:00:00+00:00")
        self.assertEqual(rows[0]["crawl_time"], "2026-09-27T11:00:00+00:00")

    def test_rss_update_timestamp_cannot_replace_original_date(self):
        rows, report, _, _ = self.run_collection(
            dates={URLS[0]: "2020-01-01T00:00:00Z", URLS[1]: None})
        self.assertEqual([row["url"] for row in rows], [URLS[2]])
        self.assertEqual(report["counts"]["outside_window"], 1)
        self.assertEqual(report["counts"]["failed"], 1)
        self.assertEqual(report["status"], "partial")

    def test_naive_original_date_is_not_accepted(self):
        rows, report, _, _ = self.run_collection(URLS[:1], dates={URLS[0]: "2026-09-26T12:00:00"})
        self.assertEqual(rows, [])
        self.assertEqual(report["status"], "failed")

    def test_access_denial_stops_immediately_and_keeps_success(self):
        rows, report, calls, _ = self.run_collection(errors={URLS[1]: AccessDenied("HTTP 429")})
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls, URLS[:2])
        self.assertTrue(report["stopped"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["counts"]["deferred"], 1)

    def test_three_consecutive_network_failures_stop_source(self):
        rows, report, calls, _ = self.run_collection(URLS[:5], errors={
            url: urllib.error.URLError("Network unavailable") for url in URLS[:3]})
        self.assertEqual(rows, [])
        self.assertEqual(calls, URLS[:3])
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["counts"]["deferred"], 2)

    def test_success_resets_network_failure_streak(self):
        failed = [URLS[0], URLS[1], URLS[3], URLS[4]]
        rows, report, calls, _ = self.run_collection(URLS[:6], errors={
            url: urllib.error.URLError("Network unavailable") for url in failed})
        self.assertEqual(len(rows), 2)
        self.assertEqual(calls, URLS[:6])
        self.assertFalse(report["stopped"])
        self.assertEqual(report["status"], "partial")

    def test_video_links_and_video_redirects_are_skipped(self):
        video = "https://www.bbc.com/news/videos/c1234567890"
        rows, report, calls, _ = self.run_collection([video, *URLS[:2]], errors={
            URLS[0]: ArticleExtractionError("live/video page is not a full text article")})
        self.assertEqual(calls, URLS[:2])
        self.assertEqual(len(rows), 1)
        self.assertEqual(report["counts"]["skipped"], 2)
        self.assertEqual(report["counts"]["failed"], 0)
        self.assertEqual(report["status"], "complete")

    def test_reports_bound_errors_and_never_expose_local_paths(self):
        _, report, _, _ = self.run_collection(URLS, errors={
            url: ValueError(r"C:\Users\someone\secret.txt") for url in URLS})
        self.assertEqual(len(report["errors"]), 20)
        self.assertEqual(report["counts"]["failed"], 30)
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn("Users", json.dumps(report))

    def test_malformed_feed_sets_source_failure(self):
        report = {}
        with patch.object(bbc, "download_feed", return_value=b"<rss>"), \
                patch.object(bbc, "ArticleClient") as client:
            self.assertEqual(list(bbc.collect(start=START, end=END, known_urls=set(),
                limit=1, workdir=Path("unused"), report=report)), [])
            client.assert_not_called()
        self.assertEqual(report["status"], "failed")


class ExistingBBCSafetyTests(unittest.TestCase):
    def test_parser_reads_visible_body_and_rejects_summary_only(self):
        html = f'<article><div data-component="text-block"><p>{BODY}</p><p>{BODY}</p></div></article>'
        self.assertEqual(parse_article(html), BODY.strip() + "\n\n" + BODY.strip())
        with self.assertRaises(ArticleExtractionError):
            parse_article('<article><p>A short summary.</p></article>')

    def test_client_does_not_retry_http_429(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ArticleClient(directory)
            error = urllib.error.HTTPError(URLS[0], 429, "Too many requests", {}, None)
            with patch.object(client, "check_robots"), patch.object(client, "_open", side_effect=error) as request:
                with self.assertRaises(AccessDenied):
                    client.fetch(URLS[0])
                self.assertEqual(request.call_count, 1)

    def test_client_obeys_robots_disallow_without_article_request(self):
        import urllib.robotparser
        client = ArticleClient("unused")
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(["User-agent: *", "Disallow: /news/"])
        client.robots["https://www.bbc.com"] = parser
        with patch.object(client, "_open") as request:
            with self.assertRaises(AccessDenied):
                client.fetch(URLS[0])
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
