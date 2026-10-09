"""Offline archive preparation and cloud append preserve the existing dataset."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_validate_cumulative as fixtures
from scripts import ft_sync
from scripts.dataset import FIELDS, read_json, refresh, write_json
from scripts.ft_archive import ArchiveError, SOURCE, batch_id, main, merge_batch, prepare_batch
from scripts.validate_database import validate

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def archive_row(number=1, **changes):
    row = {"title": f"Archive title {number}", "publish_time": f"2026-09-{number + 1:02d} 09:30:00",
           "crawled_time": "2026-09-21 20:00:00", "source_website": SOURCE,
           "url": f"https://www.ft.com/content/00000000-0000-0000-0000-{number:012x}",
           "content": f"Article {number}. " + "A factual archived paragraph with sufficient article text. " * 15}
    row.update(changes)
    return row


class FTArchiveTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.ValidateCumulativeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.add_ft(empty=True)
        self.root = fixture.root
        write_json(self.root / "crawl_config.json", {"target_articles": 2, "daily_lookback_hours": 48,
                   "schedule": {"timezone": "Asia/Shanghai", "local_time": "20:00", "cron_utc": "0 12 * * *"}})
        self.health = {"status": "failed", "last_attempt_at": "2026-10-04T20:00:00+08:00",
                       "last_success_at": "2026-10-03T20:00:00+08:00", "needs_attention": True,
                       "last_reason": "login_or_subscription_required", "execution_location": "local"}
        self.latest = {"status": "failed", "reason": "login_or_subscription_required"}
        report = read_json(self.root / "data/source_reports/ft.json")
        report.update(health=self.health, latest_run=self.latest)
        write_json(self.root / "data/source_reports/ft.json", report)
        refresh(self.root, now=NOW)
        validate(self.root)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.external = Path(temporary.name)
        self.database, self.batch_file = self.external / "source.db", self.external / "batch.json"

    def source(self, rows):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE articles(title TEXT, publish_time TEXT, crawled_time TEXT, "
                       "source_website TEXT, url TEXT PRIMARY KEY, content TEXT)")
            db.executemany("INSERT INTO articles VALUES(?,?,?,?,?,?)", [tuple(row.values()) for row in rows])

    def contents(self):
        return {str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def rows(self):
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db:
            return db.execute("SELECT * FROM news ORDER BY url").fetchall()

    def import_archive(self):
        self.source([archive_row()])
        prepare_batch(self.root, self.database, self.batch_file, now=NOW)
        return merge_batch(self.root, self.batch_file, now=NOW)

    def snapshot_git(self, root, command, revision):
        self.assertEqual(root, self.root)
        self.assertEqual(command, "show")
        parent, relative = revision.split(":", 1)
        self.assertEqual(parent, "a" * 40)
        return SimpleNamespace(stdout=(self.root / relative).read_bytes())

    def rehash_artifact(self, relative):
        manifest = read_json(self.root / "data/manifest.json")
        content = (self.root / relative).read_bytes()
        for artifact in manifest["artifacts"]:
            if artifact["path"] == relative:
                artifact.update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        write_json(self.root / "data/manifest.json", manifest)

    def test_prepare_readonly_rejects_promo_and_short_text_and_normalizes_beijing(self):
        self.source([archive_row(), archive_row(2, content="short"),
                     archive_row(3, content="What our readers say. Brief with no waffle. " * 20),
                     archive_row(4, content="Subscribe to unlock. " + "Filler " * 100)])
        original, archive = self.contents(), self.database.read_bytes()
        result = prepare_batch(self.root, self.database, self.batch_file, now=NOW)
        self.assertEqual(result["accepted_records"], 1)
        self.assertEqual(result["rejected_counts"], {"promotional_content": 2, "short_content": 1})
        self.assertEqual(self.contents(), original)
        self.assertEqual(self.database.read_bytes(), archive)
        batch = read_json(self.batch_file)
        self.assertEqual(batch["source_file"], "ft_news.db")
        self.assertEqual(batch["source_sha256"], hashlib.sha256(archive).hexdigest())
        self.assertEqual(batch["rows"][0]["publish_time"], "2026-09-02T09:30:00+08:00")
        self.assertEqual(batch["rows"][0]["crawl_time"], "2026-09-21T20:00:00+08:00")
        self.assertNotIn(str(self.external), self.batch_file.read_text(encoding="utf-8"))

    def test_merge_preserves_rows_health_seed_and_is_byte_identical_on_retry(self):
        self.source([archive_row(), archive_row(2)])
        prepare_batch(self.root, self.database, self.batch_file, now=NOW)
        previous_rows = self.rows()
        seed = read_json(self.root / "data/manifest.json")["seed_window"]
        result = merge_batch(self.root, self.batch_file, now=NOW)
        self.assertEqual((result["before"], result["after"], result["inserted"]), (2, 4, 2))
        self.assertTrue(all(row in self.rows() for row in previous_rows))
        self.assertEqual(read_json(self.root / "crawl_config.json")["target_articles"], 4)
        report = read_json(self.root / "data/source_reports/ft.json")
        self.assertEqual(report["health"], self.health)
        self.assertEqual(report["latest_run"], self.latest)
        manifest = read_json(self.root / "data/manifest.json")
        self.assertEqual(manifest["seed_window"], seed)
        self.assertEqual(manifest["publication_window"]["start"], "2026-09-02T09:30:00+08:00")
        self.assertEqual(validate(self.root)["total_articles"], 4)
        files = self.contents()
        self.assertEqual(merge_batch(self.root, self.batch_file, now=NOW)["status"], "already_imported")
        self.assertEqual(self.contents(), files)

    def test_merge_deduplicates_existing_urls_and_whitespace_normalized_content(self):
        self.source([archive_row()])
        prepare_batch(self.root, self.database, self.batch_file, now=NOW)
        merge_batch(self.root, self.batch_file, now=NOW)
        batch = read_json(self.batch_file)
        second = dict(batch["rows"][0])
        second["url"] = archive_row(2)["url"]
        second["article_id"] = hashlib.sha256(second["url"].encode()).hexdigest()[:32]
        second["content"] = second["content"].replace(" ", "  ")
        batch["rows"].append(second)
        batch["source_records"] = 2
        batch["batch_id"] = batch_id(batch)
        write_json(self.batch_file, batch)
        existing = self.rows()
        result = merge_batch(self.root, self.batch_file, now=NOW)
        self.assertEqual((result["inserted"], result["duplicates"]), (0, 2))
        self.assertEqual(self.rows(), existing)

    def test_tampered_hash_and_rehashed_invalid_rows_fail_before_any_write(self):
        self.source([archive_row(), archive_row(2)])
        prepare_batch(self.root, self.database, self.batch_file, now=NOW)
        batch = read_json(self.batch_file)
        original = self.contents()
        batch["rows"][1]["content"] = "Subscribe to unlock. " * 50
        write_json(self.batch_file, batch)
        with self.assertRaisesRegex(ArchiveError, "invalid_batch_hash"):
            merge_batch(self.root, self.batch_file, now=NOW)
        batch["batch_id"] = batch_id(batch)
        write_json(self.batch_file, batch)
        with self.assertRaisesRegex(ArchiveError, "promotional_content"):
            merge_batch(self.root, self.batch_file, now=NOW)
        self.assertEqual(self.contents(), original)

    def test_cli_error_never_prints_paths_or_article_text(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["merge", "--root", str(self.root), "--batch-file", str(self.external / "missing-private.json")])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "reason": "archive_operation_failed"})

    def test_snapshot_exports_hashed_archive_receipt_from_the_pinned_commit(self):
        result = self.import_archive()
        original = self.contents()
        with patch.object(ft_sync, "_fetch", return_value=("a" * 40, "b" * 40)), \
                patch.object(ft_sync, "_git", side_effect=self.snapshot_git) as git:
            snapshot = ft_sync._snapshot(self.root, self.external)
        staging = Path(snapshot["staging_root"])
        self.assertEqual(validate(staging)["total_articles"], 3)
        self.assertEqual((staging / result["report"]).read_bytes(), (self.root / result["report"]).read_bytes())
        git.assert_any_call(self.root, "show", "a" * 40 + ":" + result["report"])
        self.assertEqual(self.contents(), original)

    def test_snapshot_rejects_malicious_receipt_path_before_fetching_its_contents(self):
        result = self.import_archive()
        manifest = read_json(self.root / "data/manifest.json")
        manifest["archive_imports"][0]["report"] = "../../private.json"
        write_json(self.root / "data/manifest.json", manifest)
        with patch.object(ft_sync, "_fetch", return_value=("a" * 40, "b" * 40)), \
                patch.object(ft_sync, "_git", side_effect=self.snapshot_git) as git:
            with self.assertRaisesRegex(ft_sync.SyncError, "invalid_archive_receipt"):
                ft_sync._snapshot(self.root, self.external)
        fetched = [call.args[2].split(":", 1)[1] for call in git.call_args_list]
        self.assertEqual(fetched, list(ft_sync.EXPORT_FILES))
        self.assertNotIn(result["report"], fetched)

    def test_preseed_ft_article_without_receipt_membership_is_rejected(self):
        self.import_archive()
        url = archive_row(2)["url"]
        identifier = hashlib.sha256(url.encode()).hexdigest()[:32]
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("UPDATE news SET article_id=?, source=?, url=?, publish_time=? WHERE source='BBC News'",
                       (identifier, SOURCE, url, "2026-09-02T09:30:00+08:00"))
        refresh(self.root, now=NOW)
        with self.assertRaisesRegex(ValueError, "Pre-seed article lacks an explicit FT archive receipt"):
            validate(self.root)

    def test_stored_archive_metadata_must_match_manifest_even_when_database_hash_matches(self):
        self.import_archive()
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("UPDATE collection_info SET value='[]' WHERE key='archive_imports'")
        self.rehash_artifact("data/news.sqlite3")
        with self.assertRaisesRegex(ValueError, "Stored archive metadata differs from manifest"):
            validate(self.root)


if __name__ == "__main__":
    unittest.main()
