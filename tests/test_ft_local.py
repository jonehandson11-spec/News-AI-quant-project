"""Local batches never overwrite cloud rows, leak credentials or downgrade health."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_validate_cumulative as fixtures
from test_ft_integration import article
from scripts.dataset import FIELDS, read_json, refresh, write_json
from scripts.ft_local import SOURCE, LocalFTError, collect_batch, main, merge_batch
from scripts.validate_database import validate

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
MERGED = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


class LocalFTTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ValidateCumulativeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.add_ft(empty=True)
        self.root = fixture.root
        write_json(self.root / 'crawl_config.json', {
            'target_articles': 3000, 'daily_lookback_hours': 48,
            'source_max_new': {SOURCE: 100},
            'schedule': {'timezone': 'Asia/Shanghai', 'local_time': '20:00', 'cron_utc': '0 12 * * *'},
        })
        refresh(self.root, now=NOW)
        validate(self.root)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.external = Path(temporary.name)
        self.batch_file = self.external / 'batch.json'
        self.cookie_file = self.external / 'fixture-cookie.txt'
        self.cookie_file.write_text('FTSession_s=FAKE-TEST-SECRET', encoding='utf-8')

    def rows(self):
        with closing(sqlite3.connect(self.root / 'data/news.sqlite3')) as db:
            db.row_factory = sqlite3.Row
            return {row['url']: dict(row) for row in db.execute('SELECT * FROM news')}

    def contents(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def batch(self, rows=None, *, started=NOW, report=None, lookback_hours=None):
        value = {
            'format_version': 1, 'source': SOURCE, 'execution_location': 'local',
            'started_at': started.isoformat(), 'finished_at': (started + timedelta(minutes=1)).isoformat(),
            'window': {'start': (started - timedelta(hours=lookback_hours or 48)).isoformat(), 'end': started.isoformat()},
            'rows': [article(1, SOURCE)] if rows is None else rows,
            'report': report or {'status': 'complete', 'counts': {}, 'errors': []},
        }
        if lookback_hours is not None:
            value.update(lookback_hours=lookback_hours,
                         collection_mode='backfill' if lookback_hours == 120 else 'daily')
        write_json(self.batch_file, value)
        return value

    def test_merge_retains_all_cloud_sources_and_derives_identifier(self):
        original = self.rows()
        candidate = article(1, SOURCE)
        candidate['article_id'] = 'untrusted-id'
        self.batch([candidate])
        result = merge_batch(self.root, self.batch_file, now=MERGED)
        self.assertEqual(result['inserted'], 1)
        current = self.rows()
        self.assertTrue(all(current[url] == row for url, row in original.items()))
        self.assertEqual(current[candidate['url']]['article_id'], article(1, SOURCE)['article_id'])
        self.assertEqual(validate(self.root)['total_articles'], 3)
        manifest = read_json(self.root / 'data/manifest.json')
        self.assertEqual(manifest['latest_run']['local_batch']['batch_id'], result['batch_id'])
        self.assertEqual(read_json(self.root / 'data/source_reports/ft.json')['latest_run']['batch_id'], result['batch_id'])

    def test_retry_is_idempotent_and_leaves_all_files_unchanged(self):
        self.batch()
        merge_batch(self.root, self.batch_file, now=MERGED)
        previous = self.contents()
        result = merge_batch(self.root, self.batch_file, now=MERGED)
        self.assertEqual((result['inserted'], result['duplicates']), (0, 1))
        self.assertTrue(result['health_preserved'])
        self.assertEqual(self.contents(), previous)

    def test_new_cloud_rows_are_preserved_when_batch_is_merged_later(self):
        self.batch([article(1, SOURCE), article(2, SOURCE)])
        cloud = article(1, SOURCE)
        cloud['title'] = 'Cloud already has an authoritative version'
        with closing(sqlite3.connect(self.root / 'data/news.sqlite3')) as db, db:
            db.execute('INSERT INTO news VALUES (?,?,?,?,?,?,?,?)', tuple(cloud[key] for key in FIELDS))
            bbc = article(99, 'BBC News')
            db.execute('INSERT INTO news VALUES (?,?,?,?,?,?,?,?)', tuple(bbc[key] for key in FIELDS))
        refresh(self.root, now=NOW)
        result = merge_batch(self.root, self.batch_file, now=MERGED)
        self.assertEqual((result['inserted'], result['duplicates'], result['after']), (1, 1, 5))
        self.assertEqual(self.rows()[cloud['url']], cloud)
        self.assertEqual(self.rows()[bbc['url']], bbc)

    def test_cap_reaches_exactly_3000_and_preserves_existing_rows(self):
        with closing(sqlite3.connect(self.root / 'data/news.sqlite3')) as db, db:
            for number in range(2997):
                row = article(number, 'BBC News')
                db.execute('INSERT INTO news VALUES (?,?,?,?,?,?,?,?)', tuple(row[key] for key in FIELDS))
        refresh(self.root, now=NOW)
        original = self.rows()
        self.batch([article(1, SOURCE), article(2, SOURCE)])
        result = merge_batch(self.root, self.batch_file, now=MERGED)
        self.assertEqual((result['before'], result['after'], result['inserted'], result['deferred_cap']), (2999, 3000, 1, 1))
        current = self.rows()
        self.assertTrue(all(current[url] == row for url, row in original.items()))
        self.assertFalse(read_json(self.root / 'data/manifest.json')['automatic_refresh'])

    def test_invalid_later_row_prevents_every_write(self):
        for field, value in [('source', 'BBC News'), ('publish_time', '2026-09-20T10:00:00+08:00'),
                             ('crawl_time', '2027-01-01T10:00:00+08:00'), ('url', 'https://www.ft.com/login')]:
            with self.subTest(field=field):
                invalid = dict(article(2, SOURCE), **{field: value})
                self.batch([article(1, SOURCE), invalid])
                before = self.contents()
                with self.assertRaises(LocalFTError):
                    merge_batch(self.root, self.batch_file, now=MERGED)
                self.assertEqual(self.contents(), before)

    def test_preseed_publication_is_rejected_even_within_batch_window(self):
        row = article(1, SOURCE)
        row['publish_time'] = '2026-09-24T20:00:00+08:00'
        row['crawl_time'] = '2026-09-25T19:00:00+08:00'
        self.batch([row], started=NOW - timedelta(days=2))
        before = self.contents()
        with self.assertRaises(LocalFTError):
            merge_batch(self.root, self.batch_file, now=MERGED)
        self.assertEqual(self.contents(), before)

    def test_failure_batch_updates_health_without_changing_articles(self):
        original = self.rows()
        self.batch([], report={'status': 'failed', 'reason': 'auth_expired', 'counts': {}, 'errors': []})
        result = merge_batch(self.root, self.batch_file, now=MERGED)
        health = read_json(self.root / 'data/source_reports/ft.json')['health']
        self.assertEqual(result['inserted'], 0)
        self.assertEqual(self.rows(), original)
        self.assertTrue(health['needs_attention'])
        self.assertEqual(health['last_reason'], 'auth_expired')
        validate(self.root)

    def test_older_failure_cannot_downgrade_newer_ft_health_or_other_sources(self):
        for slug in ('ft', 'bbc', 'sina'):
            path = self.root / f'data/source_reports/{slug}.json'
            report = read_json(path)
            report.update(health={'status': 'complete', 'last_attempt_at': MERGED.isoformat(),
                                  'needs_attention': False, 'last_success_at': MERGED.isoformat()},
                          latest_run={'status': 'complete', 'counts': {'inserted': 1}})
            write_json(path, report)
        manifest = read_json(self.root / 'data/manifest.json')
        manifest['last_full_run_at'] = MERGED.isoformat()
        write_json(self.root / 'data/manifest.json', manifest)
        refresh(self.root, now=MERGED)
        previous = {slug: read_json(self.root / f'data/source_reports/{slug}.json') for slug in ('ft', 'bbc', 'sina')}
        self.batch([article(2, SOURCE)], report={'status': 'partial', 'reason': 'auth_expired'})
        result = merge_batch(self.root, self.batch_file, now=MERGED + timedelta(minutes=1))
        self.assertTrue(result['health_preserved'])
        self.assertEqual(result['inserted'], 1)
        for slug in previous:
            current = read_json(self.root / f'data/source_reports/{slug}.json')
            self.assertEqual(current['health'], previous[slug]['health'])
            self.assertEqual(current['latest_run'], previous[slug]['latest_run'])
        self.assertEqual(read_json(self.root / 'data/manifest.json')['last_full_run_at'], MERGED.isoformat())

    def test_collect_is_readonly_and_restores_environment_without_secret_leak(self):
        def collector(**kwargs):
            self.assertEqual(os.environ['FT_COOKIE'], 'FTSession_s=FAKE-TEST-SECRET')
            print('FAKE-TEST-SECRET')
            kwargs['report'].update(status='partial', errors=[{'reason': 'FAKE-TEST-SECRET'}], cookie='FAKE-TEST-SECRET')
            yield article(1, SOURCE)
            raise RuntimeError('FAKE-TEST-SECRET')
        original = self.contents()
        output = io.StringIO()
        with patch.dict(os.environ, {'FT_COOKIE': 'previous-fixture'}), redirect_stdout(output):
            result = collect_batch(self.root, self.cookie_file, self.batch_file, now=NOW, collector=collector)
            self.assertEqual(os.environ['FT_COOKIE'], 'previous-fixture')
        self.assertEqual(result['collected'], 1)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.contents(), original)
        self.assertNotIn('FAKE-TEST-SECRET', self.batch_file.read_text(encoding='utf-8'))
        self.assertNotIn('FAKE-TEST-SECRET', output.getvalue())
        batch = read_json(self.batch_file)
        self.assertEqual((batch['lookback_hours'], batch['collection_mode']), (48, 'daily'))

    def test_explicit_backfill_collects_and_merges_article_older_than_48_hours(self):
        started = NOW + timedelta(days=4)
        row = article(1, SOURCE)
        row.update(publish_time=(started - timedelta(hours=72)).isoformat(), crawl_time=started.isoformat())
        def collector(**kwargs):
            self.assertEqual(kwargs['end'] - kwargs['start'], timedelta(hours=120))
            yield row
        before = self.contents()
        result = collect_batch(self.root, self.cookie_file, self.batch_file, now=started,
                               lookback_hours=120, collector=collector)
        self.assertEqual(result['collected'], 1)
        self.assertEqual(self.contents(), before)
        batch = read_json(self.batch_file)
        self.assertEqual((batch['lookback_hours'], batch['collection_mode']), (120, 'backfill'))
        merged = merge_batch(self.root, self.batch_file,
                             now=datetime.fromisoformat(batch['finished_at']) + timedelta(seconds=1))
        self.assertEqual(merged['inserted'], 1)
        self.assertIn(row['url'], self.rows())
        manifest = read_json(self.root / 'data/manifest.json')
        self.assertEqual(manifest['daily_lookback_hours'], 48)
        self.assertEqual(manifest['latest_run']['lookback_hours'], 120)
        self.assertEqual(manifest['latest_run']['collection_mode'], 'backfill')
        self.assertEqual(manifest['latest_run']['local_batch']['lookback_hours'], 120)
        self.assertEqual(read_json(self.root / 'crawl_config.json')['schedule']['local_time'], '20:00')
        self.assertEqual(validate(self.root)['total_articles'], 3)

    def test_backfill_rejects_row_older_than_120_hours_before_any_write(self):
        started = NOW + timedelta(days=4)
        invalid = dict(article(2, SOURCE), publish_time=(started - timedelta(hours=120, seconds=1)).isoformat(),
                       crawl_time=started.isoformat())
        valid = dict(article(1, SOURCE), publish_time=(started - timedelta(hours=72)).isoformat(),
                     crawl_time=started.isoformat())
        self.batch([valid, invalid], started=started, lookback_hours=120)
        before = self.contents()
        with self.assertRaisesRegex(LocalFTError, 'invalid_batch_row'):
            merge_batch(self.root, self.batch_file, now=started + timedelta(hours=1))
        self.assertEqual(self.contents(), before)

    def test_backfill_requires_explicit_markers_and_matching_window(self):
        started = NOW + timedelta(days=4)
        mutations = [
            lambda batch: (batch.pop('lookback_hours'), batch.pop('collection_mode')),
            lambda batch: batch.pop('collection_mode'),
            lambda batch: batch.update(collection_mode='daily'),
            lambda batch: batch.update(lookback_hours=121),
            lambda batch: batch['window'].update(start=(started - timedelta(hours=119)).isoformat()),
            lambda batch: batch.update(lookback_hours=48, collection_mode='daily'),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                batch = self.batch([], started=started, lookback_hours=120)
                mutate(batch)
                write_json(self.batch_file, batch)
                before = self.contents()
                with self.assertRaises(LocalFTError):
                    merge_batch(self.root, self.batch_file, now=started + timedelta(hours=1))
                self.assertEqual(self.contents(), before)

    def test_backfill_still_limits_single_batch_to_100(self):
        started = NOW + timedelta(days=4)
        candidates = [dict(article(number, SOURCE), publish_time=(started - timedelta(hours=72)).isoformat(),
                           crawl_time=started.isoformat()) for number in range(101)]
        result = collect_batch(self.root, self.cookie_file, self.batch_file, now=started,
                               lookback_hours=120, collector=lambda **kwargs: iter(candidates))
        self.assertEqual(result['collected'], 100)
        batch = read_json(self.batch_file)
        self.assertEqual(len(batch['rows']), 100)
        batch['rows'] = candidates
        write_json(self.batch_file, batch)
        before = self.contents()
        with self.assertRaisesRegex(LocalFTError, 'invalid_batch_rows'):
            merge_batch(self.root, self.batch_file,
                        now=datetime.fromisoformat(batch['finished_at']) + timedelta(seconds=1))
        self.assertEqual(self.contents(), before)

    def test_invalid_lookback_is_rejected_before_collecting(self):
        collector = Mock()
        for value in (0, 49, 121, True, '120'):
            with self.subTest(value=value), self.assertRaisesRegex(LocalFTError, 'lookback_hours_must_be_48_or_120'):
                collect_batch(self.root, self.cookie_file, self.batch_file,
                              lookback_hours=value, now=NOW, collector=collector)
        collector.assert_not_called()
        self.assertFalse(self.batch_file.exists())

    def test_cli_explicit_backfill_is_forwarded_without_changing_default(self):
        for extra, expected in (([], 48), (['--lookback-hours', '120'], 120)):
            with self.subTest(expected=expected), patch('scripts.ft_local.collect_batch', return_value={'status': 'complete'}) as collect:
                with redirect_stdout(io.StringIO()):
                    code = main(['collect', '--root', str(self.root), '--cookie-file', str(self.cookie_file),
                                 '--batch-file', str(self.batch_file), *extra])
                self.assertEqual(code, 0)
                self.assertEqual(collect.call_args.kwargs['lookback_hours'], expected)

    def test_discovery_metadata_is_preserved_without_arbitrary_text_or_credentials(self):
        def collector(**kwargs):
            kwargs['report']['discovery'] = {
                'mode': 'rss_and_category_pages', 'pages_fetched': 2, 'page_limit': 40,
                'category_page_limit': 5, 'categories_completed': 1, 'categories_total': 13,
                'coverage_limited': False, 'coverage_note': 'FAKE-TEST-SECRET',
                'cookie': 'FAKE-TEST-SECRET',
                'category_outcomes': [
                    {'url': 'https://www.ft.com/markets', 'pages': 2, 'reason': 'window_boundary',
                     'cookie': 'FAKE-TEST-SECRET'},
                    {'url': 'https://www.ft.com/markets?token=FAKE-TEST-SECRET', 'pages': 1, 'reason': 'no_next_page'},
                ],
            }
            yield article(1, SOURCE)
        collect_batch(self.root, self.cookie_file, self.batch_file, now=NOW,
                      lookback_hours=120, collector=collector)
        batch = read_json(self.batch_file)
        discovery = batch['report']['discovery']
        self.assertEqual(discovery['pages_fetched'], 2)
        self.assertTrue(discovery['coverage_limited'])
        self.assertEqual(discovery['category_outcomes'], [
            {'url': 'https://www.ft.com/markets', 'pages': 2, 'reason': 'window_boundary'}])
        self.assertNotIn('FAKE-TEST-SECRET', self.batch_file.read_text(encoding='utf-8'))

    def test_collect_failure_still_writes_mergeable_diagnostic_batch(self):
        self.cookie_file.unlink()
        result = collect_batch(self.root, self.cookie_file, self.batch_file, now=NOW)
        self.assertEqual(result['reason'], 'credential_unavailable')
        merge_batch(self.root, self.batch_file)
        health = read_json(self.root / 'data/source_reports/ft.json')['health']
        self.assertEqual(health['last_reason'], 'credential_unavailable')

    def test_target_full_never_reads_cookie_or_calls_collector(self):
        config = read_json(self.root / 'crawl_config.json')
        config['target_articles'] = 2
        write_json(self.root / 'crawl_config.json', config)
        refresh(self.root, now=NOW)
        self.cookie_file.unlink()
        collector = Mock()
        result = collect_batch(self.root, self.cookie_file, self.batch_file, now=NOW, collector=collector)
        self.assertEqual(result['reason'], 'target_reached')
        self.assertEqual(result['collected'], 0)
        collector.assert_not_called()

    def test_cli_arbitrary_exception_outputs_only_fixed_failure(self):
        output = io.StringIO()
        with patch('scripts.ft_local.merge_batch', side_effect=RuntimeError('FAKE-TEST-SECRET')), redirect_stdout(output):
            code = main(['merge', '--root', str(self.root), '--batch-file', str(self.batch_file)])
        self.assertEqual(code, 1)
        self.assertNotIn('FAKE-TEST-SECRET', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['reason'], 'local_ft_operation_failed')

    def test_private_batch_and_cookie_cannot_be_written_inside_repo(self):
        with self.assertRaises(LocalFTError):
            collect_batch(self.root, self.cookie_file, self.root / 'batch.json', now=NOW)

    def test_merge_cli_writes_safe_result_file(self):
        self.batch()
        result_file = self.external / 'result.json'
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['merge', '--root', str(self.root), '--batch-file', str(self.batch_file),
                         '--result-file', str(result_file)])
        self.assertEqual(code, 0)
        result = read_json(result_file)
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(len(result['batch_id']), 64)
        self.assertNotIn('content', result)
        self.assertNotIn(article(1, SOURCE)['content'], output.getvalue())


if __name__ == '__main__':
    unittest.main()
