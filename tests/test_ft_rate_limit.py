"""Offline FT rate-limit tests: synthetic cookies, mocked transport, no network."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from crawler import ft
from crawler.ft_impl.session import FTSession, StopCollection, retry_after_seconds
from scripts import ft_local


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
ARTICLE = "https://www.ft.com/content/00000000-0000-0000-0000-000000000001"
FEED = "https://www.ft.com/world?format=rss"
CATEGORY = "https://www.ft.com/world?page=1"


class Adapter(requests.adapters.BaseAdapter):
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def send(self, request, **kwargs):
        self.calls.append(request.url)
        status, headers, body = self.responses[len(self.calls) - 1]
        response = requests.Response()
        response.status_code = status
        response.headers.update(headers)
        response.url, response.request = request.url, request
        response._content = body.encode()
        response._content_consumed = True
        return response

    def close(self):
        pass


class RetryAfterTests(unittest.TestCase):
    def test_seconds_and_http_date_are_safe_integers(self):
        self.assertEqual(retry_after_seconds(' 120 '), 120)
        self.assertEqual(retry_after_seconds('Thu, 01 Oct 2026 12:03:00 GMT', now=NOW), 180)
        self.assertEqual(retry_after_seconds('Thu, 01 Oct 2026 12:03:00 GMT',
                                            now=NOW + timedelta(microseconds=1)), 180)
        self.assertEqual(retry_after_seconds('Thu, 01 Oct 2026 11:00:00 GMT', now=NOW), 0)

    def test_missing_or_invalid_headers_default_to_one_hour(self):
        for value in (None, '', '-1', '1.5', 'SECRET=fixture', 'Thu, 01 Oct 2026 12:03:00'):
            with self.subTest(value=value):
                self.assertEqual(retry_after_seconds(value, now=NOW), 3600)

    def test_unreasonably_large_seconds_remain_bounded(self):
        self.assertEqual(retry_after_seconds('9' * 5000), 2_147_483_647)


class SessionPacingTests(unittest.TestCase):
    def test_default_delay_is_ten_seconds(self):
        with patch.dict(os.environ, {}, clear=True), FTSession(raw_cookie='') as session:
            self.assertEqual(session.delay, 10)

    def test_configured_delay_never_drops_below_five_seconds(self):
        for configured, expected in [('0', 5), ('-100', 5), ('5.5', 5.5), ('12', 12),
                                     ('nan', 10), ('inf', 10), ('invalid', 10)]:
            with self.subTest(configured=configured), \
                    patch.dict(os.environ, {'FT_REQUEST_DELAY_SECONDS': configured}), \
                    FTSession(raw_cookie='') as session:
                self.assertEqual(session.delay, expected)
        with FTSession(raw_cookie='', delay=0) as session:
            self.assertEqual(session.delay, 5)

    def test_robots_can_increase_delay_further(self):
        adapter = Adapter([(200, {}, 'User-agent: *\nCrawl-delay: 25\nAllow: /'),
                           (200, {}, 'fixture')])
        with FTSession(raw_cookie='', delay=10) as session, \
                patch('crawler.ft_impl.session.time.sleep'):
            session.mount('https://', adapter)
            session.fetch(FEED)
            self.assertEqual(session.delay, 25)

    def test_requests_wait_for_the_remaining_configured_interval(self):
        adapter = Adapter([(200, {}, 'first'), (200, {}, 'second')])
        with FTSession(raw_cookie='', delay=10) as session, \
                patch('crawler.ft_impl.session.time.monotonic', side_effect=[100, 102, 110]), \
                patch('crawler.ft_impl.session.time.sleep') as sleep:
            session.mount('https://', adapter)
            session.get(FEED)
            session.get(CATEGORY)
            sleep.assert_called_once_with(8)

    def test_rate_limit_classifies_actual_endpoint_and_does_not_retry(self):
        for url, kind in [('https://www.ft.com/robots.txt', 'robots'), (FEED, 'feed'),
                          (CATEGORY, 'category'), (ARTICLE, 'article')]:
            with self.subTest(kind=kind), FTSession(raw_cookie='') as session:
                adapter = Adapter([(429, {'Retry-After': '180'}, 'sensitive body')])
                session.mount('https://', adapter)
                with self.assertRaises(StopCollection) as raised:
                    session.get(url)
                self.assertEqual(adapter.calls, [url])
                self.assertEqual(raised.exception.reason, 'rate_limited')
                self.assertEqual(raised.exception.retry_after_seconds, 180)
                self.assertEqual(raised.exception.endpoint_kind, kind)
                self.assertEqual(str(raised.exception), 'rate_limited')

    def test_robots_429_blocks_feed_and_article_requests(self):
        for requested in (FEED, ARTICLE):
            with self.subTest(requested=requested), FTSession(raw_cookie='') as session:
                adapter = Adapter([(429, {}, 'sensitive body')])
                session.mount('https://', adapter)
                with self.assertRaises(StopCollection) as raised:
                    session.fetch(requested)
                self.assertEqual(adapter.calls, ['https://www.ft.com/robots.txt'])
                self.assertEqual(raised.exception.endpoint_kind, 'robots')
                self.assertEqual(raised.exception.retry_after_seconds, 3600)

    def test_feed_429_after_robots_success_reports_feed(self):
        adapter = Adapter([(200, {}, 'User-agent: *\nAllow: /'), (429, {'Retry-After': '90'}, '')])
        with FTSession(raw_cookie='') as session, patch('crawler.ft_impl.session.time.sleep'):
            session.mount('https://', adapter)
            with self.assertRaises(StopCollection) as raised:
                session.fetch(FEED)
            self.assertEqual(adapter.calls, ['https://www.ft.com/robots.txt', FEED])
            self.assertEqual(raised.exception.endpoint_kind, 'feed')


class ReportTests(unittest.TestCase):
    def test_collector_propagates_rate_limit_at_each_stage(self):
        class Session:
            def __init__(self, failing_url, endpoint):
                self.failing_url, self.endpoint, self.calls = failing_url, endpoint, []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def has_login_cookie(self):
                return True

            def fetch(self, url):
                self.calls.append(url)
                if url == self.failing_url:
                    raise StopCollection('rate_limited', retry_after_seconds=700,
                                         endpoint_kind=self.endpoint)
                return f'<rss><channel><item><link>{ARTICLE}</link></item></channel></rss>'

        for failing_url, endpoint, hours in [(FEED, 'robots', 48), (FEED, 'feed', 48),
                                             (CATEGORY, 'category', 120), (ARTICLE, 'article', 48)]:
            report = {}
            session = Session(failing_url, endpoint)
            with self.subTest(endpoint=endpoint), patch.object(ft, 'FTSession', return_value=session), \
                    patch.object(ft, 'RSS_FEEDS', (FEED,)), \
                    patch.object(ft, 'CATEGORY_PAGES', ('https://www.ft.com/world',)):
                rows = list(ft.collect(start=NOW - timedelta(hours=hours), end=NOW,
                                       known_urls=set(), limit=20, workdir=Path('.'), report=report))
            self.assertEqual(rows, [])
            self.assertEqual(report['reason'], 'rate_limited')
            self.assertEqual(report['retry_after_seconds'], 700)
            self.assertEqual(report['endpoint_kind'], endpoint)
            self.assertEqual(report['status'], 'failed')
            self.assertTrue(report['stopped'])
            self.assertEqual(session.calls[-1], failing_url)

    def test_report_projection_excludes_raw_headers_urls_and_cookie(self):
        value = {'status': 'failed', 'reason': 'rate_limited', 'retry_after_seconds': 3600,
                 'endpoint_kind': 'robots', 'url': 'https://www.ft.com/?token=SECRET',
                 'headers': {'Retry-After': 'SECRET'}, 'cookie': 'SECRET'}
        safe = ft_local._report(value)
        self.assertEqual(safe['retry_after_seconds'], 3600)
        self.assertEqual(safe['endpoint_kind'], 'robots')
        self.assertNotIn('SECRET', json.dumps(safe))
        for retry, endpoint in [(True, 'SECRET'), (-1, {}), ('SECRET', []), (2_147_483_648, None)]:
            safe = ft_local._report(dict(value, retry_after_seconds=retry, endpoint_kind=endpoint))
            self.assertNotIn('retry_after_seconds', safe)
            self.assertNotIn('endpoint_kind', safe)
        safe = ft_local._report(dict(value, reason='auth_expired'))
        self.assertNotIn('retry_after_seconds', safe)
        self.assertNotIn('endpoint_kind', safe)

    def test_local_result_exposes_safe_cooldown_fields(self):
        def collector(**kwargs):
            kwargs['report'].update(status='failed', reason='rate_limited', stopped=True,
                                    retry_after_seconds=3600, endpoint_kind='robots')
            return iter(())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'repo'
            root.mkdir()
            credential = Path(temporary) / 'fixture-cookie.txt'
            credential.write_text('FTSession_s=synthetic-fixture', encoding='utf-8')
            manifest = {'publication_window': {'start': (NOW - timedelta(days=10)).isoformat()}}
            with patch.object(ft_local, 'validate', return_value={'total_articles': 0}), \
                    patch.object(ft_local, 'read_json', side_effect=[{'target_articles': 100}, manifest]), \
                    patch.object(ft_local.sqlite3, 'connect') as connection:
                connection.return_value.execute.return_value = []
                result = ft_local.collect_batch(root, credential, Path(temporary) / 'batch.json',
                                                now=NOW, collector=collector)
            self.assertEqual(result['reason'], 'rate_limited')
            self.assertEqual(result['retry_after_seconds'], 3600)
            self.assertEqual(result['endpoint_kind'], 'robots')
            self.assertNotIn('synthetic-fixture', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
