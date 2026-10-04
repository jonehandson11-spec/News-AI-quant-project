"""Bounded access probes and article restrictions; no network or real credentials."""
from datetime import timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests

from crawler import ft
from crawler.ft_impl.session import StopCollection
from scripts.ft_local import _report
from test_ft import NOW, URLS, FakeSession, article, feed

BARRIER = '<html><h1>Subscribe to unlock</h1><p>Discover all the plans</p></html>'
PROBES = [f'https://www.ft.com/content/00000000-0000-0000-0000-{i:012d}' for i in (100, 101, 102)]


class Session(FakeSession):
    closed = False

    def __exit__(self, *args):
        self.closed = True


class TestAccessProbes(unittest.TestCase):
    def run_collection(self, session, *, probes=(), limit=10, close_after_first=False, cache=None):
        report = {}
        with patch.object(ft, 'FTSession', return_value=session) as factory, \
                patch.object(ft, 'RSS_FEEDS', ('feed',)):
            gen = ft.collect(start=NOW - timedelta(hours=48), end=NOW,
                             known_urls=set(), limit=limit, workdir=Path('.'), report=report,
                             access_probe_urls=probes, session_cache=cache)
            if close_after_first:
                rows = [next(gen)]
                gen.close()
            else:
                rows = list(gen)
        self.assertTrue(session.closed)
        self.assertEqual(factory.call_count, 1)
        return rows, report

    def test_two_subscription_probes_stop_before_discovery(self):
        session = Session({url: BARRIER for url in PROBES})
        rows, report = self.run_collection(session, probes=PROBES)
        self.assertEqual(rows, [])
        self.assertEqual(session.calls, PROBES[:2])
        self.assertTrue(report['stopped'])
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['reason'], 'login_or_subscription_required')
        self.assertEqual(report['counts']['access_probe_checks'], 2)
        self.assertEqual(report['counts']['access_probe_successes'], 0)
        self.assertEqual(report['counts']['discovered'], 0)
        self.assertEqual([error['url'] for error in report['errors']], PROBES[:2])

    def test_second_readable_probe_allows_discovery_and_is_never_collected(self):
        session = Session({PROBES[0]: BARRIER, PROBES[1]: article('2026-08-01T09:00:00Z'),
                           'feed': feed([*PROBES[:2], URLS[0]]), URLS[0]: article()})
        rows, report = self.run_collection(session, probes=PROBES[:2])
        self.assertEqual([row['url'] for row in rows], [URLS[0]])
        self.assertEqual(session.calls, [*PROBES[:2], 'feed', URLS[0]])
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['counts']['success'], 1)
        self.assertEqual(report['counts']['outside_window'], 0)
        self.assertEqual(report['counts']['access_probe_checks'], 2)
        self.assertEqual(report['counts']['access_probe_successes'], 1)

    def test_first_readable_probe_does_not_request_second(self):
        session = Session({PROBES[0]: article(), 'feed': feed([])})
        rows, report = self.run_collection(session, probes=PROBES[:2])
        self.assertEqual(rows, [])
        self.assertEqual(session.calls, [PROBES[0], 'feed'])
        self.assertEqual(report['counts']['access_probe_checks'], 1)
        self.assertEqual(report['counts']['success'], 0)

    def test_duplicate_probes_are_not_requested_twice(self):
        session = Session({url: BARRIER for url in PROBES})
        self.run_collection(session, probes=[PROBES[0], PROBES[0], PROBES[1]])
        self.assertEqual(session.calls, PROBES[:2])

    def test_invalid_probe_hints_are_not_requested_or_allowed_to_block_valid_probe(self):
        session = Session({PROBES[0]: article(), 'feed': feed([])})
        probes = ['https://example.org/private', 'https://www.ft.com/login', None,
                  'https://www.ft.com/content/not-a-uuid', PROBES[0]]
        _, report = self.run_collection(session, probes=probes)
        self.assertEqual(session.calls, [PROBES[0], 'feed'])
        self.assertEqual(report['counts']['access_probe_checks'], 1)

    def test_missing_cookie_prevents_probe_and_discovery(self):
        session = Session({}, logged_in=False)
        _, report = self.run_collection(session, probes=PROBES[:2])
        self.assertEqual(session.calls, [])
        self.assertEqual(report['reason'], 'auth_required')

    def test_hard_probe_stops_do_not_request_another_probe_or_feed(self):
        for reason in ('auth_expired', 'auth_required', 'access_denied', 'rate_limited',
                       'robots_unavailable', 'robots_disallowed', 'unsafe_destination'):
            with self.subTest(reason=reason):
                session = Session({PROBES[0]: StopCollection(reason, retry_after_seconds=7200,
                                                             endpoint_kind='article')})
                rows, report = self.run_collection(session, probes=PROBES[:2])
                self.assertEqual(rows, [])
                self.assertEqual(session.calls, [PROBES[0]])
                self.assertTrue(report['stopped'])
                self.assertEqual(report['reason'], reason)
                if reason == 'rate_limited':
                    self.assertEqual(report['retry_after_seconds'], 7200)
                    self.assertEqual(report['endpoint_kind'], 'article')

    def test_inconclusive_probes_do_not_claim_authentication_failure(self):
        for response, reason in (
            (requests.ConnectionError('private detail'), 'article_request_failed'),
            (article(body='<p>Short teaser.</p>'), 'invalid_or_incomplete_article'),
        ):
            with self.subTest(reason=reason):
                session = Session({PROBES[0]: response, PROBES[1]: BARRIER})
                _, report = self.run_collection(session, probes=PROBES[:2])
                self.assertEqual(session.calls, PROBES[:2])
                self.assertEqual(report['reason'], reason)
                self.assertTrue(report['stopped'])
                self.assertNotIn('private detail', str(report))

    def test_without_access_evidence_first_barrier_stops_without_refetch(self):
        session = Session({'feed': feed(URLS[:2]), URLS[0]: BARRIER})
        rows, report = self.run_collection(session)
        self.assertEqual(rows, [])
        self.assertEqual(session.calls, ['feed', URLS[0]])
        self.assertEqual(report['reason'], 'login_or_subscription_required')
        self.assertTrue(report['stopped'])

    def test_readable_probe_allows_one_article_restriction_and_later_collection(self):
        session = Session({PROBES[0]: article(), 'feed': feed(URLS[:2]),
                           URLS[0]: BARRIER, URLS[1]: article()})
        rows, report = self.run_collection(session, probes=PROBES[:1])
        self.assertEqual([row['url'] for row in rows], [URLS[1]])
        self.assertEqual(session.calls, [PROBES[0], 'feed', *URLS[:2]])
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['stopped'])
        self.assertEqual(report['reason'], 'article_access_unavailable')
        self.assertEqual(report['errors'], [{'url': URLS[0], 'reason': 'article_access_unavailable'}])
        cleaned = _report(report)
        self.assertEqual(cleaned['counts']['article_access_unavailable'], 1)
        self.assertEqual(cleaned['counts']['access_probe_checks'], 1)
        self.assertEqual(cleaned['counts']['access_probe_successes'], 1)
        self.assertEqual(cleaned['errors'], report['errors'])

    def test_prior_collected_article_also_establishes_access_without_probe(self):
        session = Session({'feed': feed(URLS[:3]), URLS[0]: article(),
                           URLS[1]: BARRIER, URLS[2]: article()})
        rows, report = self.run_collection(session)
        self.assertEqual([row['url'] for row in rows], [URLS[0], URLS[2]])
        self.assertEqual(report['status'], 'partial')
        self.assertFalse(report['stopped'])

    def test_two_consecutive_restrictions_stop_remaining_articles(self):
        session = Session({'feed': feed(URLS[:4]), URLS[0]: article(),
                           URLS[1]: BARRIER, URLS[2]: BARRIER})
        rows, report = self.run_collection(session)
        self.assertEqual(len(rows), 1)
        self.assertEqual(session.calls, ['feed', *URLS[:3]])
        self.assertEqual(report['counts']['article_access_unavailable'], 2)
        self.assertEqual(report['counts']['failed'], 2)
        self.assertEqual(report['counts']['deferred'], 1)
        self.assertEqual(report['reason'], 'article_access_unavailable')
        self.assertTrue(report['stopped'])

    def test_readable_articles_reset_consecutive_but_not_total_restriction_limit(self):
        session = Session({PROBES[0]: article(), 'feed': feed(URLS),
                           **{url: BARRIER if i % 2 == 0 else article() for i, url in enumerate(URLS)}})
        rows, report = self.run_collection(session, probes=PROBES[:1])
        self.assertEqual([row['url'] for row in rows], [URLS[1], URLS[3]])
        self.assertEqual(session.calls, [PROBES[0], 'feed', *URLS[:5]])
        self.assertEqual(report['counts']['article_access_unavailable'], 3)
        self.assertEqual(report['counts']['deferred'], 1)
        self.assertTrue(report['stopped'])

    def test_incomplete_body_does_not_reset_consecutive_restrictions(self):
        session = Session({PROBES[0]: article(), 'feed': feed(URLS[:4]),
                           URLS[0]: BARRIER, URLS[1]: article(body='<p>Short.</p>'), URLS[2]: BARRIER})
        _, report = self.run_collection(session, probes=PROBES[:1])
        self.assertEqual(session.calls, [PROBES[0], 'feed', *URLS[:3]])
        self.assertTrue(report['stopped'])

    def test_successful_probe_does_not_relax_http_or_robots_stop(self):
        for reason in ('auth_expired', 'auth_required', 'access_denied', 'rate_limited',
                       'robots_unavailable', 'robots_disallowed', 'login_or_subscription_required'):
            with self.subTest(reason=reason):
                session = Session({PROBES[0]: article(), 'feed': feed(URLS[:2]),
                                   URLS[0]: StopCollection(reason)})
                rows, report = self.run_collection(session, probes=PROBES[:1])
                self.assertEqual(rows, [])
                self.assertEqual(session.calls, [PROBES[0], 'feed', URLS[0]])
                self.assertEqual(report['reason'], reason)
                self.assertTrue(report['stopped'])
                self.assertEqual(report['counts']['article_access_unavailable'], 0)

    def test_batch_writer_close_releases_the_only_session(self):
        session = Session({PROBES[0]: article(), 'feed': feed(URLS[:2]), URLS[0]: article()})
        rows, report = self.run_collection(session, probes=PROBES[:1], limit=1, close_after_first=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(report['counts']['deferred'], 1)

    def test_cache_restore_precedes_requests_and_only_fulltext_is_saved(self):
        session = Session({PROBES[0]: article(), 'feed': feed(URLS[:3]),
                           URLS[0]: BARRIER, URLS[1]: article(body='<p>Teaser.</p>'), URLS[2]: article()})
        cache = Mock()
        cache.restore.side_effect = lambda restored: self.assertEqual(restored.calls, [])
        rows, _ = self.run_collection(session, probes=PROBES[:1], cache=cache)
        self.assertEqual([row['url'] for row in rows], [URLS[2]])
        cache.restore.assert_called_once_with(session)
        self.assertEqual(cache.save.call_count, 2)
        self.assertTrue(all(call.args == (session,) for call in cache.save.call_args_list))

    def test_failed_probe_never_overwrites_cached_session(self):
        session = Session({url: BARRIER for url in PROBES})
        cache = Mock()
        self.run_collection(session, probes=PROBES[:2], cache=cache)
        cache.restore.assert_called_once_with(session)
        cache.save.assert_not_called()

    def test_cache_failure_does_not_change_collection_or_disclose_details(self):
        for result in (False, RuntimeError('private cache path')):
            with self.subTest(result=type(result).__name__):
                cache = Mock()
                if isinstance(result, Exception):
                    cache.restore.side_effect = cache.save.side_effect = result
                else:
                    cache.restore.return_value = cache.save.return_value = result
                session = Session({'feed': feed(URLS[:1]), URLS[0]: article()})
                rows, report = self.run_collection(session, cache=cache)
                self.assertEqual(len(rows), 1)
                self.assertEqual(report['status'], 'complete')
                self.assertNotIn('private cache path', str(report))


if __name__ == '__main__':
    unittest.main()
