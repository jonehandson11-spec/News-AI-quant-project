"""Bounded credentialed rechecks; no network or real credentials."""
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import requests

from crawler import ft
from crawler.ft_impl.session import StopCollection
from scripts.ft_local import _report
from test_ft import NOW, URLS, FakeSession, article, feed

BARRIER = article(extra='<div class="barrier">Subscribe for full access</div>')


class Session(FakeSession):
    delay = 10
    closed = False

    def __exit__(self, *args):
        self.closed = True


class TestSessionRecheck(unittest.TestCase):
    def run_collection(self, old, new=None, limit=3, close_after_first=False):
        report = {}
        with patch.object(ft, 'FTSession', side_effect=[old, new]) as factory, \
                patch.object(ft, 'RSS_FEEDS', ('feed',)), patch.object(ft.time, 'sleep') as sleep:
            gen = ft.collect(start=NOW - timedelta(hours=48), end=NOW,
                             known_urls=set(), limit=limit, workdir=Path('.'), report=report)
            if close_after_first:
                rows = [next(gen)]
                gen.close()
            else:
                rows = list(gen)
        self.assertTrue(old.closed)
        if factory.call_count == 2:
            self.assertTrue(new.closed)
        return rows, report, factory.call_count, sleep

    def test_recovers_once_continues_with_replacement_and_keeps_delay(self):
        old = Session({'feed': feed(URLS[:2]), URLS[0]: BARRIER})
        old.delay = 65
        new = Session({URLS[0]: article(), URLS[1]: article()})
        rows, report, calls, sleep = self.run_collection(old, new)
        self.assertEqual([r['url'] for r in rows], URLS[:2])
        self.assertEqual(new.calls, URLS[:2])
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(65)
        self.assertEqual(new.delay, 65)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['counts']['failed'], 0)
        self.assertEqual(report['counts']['success'], 2)
        cleaned = _report(report)
        self.assertEqual(cleaned['counts']['session_rechecks'], 1)
        self.assertEqual(cleaned['counts']['session_recoveries'], 1)

    def test_later_barrier_has_no_second_recheck(self):
        old = Session({'feed': feed(URLS[:3]), URLS[0]: BARRIER})
        new = Session({URLS[0]: article(), URLS[1]: BARRIER})
        rows, report, calls, sleep = self.run_collection(old, new)
        self.assertEqual(len(rows), 1)
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(30.0)
        self.assertTrue(report['stopped'])
        self.assertEqual(report['reason'], 'login_or_subscription_required')
        self.assertEqual(report['counts']['deferred'], 1)

    def test_failed_recheck_stops_and_preserves_reason(self):
        cases = [
            (BARRIER, 'login_or_subscription_required'),
            (StopCollection('access_denied'), 'access_denied'),
            (StopCollection('rate_limited', retry_after_seconds=7200, endpoint_kind='article'), 'rate_limited'),
            (StopCollection('robots_disallowed'), 'robots_disallowed'),
            (StopCollection('unsafe_destination'), 'unsafe_destination'),
            (requests.ConnectionError('private diagnostic'), 'article_request_failed'),
            (article(body='<p>Only a teaser.</p>'), 'invalid_or_incomplete_article'),
        ]
        for response, reason in cases:
            with self.subTest(reason=reason):
                old = Session({'feed': feed(URLS[:2]), URLS[0]: BARRIER})
                new = Session({URLS[0]: response})
                rows, report, calls, _ = self.run_collection(old, new)
                self.assertEqual(rows, [])
                self.assertEqual(calls, 2)
                self.assertTrue(report['stopped'])
                self.assertEqual(report['reason'], reason)
                self.assertEqual(report['counts']['session_recoveries'], 0)
                self.assertNotIn('private diagnostic', str(report))
                if reason == 'rate_limited':
                    self.assertEqual(report['retry_after_seconds'], 7200)
                    self.assertEqual(report['endpoint_kind'], 'article')

    def test_http_and_robots_failure_never_triggers_recheck(self):
        for reason in ('auth_expired', 'auth_required', 'access_denied', 'rate_limited',
                       'robots_unavailable', 'robots_disallowed'):
            with self.subTest(reason=reason):
                old = Session({'feed': feed(URLS[:2]), URLS[0]: StopCollection(reason)})
                _, report, calls, sleep = self.run_collection(old)
                self.assertEqual(calls, 1)
                sleep.assert_not_called()
                self.assertEqual(report['reason'], reason)

    def test_initial_and_replacement_require_login_cookie(self):
        old = Session({}, logged_in=False)
        _, report, calls, sleep = self.run_collection(old)
        self.assertEqual(calls, 1)
        sleep.assert_not_called()
        self.assertEqual(report['reason'], 'auth_required')
        old = Session({'feed': feed(URLS[:2]), URLS[0]: BARRIER})
        new = Session({}, logged_in=False)
        _, report, calls, _ = self.run_collection(old, new)
        self.assertEqual(calls, 2)
        self.assertEqual(new.calls, [])
        self.assertEqual(report['reason'], 'auth_required')

    def test_batch_writer_close_releases_both_sessions(self):
        old = Session({'feed': feed(URLS[:2]), URLS[0]: BARRIER})
        new = Session({URLS[0]: article()})
        rows, report, calls, _ = self.run_collection(old, new, limit=1, close_after_first=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['counts']['deferred'], 1)
        self.assertEqual(calls, 2)


if __name__ == '__main__':
    unittest.main()
