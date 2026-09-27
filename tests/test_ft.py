"""No real FT requests or credentials; source contracts and CookieJar regression."""
from datetime import datetime, timedelta, timezone
from email.message import Message
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from crawler import ft
from crawler.ft_impl.parsing import canonical_url, parse_article
from crawler.ft_impl.session import FTSession, StopCollection, effective_cookies

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
URLS = [f"https://www.ft.com/content/00000000-0000-0000-0000-{i:012d}" for i in range(6)]
PROSE = "This synthetic article describes a fictional economic event for a parser fixture. " * 8


def article(date="2026-09-27T09:00:00Z", extra="", body=None):
    metadata = {"@type": "NewsArticle", "datePublished": date}
    return ('<html><head><script type="application/ld+json">' + json.dumps(metadata)
            + '</script></head><body><h1>Synthetic fixture</h1>' + extra
            + '<div class="article__content-body">'
            + (body if body is not None else f'<p>{PROSE}</p><p>{PROSE}</p>')
            + '</div></body></html>')


def feed(urls):
    return '<rss><channel>' + ''.join('<item><title>Fixture</title><link>' + u
                                      + '</link></item>' for u in urls) + '</channel></rss>'


class FakeSession:
    def __init__(self, responses, logged_in=True):
        self.responses, self.logged_in, self.calls = responses, logged_in, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def has_login_cookie(self):
        return self.logged_in

    def fetch(self, url):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


class RedirectAdapter(requests.adapters.BaseAdapter):
    def __init__(self, status=301, destination=None):
        self.calls = []
        self.status = status
        self.destination = destination or URLS[1]

    def send(self, request, **kwargs):
        self.calls.append(request)
        response = requests.Response()
        response.status_code = self.status if len(self.calls) == 1 else 200
        response.url, response.request = request.url, request
        response._content = b"response fixture"
        response._content_consumed = True
        headers = Message()
        if len(self.calls) == 1 and self.status == 301:
            response.headers['Location'] = self.destination
            cookie = "FTSession_s=rotated-fixture; Domain=.ft.com; Path=/; Secure"
            response.headers['Set-Cookie'] = cookie
            headers.add_header('Set-Cookie', cookie)
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
        return response

    def close(self):
        pass


class TestFTSession(unittest.TestCase):
    def test_cookie_whitelist_and_placeholders(self):
        parsed = effective_cookies('FTSession_s=fixture; tracking=private; FTConsent=yes; FTSession=YOUR_KEY_HERE')
        self.assertEqual(parsed, {'FTSession_s': 'fixture', 'FTConsent': 'yes'})

    def test_301_retains_and_rotates_cookiejar(self):
        with FTSession('FTSession_s=initial-fixture') as session:
            adapter = RedirectAdapter()
            session.mount('https://', adapter)
            with patch('crawler.ft_impl.session.time.sleep'):
                session.get(URLS[0])
            self.assertIn('FTSession_s=initial-fixture', adapter.calls[0].headers['Cookie'])
            self.assertIn('FTSession_s=rotated-fixture', adapter.calls[1].headers['Cookie'])
            self.assertNotIn('Cookie', session.headers)

    def test_external_redirect_never_receives_credentials(self):
        with FTSession('FTSession_s=fixture') as session:
            adapter = RedirectAdapter(destination='https://example.org/content/leak')
            session.mount('https://', adapter)
            with self.assertRaises(StopCollection) as raised:
                session.get(URLS[0])
            self.assertEqual(raised.exception.reason, 'unsafe_destination')
            self.assertEqual(len(adapter.calls), 1)

    def test_restricted_status_stops_without_retry(self):
        for status, reason in [(401, 'auth_expired'), (403, 'access_denied'), (429, 'rate_limited')]:
            with self.subTest(status=status), FTSession('FTSession_s=fixture') as session:
                adapter = RedirectAdapter(status=status)
                session.mount('https://', adapter)
                with self.assertRaises(StopCollection) as raised:
                    session.get(URLS[0])
                self.assertEqual(raised.exception.reason, reason)
                self.assertEqual(len(adapter.calls), 1)

    def test_robots_disallow_prevents_article_request(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b'User-agent: *\nDisallow: /content/'
        response._content_consumed = True
        with FTSession('FTSession_s=fixture') as session, patch.object(session, 'get', return_value=response) as get:
            with self.assertRaises(StopCollection) as raised:
                session.fetch(URLS[0])
            self.assertEqual(raised.exception.reason, 'robots_disallowed')
            self.assertEqual(get.call_count, 1)

    def test_minimum_request_delay(self):
        with FTSession('FTSession_s=fixture', delay=0) as session:
            self.assertEqual(session.delay, 1)

    def test_robots_disallowed_redirect_is_not_requested(self):
        from urllib.robotparser import RobotFileParser
        with FTSession('FTSession_s=fixture') as session:
            parser = RobotFileParser()
            parser.parse(['User-agent: *', 'Disallow: /content/' + URLS[1].rsplit('/', 1)[1]])
            session._robots = parser
            adapter = RedirectAdapter()
            session.mount('https://', adapter)
            with self.assertRaises(StopCollection) as raised:
                session.get(URLS[0])
            self.assertEqual(raised.exception.reason, 'robots_disallowed')
            self.assertEqual(len(adapter.calls), 1)


class TestFTParser(unittest.TestCase):
    def test_canonical_url_and_uuid(self):
        self.assertEqual(canonical_url(URLS[0] + '?utm_source=feed#section'), URLS[0])
        with self.assertRaises(ValueError):
            canonical_url('https://www.ft.com/content/not-an-article')

    def test_fulltext_and_date_published(self):
        parsed = parse_article(article(), URLS[0])
        self.assertEqual(parsed['published'], datetime(2026, 9, 27, 9, tzinfo=timezone.utc))
        self.assertGreater(len(parsed['content']), 600)

    def test_naive_and_missing_dates_rejected(self):
        for date in ['2026-09-27T09:00:00', None]:
            with self.subTest(date=date), self.assertRaises(ValueError):
                parse_article(article(date=date), URLS[0])

    def test_updated_date_does_not_substitute_published(self):
        html = article().replace('datePublished', 'dateModified')
        with self.assertRaises(ValueError):
            parse_article(html, URLS[0])

    def test_explicit_published_time_fallback(self):
        html = article().replace('datePublished', 'dateModified')
        html += '<time itemprop="datePublished" datetime="2026-09-27T07:00:00Z"></time>'
        self.assertEqual(parse_article(html, URLS[0])['published'].hour, 7)

    def test_promotion_and_teaser_barrier_rejected(self):
        cases = [article(body='<p>Subscribe to unlock</p><p>' + PROSE + '</p>'),
                 article(extra='<div class="barrier">Subscribe for full access</div>')]
        for html in cases:
            with self.assertRaises(StopCollection) as raised:
                parse_article(html, URLS[0])
            self.assertEqual(raised.exception.reason, 'login_or_subscription_required')

    def test_rss_summary_or_short_teaser_is_not_fulltext(self):
        with self.assertRaises(ValueError):
            parse_article(article(body='<p>A short summary.</p>'), URLS[0])


class TestFTCollector(unittest.TestCase):
    def collect(self, session, limit=2, known=None, feeds=None):
        report = {}
        with patch.object(ft, 'FTSession', return_value=session), patch.object(ft, 'RSS_FEEDS', feeds or ('feed',)):
            rows = list(ft.collect(start=NOW - timedelta(hours=48), end=NOW,
                                   known_urls=known or set(), limit=limit,
                                   workdir=Path('.'), report=report))
        return rows, report

    def test_no_credential_fails_without_network(self):
        session = FakeSession({}, logged_in=False)
        rows, report = self.collect(session)
        self.assertEqual(rows, [])
        self.assertEqual(session.calls, [])
        self.assertEqual((report['status'], report['reason']), ('failed', 'auth_required'))

    def test_limit_and_known_urls_skip_article_requests(self):
        session = FakeSession({'feed': feed(URLS[:4]), **{u: article() for u in URLS[:4]}})
        rows, report = self.collect(session, limit=1, known={URLS[0]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['url'], URLS[1])
        self.assertEqual(session.calls, ['feed', URLS[1]])
        self.assertEqual(report['counts']['known'], 1)
        self.assertEqual(len(rows[0]), 8)

    def test_old_article_skipped_on_original_publication(self):
        session = FakeSession({'feed': feed(URLS[:2]), URLS[0]: article('2026-09-01T09:00:00Z'), URLS[1]: article()})
        rows, report = self.collect(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['counts']['outside_window'], 1)

    def test_zero_limit_no_network(self):
        session = FakeSession({})
        rows, report = self.collect(session, limit=0)
        self.assertEqual(rows, [])
        self.assertEqual(session.calls, [])

    def test_three_network_failures_stop_and_redact_exception(self):
        responses = {'feed': feed(URLS[:4]), **{u: requests.ConnectionError('secret-exception-must-not-appear') for u in URLS[:4]}}
        session = FakeSession(responses)
        rows, report = self.collect(session)
        self.assertEqual(rows, [])
        self.assertEqual(session.calls, ['feed'] + URLS[:3])
        self.assertTrue(report['stopped'])
        self.assertNotIn('secret-exception', json.dumps(report))

    def test_source_partial_retains_prior_success(self):
        session = FakeSession({'feed': feed(URLS[:2]), URLS[0]: article(),
                               URLS[1]: StopCollection('login_or_subscription_required')})
        rows, report = self.collect(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(report['reason'], 'login_or_subscription_required')

    def test_thirteen_feeds_maximum(self):
        self.assertEqual(len(ft.RSS_FEEDS), 13)

    def test_rss_dates_only_prioritize_and_do_not_substitute_article_date(self):
        xml = ('<rss><channel><item><link>' + URLS[0]
               + '</link><pubDate>Sun, 27 Sep 2026 08:00:00 GMT</pubDate></item><item><link>'
               + URLS[1] + '</link><pubDate>Sun, 27 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>')
        session = FakeSession({'feed': xml, URLS[0]: article(), URLS[1]: article('2026-09-01T09:00:00Z')})
        rows, report = self.collect(session, limit=1)
        self.assertEqual(session.calls, ['feed', URLS[1], URLS[0]])
        self.assertEqual(rows[0]['url'], URLS[0])
        self.assertEqual(report['counts']['outside_window'], 1)


if __name__ == '__main__':
    unittest.main()
