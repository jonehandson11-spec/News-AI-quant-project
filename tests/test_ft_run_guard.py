"""Access recovery is driven by real credential changes, not repeated requests."""
from datetime import timedelta
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import ft_run_guard as guard, ft_sync, ft_autorun as runner
from scripts.dataset import read_json, write_json
import test_ft_sync as fixtures

NOW = fixtures.NOW


class GuardUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.root = self.state / 'repo'
        self.cookie = self.state / 'cookie.txt'
        self.cookie.write_text('FTSession_s=synthetic-secret', encoding='utf-8')
        self.fp = guard.credential_fingerprint(self.cookie)

    def batch(self, ident='a', reason='login_or_subscription_required', success=0):
        return {'batch_id': ident * 64, 'finished_at': NOW.isoformat(),
                'report': {'status': 'failed' if not success else 'partial',
                           'reason': reason, 'stopped': True, 'counts': {'success': success}}}

    def test_formatting_or_tracking_cookie_changes_do_not_unlock_auth(self):
        self.cookie.write_text('\ufeffFTSession_s=synthetic-secret; tracking=another\n', encoding='utf-8')
        self.assertEqual(self.fp, guard.credential_fingerprint(self.cookie))
        state = guard.observe(self.state, self.root, self.fp, self.batch())
        self.assertEqual(guard.decision(state, self.fp, NOW)['reason'], 'awaiting_credential_update')
        self.assertEqual(guard.decision(state, self.fp, NOW + timedelta(days=7))['reason'], 'awaiting_credential_update')
        self.assertNotIn('synthetic-secret', (self.state / guard.STATE_FILE).read_text())

    def test_changed_credentials_unlock_once_without_clearing_other_guards(self):
        state = guard.observe(self.state, self.root, self.fp, self.batch())
        self.cookie.write_text('FTSession_s=new-fixture', encoding='utf-8')
        changed = guard.credential_fingerprint(self.cookie)
        self.assertIsNone(guard.decision(state, changed, NOW))
        self.assertTrue(guard.changed_credentials(state, changed, {'credential_fingerprint': self.fp}))
        self.assertFalse(guard.changed_credentials(state, changed, {'credential_fingerprint': changed}))
        blocked = guard.decision(state, changed, NOW, inherited_cooldown=(NOW + timedelta(hours=2)).isoformat())
        self.assertEqual(blocked['reason'], 'rate_limited')

    def test_rate_deadline_is_based_on_failure_time_and_replays_do_not_extend_it(self):
        batch = self.batch(reason='rate_limited')
        batch['report']['retry_after_seconds'] = 7200
        state = guard.observe(self.state, self.root, self.fp, batch)
        replay = guard.observe(self.state, self.root, self.fp, batch)
        self.assertEqual(state, replay)
        self.assertEqual(state['retry_after'], (NOW + timedelta(hours=2)).isoformat())
        self.assertEqual(guard.decision(state, self.fp, NOW)['reason'], 'rate_limited')
        self.assertIsNone(guard.decision(state, self.fp, NOW + timedelta(hours=2)))

    def test_local_article_barrier_does_not_create_a_global_auth_block(self):
        state = guard.observe(self.state, self.root, self.fp, self.batch(reason='article_access_unavailable', success=2))
        self.assertIsNone(guard.decision(state, self.fp, NOW))
        self.assertNotIn('auth_reason', state)

    def test_real_success_clears_auth_state_but_stale_observation_cannot_restore_it(self):
        old = self.batch()
        guard.observe(self.state, self.root, self.fp, old)
        batch = self.batch(ident='b', reason=None, success=1)
        batch['finished_at'] = (NOW + timedelta(minutes=1)).isoformat()
        batch['report'].update(status='complete', stopped=False)
        state = guard.observe(self.state, self.root, 'new-fingerprint', batch)
        self.assertNotIn('blocked_fingerprint', state)
        self.assertEqual(guard.observe(self.state, self.root, self.fp, old), state)


class PrepareRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FTSyncTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture
        self.f.cookie.write_text('FTSession_s=fixture-one', encoding='utf-8')

    def auth_failure(self):
        def failed(**kwargs):
            kwargs['report'].update(status='failed', reason='login_or_subscription_required',
                                    stopped=True, counts={'success': 0, 'failed': 1},
                                    errors=[{'reason': 'login_or_subscription_required'}])
            return iter(())
        from scripts.ft_local import collect_batch
        with patch.object(ft_sync, '_collect_batch', side_effect=lambda *a, **kw: collect_batch(*a, **kw, collector=failed)):
            plan = ft_sync.prepare(self.f.repo, self.f.state, self.f.cookie, now=NOW)
        # Mark the local plan acknowledged to exercise the new-collection gate.
        saved = read_json(Path(plan['plan_file']))
        saved['receipt_file'] = str(Path(plan['plan_file']).parent / 'receipt.json')
        write_json(Path(plan['plan_file']), saved)
        return plan

    def test_same_credentials_block_force_and_backfill_before_collector(self):
        plan = self.auth_failure()
        with patch.object(ft_sync, '_collect_batch', side_effect=AssertionError('must not fetch FT')), \
                patch.object(ft_sync, '_snapshot', side_effect=AssertionError('blocked check must not copy DB')):
            for options in ({}, {'force': True}, {'lookback_hours': 120}):
                result = ft_sync.prepare(self.f.repo, self.f.state, self.f.cookie,
                                         now=NOW + timedelta(days=1), **options)
                self.assertEqual(result['status'], 'skipped')
                self.assertEqual(result['result_summary']['reason'], 'awaiting_credential_update')
                self.assertEqual(result['publish_files'], [])
        self.assertNotIn('credential_fingerprint', json.dumps(plan))
        self.assertNotIn('fixture-one', Path(plan['batch_file']).read_text())

    def test_credential_update_allows_same_slot_collection_once(self):
        self.auth_failure()
        self.f.cookie.write_text('FTSession_s=fixture-two', encoding='utf-8')
        plan, calls = self.f.prepare()
        self.assertEqual(calls, 1)
        self.assertEqual(plan['status'], 'ready')
        self.assertNotIn('auth_reason', guard.load(self.f.state, self.f.repo))
        with patch.object(ft_sync, '_collect_batch', side_effect=AssertionError('pending must resume')):
            resumed = ft_sync.prepare(self.f.repo, self.f.state, self.f.cookie, now=NOW)
        self.assertEqual(resumed['batch_id'], plan['batch_id'])

    def test_direct_prepare_respects_cooldown_even_with_force_and_changed_cookie(self):
        self.auth_failure()
        self.f.cookie.write_text('FTSession_s=fixture-two', encoding='utf-8')
        write_json(self.f.state / 'autorun.json', {'cooldown_until': (NOW + timedelta(hours=1)).isoformat()})
        with patch.object(ft_sync, '_collect_batch', side_effect=AssertionError('cooldown must prevent FT')):
            result = ft_sync.prepare(self.f.repo, self.f.state, self.f.cookie, now=NOW, force=True)
        self.assertEqual(result['result_summary']['reason'], 'rate_limited')


class GuardResultTests(unittest.TestCase):
    def test_runner_preserves_attention_instead_of_treating_blocked_as_success(self):
        from contextlib import nullcontext
        settings = SimpleNamespace(check=False, state_dir=Path('.'), root=Path('.'), cookie_file=Path('.'),
                                   max_new=100, lookback_hours=48, resume_only=False)
        summary = {'reason': 'awaiting_credential_update', 'auth_reason': 'login_or_subscription_required', 'after': 2358}
        with patch.object(runner, 'validate_settings'), patch.object(runner, 'process_lock', return_value=nullcontext()), \
                patch.object(runner, '_load_state', return_value={}), patch.object(runner, '_inherit_cooldown'), \
                patch.object(runner, '_pending', return_value=None), \
                patch.object(ft_sync, 'prepare', return_value={'status': 'skipped', 'result_summary': summary}), \
                patch.object(runner, 'service_plan', side_effect=AssertionError('no empty batch upload')):
            result = runner.run(settings)
        self.assertEqual(result['status'], 'needs_attention')
        self.assertEqual(result['reason'], 'login_or_subscription_required')
        self.assertEqual(result['action'], 'update_local_credentials')


if __name__ == '__main__':
    unittest.main()
