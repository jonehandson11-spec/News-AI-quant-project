"""Offline checks for bounded FT backfill discovery; never use real credentials."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import requests

from crawler import ft
from crawler.ft_impl.parsing import parse_category_page
from crawler.ft_impl.session import StopCollection


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
CATEGORY = "https://www.ft.com/world"
OTHER = "https://www.ft.com/markets"
URLS = [f"https://www.ft.com/content/00000000-0000-0000-0000-{i:012d}" for i in range(8)]


def page(urls, next_href=None):
    stream = '<ul class="js-stream-list">' + ''.join(
        '<li><div class="stream-item"><a class="js-teaser-heading-link" href="'
        + url + '">Fixture</a></div></li>' for url in urls) + '</ul>'
    return stream + (f'<a data-trackable="next-page" href="{next_href}">Next page</a>'
                     if next_href else '')


def article(date):
    prose = "Synthetic full-text article fixture for bounded historical discovery. " * 12
    return ('<h1>Fixture</h1><script type="application/ld+json">'
            + json.dumps({"@type": "NewsArticle", "datePublished": date})
            + f'</script><div class="article__content-body"><p>{prose}</p><p>{prose}</p></div>')


class Session:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def has_login_cookie(self):
        return True

    def fetch(self, url):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class CategoryParserTests(unittest.TestCase):
    def test_only_stream_links_are_discovered_and_canonicalized(self):
        html = page([URLS[0] + '?tracking=1', URLS[0]], '?page=2')
        html += f'<a class="js-teaser-heading-link" href="{URLS[1]}">Recommended</a>'
        items, next_url = parse_category_page(html, CATEGORY + '?page=1')
        self.assertEqual([item['url'] for item in items], URLS[:1])
        self.assertEqual(next_url, CATEGORY + '?page=2')

    def test_next_link_must_be_consecutive_and_same_category(self):
        for destination in ('https://example.org/world?page=2', '/markets?page=2',
                            '?page=1', '?page=3', '?page=2&access=anything', '?page=2&page=2'):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                parse_category_page(page(URLS[:1], destination), CATEGORY + '?page=1')

    def test_missing_stream_is_not_silent_success(self):
        with self.assertRaises(ValueError):
            parse_category_page('<h1>Unexpected page</h1>', CATEGORY + '?page=1')

    def test_subscription_barrier_stops_discovery(self):
        with self.assertRaises(StopCollection):
            parse_category_page('<div class="barrier">Sign in to continue</div>', CATEGORY)


class BackfillDiscoveryTests(unittest.TestCase):
    def collect(self, session, hours=120, categories=(CATEGORY,), total_limit=40, category_limit=5):
        report = {}
        with patch.object(ft, 'FTSession', return_value=session), \
                patch.object(ft, 'RSS_FEEDS', ('rss',)), \
                patch.object(ft, 'CATEGORY_PAGES', categories), \
                patch.object(ft, 'DISCOVERY_PAGE_LIMIT', total_limit), \
                patch.object(ft, 'CATEGORY_PAGE_LIMIT', category_limit):
            rows = list(ft.collect(start=NOW - timedelta(hours=hours), end=NOW,
                                   known_urls=set(), limit=20, workdir=Path('.'), report=report))
        return rows, report

    def test_default_48_hours_uses_only_rss(self):
        session = Session({'rss': '<rss><channel/></rss>'})
        rows, report = self.collect(session, hours=48)
        self.assertEqual((rows, session.calls), ([], ['rss']))
        self.assertEqual(report['discovery']['mode'], 'rss')

    def test_backfill_follows_next_and_checks_original_publication(self):
        session = Session({
            'rss': '<rss><channel/></rss>',
            CATEGORY + '?page=1': page(URLS[:1], '?page=2'),
            CATEGORY + '?page=2': page(URLS[:2]),
            URLS[0]: article('2026-09-27T09:00:00Z'),
            URLS[1]: article('2026-09-20T09:00:00Z'),
        })
        rows, report = self.collect(session)
        self.assertEqual([row['url'] for row in rows], URLS[:1])
        self.assertEqual(report['counts']['outside_window'], 1)
        self.assertEqual(report['discovery']['pages_fetched'], 2)
        self.assertEqual(report['discovery']['categories_completed'], 1)
        self.assertTrue(report['discovery']['coverage_limited'])

    def test_total_budget_is_round_robin_across_categories(self):
        responses = {'rss': '<rss><channel/></rss>'}
        for category in (CATEGORY, OTHER):
            responses[category + '?page=1'] = page(URLS[:1], '?page=2')
            responses[category + '?page=2'] = page(URLS[1:2], '?page=3')
        responses.update({url: article('2026-09-27T09:00:00Z') for url in URLS[:2]})
        session = Session(responses)
        _, report = self.collect(session, categories=(CATEGORY, OTHER), total_limit=3)
        self.assertEqual(session.calls[:4], ['rss', CATEGORY + '?page=1', OTHER + '?page=1',
                                             CATEGORY + '?page=2'])
        self.assertEqual(report['discovery']['pages_fetched'], 3)
        self.assertEqual([o['reason'] for o in report['discovery']['category_outcomes']],
                         ['total_page_limit', 'total_page_limit'])

    def test_per_category_limit_is_bounded(self):
        session = Session({'rss': '<rss><channel/></rss>',
                           CATEGORY + '?page=1': page(URLS[:1], '?page=2'),
                           URLS[0]: article('2026-09-27T09:00:00Z')})
        _, report = self.collect(session, category_limit=1)
        self.assertEqual(report['discovery']['category_outcomes'][0]['reason'], 'page_limit')
        self.assertNotIn(CATEGORY + '?page=2', session.calls)

    def test_repeated_page_stops_that_category(self):
        session = Session({'rss': '<rss><channel/></rss>',
                           CATEGORY + '?page=1': page(URLS[:1], '?page=2'),
                           CATEGORY + '?page=2': page(URLS[:1], '?page=3'),
                           URLS[0]: article('2026-09-27T09:00:00Z')})
        _, report = self.collect(session)
        self.assertEqual(report['discovery']['category_outcomes'][0]['reason'], 'no_progress')
        self.assertNotIn(CATEGORY + '?page=3', session.calls)

    def test_restrictions_stop_before_any_further_requests(self):
        for reason in ('auth_expired', 'access_denied', 'rate_limited', 'robots_disallowed',
                       'login_or_subscription_required'):
            with self.subTest(reason=reason):
                session = Session({'rss': '<rss><channel/></rss>',
                                   CATEGORY + '?page=1': StopCollection(reason)})
                rows, report = self.collect(session, categories=(CATEGORY, OTHER))
                self.assertEqual(rows, [])
                self.assertEqual(session.calls, ['rss', CATEGORY + '?page=1'])
                self.assertEqual(report['reason'], reason)
                self.assertTrue(report['stopped'])
                self.assertEqual([o['reason'] for o in report['discovery']['category_outcomes']],
                                 ['collection_stopped', 'collection_stopped'])

    def test_three_network_failures_stop_and_redact(self):
        categories = (CATEGORY, OTHER, 'https://www.ft.com/europe', 'https://www.ft.com/energy')
        session = Session({'rss': '<rss><channel/></rss>', **{
            url + '?page=1': requests.ConnectionError('SECRET-SHOULD-NOT-APPEAR')
            for url in categories}})
        rows, report = self.collect(session, categories=categories)
        self.assertEqual(rows, [])
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(report['reason'], 'consecutive_network_failures')
        self.assertNotIn('SECRET', json.dumps(report))
        self.assertEqual(report['discovery']['category_outcomes'][-1]['reason'],
                         'collection_stopped')


if __name__ == '__main__':
    unittest.main()
