"""Article-local failures must not abandon the rest of a productive backfill."""
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ft_autorun as runner
from scripts.ft_autorun import can_continue_backfill


class BackfillContinuationTests(unittest.TestCase):
    def report(self, reason='invalid_or_incomplete_article', stopped=False):
        return {'status': 'partial', 'stopped': stopped, 'reason': reason,
                'errors': [{'reason': reason}]}

    def test_partial_article_failures_can_continue_after_saving_new_rows(self):
        for reason in ('invalid_or_incomplete_article', 'article_request_failed'):
            self.assertTrue(can_continue_backfill(self.report(reason), 100))

    def test_empty_or_duplicate_only_batch_stops(self):
        self.assertFalse(can_continue_backfill(self.report(), 0))

    def test_stopped_and_restricted_runs_never_continue(self):
        self.assertFalse(can_continue_backfill(self.report(stopped=True), 1))
        for reason in ('rate_limited', 'access_denied', 'auth_required', 'auth_expired',
                       'login_or_subscription_required', 'robots_disallowed',
                       'unsafe_destination', 'consecutive_network_failures',
                       'feed_request_failed', 'collector_failed', 'invalid_candidate'):
            self.assertFalse(can_continue_backfill(self.report(reason), 1), reason)

    def test_missing_stop_flag_or_mixed_failures_do_not_continue(self):
        report = self.report()
        del report['stopped']
        self.assertFalse(can_continue_backfill(report, 1))
        report = self.report()
        report['errors'].append({'reason': 'auth_required'})
        self.assertFalse(can_continue_backfill(report, 1))

    def test_backfill_continues_after_saved_article_warning_but_daily_does_not(self):
        for hours, expected_calls in ((120, 2), (48, 1)):
            settings = SimpleNamespace(check=False, state_dir=None, root=None,
                                       resume_only=False, lookback_hours=hours)
            results = [{'status': 'imported_needs_attention', 'continue_backfill': True,
                        'inserted': 100, 'total_articles': 2000},
                       {'status': 'imported', 'inserted': 0, 'total_articles': 2000}]
            with patch.object(runner, 'validate_settings'), \
                 patch.object(runner, 'process_lock', return_value=nullcontext()), \
                 patch.object(runner, '_load_state', return_value={}), \
                 patch.object(runner, '_inherit_cooldown'), \
                 patch.object(runner, '_pending', return_value={}), \
                 patch.object(runner, 'service_plan', side_effect=results) as service:
                runner.run(settings)
                self.assertEqual(service.call_count, expected_calls)


if __name__ == '__main__':
    unittest.main()
