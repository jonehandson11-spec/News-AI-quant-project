"""Synthetic responses only: no FT access, account state or genuine credentials."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from crawler.ft_impl.session import FTSession, StopCollection
from scripts.ft_local import _report
from scripts.ft_preflight import main, preflight
import test_ft as fixtures
from test_ft import article, FakeSession, feed, RedirectAdapter, URLS

SECRET = "SYNTHETIC-DO-NOT-EMIT"


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo'
        self.root.mkdir()
        self.cookie = Path(self.temp.name) / 'private.txt'
        self.cookie.write_text('FTSession_s=' + SECRET)

    def probe(self, html):
        session = FakeSession({URLS[0]: html})
        with patch('crawler.ft_impl.session.FTSession', return_value=session):
            result = preflight(self.cookie, URLS[0], root=self.root)
        self.assertEqual(session.calls, [URLS[0]])
        self.assertNotIn(SECRET, json.dumps(result))
        return result

    def test_response_categories_and_actions(self):
        cases = [
            (article(), 'ok', 'complete', 'none'),
            ('<div class="barrier">Sign in to continue</div>', 'login_prompt_detected', 'article_parse', 'refresh_session'),
            ('<div class="barrier">Subscribe for access or sign in</div>', 'subscription_barrier_detected', 'article_parse', 'check_subscription_access'),
            ('<p>Discover all the plans</p>', 'paywall_page_text_detected', 'article_parse', 'verify_browser_access_then_parser'),
            (article(body='<p>Subscribe to unlock</p>'), 'paywall_body_text_detected', 'article_parse', 'verify_browser_access_then_parser'),
            ('<div>Changed layout</div>', 'visible_article_body_missing', 'article_parse', 'check_parser'),
            (article(body='<p>Short teaser</p>'), 'incomplete_article_body', 'article_parse', 'check_parser'),
            (article(date=SECRET), 'invalid_publication_time', 'article_parse', 'check_parser'),
            (StopCollection('auth_expired'), 'auth_expired', 'article_fetch', 'refresh_session'),
            (StopCollection('access_denied'), 'access_denied', 'article_fetch', 'check_access_restriction'),
            (StopCollection('rate_limited'), 'rate_limited', 'article_fetch', 'wait_before_retry'),
            (StopCollection('robots_disallowed'), 'robots_disallowed', 'article_fetch', 'stop_robots_restriction'),
            (requests.ConnectionError(SECRET), 'article_request_failed', 'article_fetch', 'check_network'),
        ]
        for html, reason, stage, action in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.probe(html), {'reason': reason, 'stage': stage, 'action': action})

    def test_navigation_is_not_a_subscription_barrier(self):
        self.assertEqual(self.probe(article(extra='<nav><a>Subscribe</a><a>Sign in</a></nav>'))['reason'], 'ok')

    def test_every_explicit_barrier_is_checked(self):
        html = article(extra='<div class="barrier">Other</div><div class="barrier">Subscribe</div>')
        self.assertEqual(self.probe(html)['reason'], 'subscription_barrier_detected')

    def test_no_credentials_no_network(self):
        for content in ['', 'FTConsent=yes', 'FTSession_s=YOUR_KEY_HERE']:
            self.cookie.write_text(content)
            with patch.object(FTSession, 'fetch') as fetch:
                result = preflight(self.cookie, URLS[0], root=self.root)
                self.assertEqual(result['reason'], 'auth_required')
                fetch.assert_not_called()
        self.cookie.unlink()
        self.assertEqual(preflight(self.cookie, URLS[0], root=self.root)['reason'], 'credential_unavailable')

    def test_private_path_and_url_validation_before_network(self):
        with patch.object(FTSession, 'fetch') as fetch:
            self.assertEqual(preflight(self.root / 'private', URLS[0], root=self.root)['reason'], 'private_file_must_be_external')
            self.assertEqual(preflight(self.cookie, 'https://example.org/' + SECRET, root=self.root)['reason'], 'unsafe_destination')
            self.assertEqual(preflight(self.cookie, 'https://www.ft.com/content/invalid', root=self.root)['reason'], 'invalid_article_url')
            fetch.assert_not_called()

    def test_auth_subscription_redirects_stop_before_destination(self):
        cases = [('https://accounts.ft.com/login', 'auth_required'),
                 ('https://subs.ft.com/products', 'subscription_required'),
                 ('https://www.ft.com/subscribe', 'subscription_required'),
                 ('https://www.ft.com/login', 'auth_required')]
        for destination, reason in cases:
            with self.subTest(destination=destination), FTSession('FTSession_s=fixture') as session:
                adapter = RedirectAdapter(destination=destination)
                session.mount('https://', adapter)
                with self.assertRaises(StopCollection) as error:
                    session.get(URLS[0])
                self.assertEqual(error.exception.reason, reason)
                self.assertEqual(len(adapter.calls), 1)

    def test_cli_success_is_readonly_and_keeps_environment(self):
        before = {p: p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()}
        out, err = io.StringIO(), io.StringIO()
        session = FakeSession({URLS[0]: article()})
        with patch.dict(os.environ, {'FT_COOKIE': 'previous'}), patch('crawler.ft_impl.session.FTSession', return_value=session), redirect_stdout(out), redirect_stderr(err):
            code = main(['--cookie-file', str(self.cookie), '--article-url', URLS[0]])
            self.assertEqual(os.environ['FT_COOKIE'], 'previous')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), {'status': 'ok', 'reason': 'ok', 'stage': 'complete', 'action': 'none'})
        self.assertEqual(err.getvalue(), '')
        self.assertEqual(before, {p: p.read_bytes() for p in Path(self.temp.name).rglob('*') if p.is_file()})

    def test_cli_suppresses_dependency_output_logs_and_exceptions(self):
        out, err, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        previous = logging.root.manager.disable
        def noisy(*args):
            print(SECRET)
            import sys
            print(SECRET, file=sys.stderr)
            logging.critical(SECRET)
            raise RuntimeError(SECRET)
        with patch('scripts.ft_preflight.preflight', side_effect=noisy), redirect_stdout(out), redirect_stderr(err):
            code = main(['--cookie-file', str(self.cookie), '--article-url', URLS[0]])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())['reason'], 'diagnostic_failed')
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue() + logs.getvalue())
        self.assertEqual(logging.root.manager.disable, previous)

    def test_cli_redacts_invalid_arguments_and_untrusted_reason(self):
        for args in [['--unexpected', SECRET], ['--cookie-file', str(self.cookie), '--article-url', URLS[0]]]:
            out, err = io.StringIO(), io.StringIO()
            with patch('scripts.ft_preflight.preflight', return_value={'reason': SECRET, 'stage': SECRET}), redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main(args), 1)
            self.assertNotIn(SECRET, out.getvalue() + err.getvalue())

    def test_collection_diagnostics_survive_local_report_filter(self):
        for html, reason in [('<div class="barrier">Subscribe</div>', 'subscription_barrier_detected'),
                             ('<p>Discover all the plans</p>', 'paywall_page_text_detected'),
                             ('<div>Changed layout</div>', 'visible_article_body_missing')]:
            session = FakeSession({'feed': feed(URLS[:1]), URLS[0]: html})
            rows, report = fixtures.TestFTCollector().collect(session)
            self.assertEqual(rows, [])
            safe = _report(report)
            self.assertEqual(safe['reason'], reason)
            self.assertEqual(safe['diagnostic']['stage'], 'article_parse')
            self.assertEqual(safe['errors'][0]['diagnostic']['reason'], reason)
            self.assertNotIn(URLS[0], json.dumps(safe))
        injected = _report({'status': 'failed', 'reason': 'auth_expired',
                            'diagnostic': {'reason': SECRET, 'stage': SECRET, 'action': SECRET, 'cookie': SECRET}})
        self.assertNotIn(SECRET, json.dumps(injected))

    def test_summary_diagnostic_matches_selected_failure(self):
        session = FakeSession({'feed': feed(URLS[:2]),
                               URLS[0]: '<div>Changed layout</div>',
                               URLS[1]: article(body='<p>Short</p>')})
        _, report = fixtures.TestFTCollector().collect(session)
        safe = _report(report)
        self.assertEqual(safe['reason'], 'visible_article_body_missing')
        self.assertEqual(safe['diagnostic']['reason'], safe['reason'])


if __name__ == '__main__':
    unittest.main()
