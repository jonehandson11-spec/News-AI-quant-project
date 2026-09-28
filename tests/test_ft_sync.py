"""Exercise inbox preparation using local Git repositories and a fake FT collector."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ft_sync
from scripts.dataset import FIELDS, refresh, write_json
from scripts.ft_local import collect_batch, merge_batch
from scripts.validate_database import validate

NOW = datetime(2030, 1, 4, 12, 0, tzinfo=timezone.utc)
FT_URL = "https://www.ft.com/content/12345678-abcd-4567-89ab-0123456789ab"


class FTSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.remote = self.directory / "remote.git"
        self.writer = self.directory / "writer"
        self.repo = self.directory / "audited"
        self.state = self.directory / "private-state"
        self.cookie = self.directory / "cookie.txt"
        self.cookie.write_text("dummy-test-cookie", encoding="utf-8")
        self.git = ft_sync._git_executable()
        self.command(self.directory, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.command(self.directory, "init", "--initial-branch=main", str(self.writer))
        self.command(self.writer, "config", "user.name", "Fixture")
        self.command(self.writer, "config", "user.email", "fixture@example.invalid")
        self.command(self.writer, "config", "core.autocrlf", "false")
        self.make_data(self.writer)
        (self.writer / "private-cookie.txt").write_text("must-not-export", encoding="utf-8")
        self.command(self.writer, "add", ".")
        self.command(self.writer, "commit", "-m", "Initial fixture")
        self.command(self.writer, "remote", "add", "origin", str(self.remote))
        self.command(self.writer, "push", "origin", "main")
        self.command(self.directory, "clone", str(self.remote), str(self.repo))
        self.initial = self.command(self.repo, "rev-parse", "HEAD").strip()

    def command(self, cwd, *args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
        result = subprocess.run([self.git, "-C", str(cwd), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=True, env=env, timeout=30)
        return result.stdout.decode("utf-8").strip()

    def make_data(self, root):
        (root / "data/source_reports").mkdir(parents=True)
        schema = """
CREATE TABLE news(article_id TEXT PRIMARY KEY,source TEXT NOT NULL,title TEXT NOT NULL,
content TEXT NOT NULL,publish_time TEXT NOT NULL,crawl_time TEXT NOT NULL,url TEXT NOT NULL UNIQUE,language TEXT NOT NULL);
CREATE TABLE collection_info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE VIEW raw_news AS SELECT * FROM news;
CREATE VIEW bbc_news AS SELECT * FROM news WHERE source='BBC News';
CREATE VIEW sina_news AS SELECT * FROM news WHERE source='新浪财经';
CREATE VIEW ft_news AS SELECT * FROM news WHERE source='Financial Times';
CREATE VIEW source_summary AS SELECT source,count(*) AS article_count,min(publish_time) AS earliest_publish_time,
max(publish_time) AS latest_publish_time FROM news GROUP BY source;
"""
        (root / "schema.sql").write_text(schema, encoding="utf-8")
        window = {"start": "2030-01-01T20:00:00+08:00", "end": "2030-01-03T20:00:00+08:00", "inclusive": True}
        sources = {}
        with closing(sqlite3.connect(root / "data/news.sqlite3")) as db, db:
            db.executescript(schema)
            for source, slug, url, language in (
                ("BBC News", "bbc", "https://www.bbc.com/news/fixture", "en"),
                ("新浪财经", "sina", "https://finance.sina.com.cn/fixture", "zh-CN"),
                ("Financial Times", "ft", "https://www.ft.com/content/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "en"),
            ):
                db.execute("INSERT INTO news VALUES(?,?,?,?,?,?,?,?)", (
                    hashlib.sha256(url.encode()).hexdigest()[:32], source, "Fixture title", "Verified full article. " * 40,
                    "2030-01-02T20:00:00+08:00", "2030-01-03T20:00:00+08:00", url, language))
                path = f"data/source_reports/{slug}.json"
                sources[source] = {"report": path}
                write_json(root / path, {"source": source, "saved_full_text_articles": 1})
        write_json(root / "data/manifest.json", {"format_version": 1, "database": "data/news.sqlite3",
                   "csv": "data/news.csv", "publication_window": window, "sources": sources})
        write_json(root / "crawl_config.json", {"target_articles": 3000, "daily_lookback_hours": 48,
                   "schedule": {"timezone": "Asia/Shanghai", "local_time": "20:00", "cron_utc": "0 12 * * *"},
                   "source_max_new": {"Financial Times": 100}})
        refresh(root, now=NOW - timedelta(days=1))
        validate(root)

    def fake_collector(self, **kwargs):
        kwargs["report"].update(status="complete", counts={"success": 1}, errors=[])
        yield dict(zip(FIELDS, ("derived", "Financial Times", "FT new title", "Verified new reporting. " * 40,
                    "2030-01-04T18:00:00+08:00", "2030-01-04T20:00:00+08:00", FT_URL, "en")))

    def collect(self, *args, **kwargs):
        return collect_batch(*args, **kwargs, collector=self.fake_collector)

    def prepare(self, **kwargs):
        with patch.object(ft_sync, "_collect_batch", side_effect=self.collect) as collect:
            plan = ft_sync.prepare(self.repo, self.state, self.cookie, now=NOW, **kwargs)
            return plan, collect.call_count

    def push_data(self, message="Update fixture"):
        self.command(self.writer, "add", "data", "schema.sql", "crawl_config.json")
        self.command(self.writer, "commit", "-m", message)
        self.command(self.writer, "push", "origin", "main")
        return self.command(self.writer, "rev-parse", "HEAD")

    def test_prepare_exports_allowlist_and_only_publishes_one_batch(self):
        before = (self.repo / "data/news.sqlite3").read_bytes()
        plan, calls = self.prepare()
        self.assertEqual(calls, 1)
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["main_sha"], self.initial)
        self.assertEqual(plan["parent_sha"], self.initial)
        self.assertIsNone(plan["expected_inbox_sha"])
        self.assertEqual(plan["branch_name"], "codex/ft-inbox")
        self.assertEqual([item["path"] for item in plan["publish_files"]], ["incoming/ft-batch.json"])
        item = plan["publish_files"][0]
        content = Path(item["local_path"]).read_bytes()
        self.assertEqual(item["blob_sha"], self.command(self.repo, "hash-object", "--no-filters", item["local_path"]))
        self.assertEqual(item["sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotIn(b"dummy-test-cookie", content)
        for snapshot in Path(plan["plan_file"]).parent.glob("snapshot-*"):
            exported = {path.relative_to(snapshot).as_posix() for path in snapshot.rglob("*") if path.is_file()}
            self.assertEqual(exported, set(ft_sync.EXPORT_FILES))
        self.assertEqual((self.repo / "data/news.sqlite3").read_bytes(), before)
        self.assertEqual(self.command(self.repo, "rev-parse", "HEAD"), self.initial)
        self.assertEqual(self.command(self.repo, "status", "--porcelain"), "")

    def test_existing_inbox_is_parent_but_tree_is_current_main(self):
        self.command(self.writer, "checkout", "-b", ft_sync.INBOX_BRANCH)
        (self.writer / "inbox-only.txt").write_text("previous inbox", encoding="utf-8")
        self.command(self.writer, "add", "inbox-only.txt")
        self.command(self.writer, "commit", "-m", "Old inbox")
        inbox = self.command(self.writer, "rev-parse", "HEAD")
        self.command(self.writer, "push", "origin", ft_sync.INBOX_BRANCH)
        self.command(self.writer, "checkout", "main")
        plan, _ = self.prepare()
        self.assertEqual(plan["parent_sha"], inbox)
        self.assertEqual(plan["expected_inbox_sha"], inbox)
        self.assertEqual(plan["base_tree_sha"], self.command(self.writer, "rev-parse", "main^{tree}"))

    def test_resuming_pending_plan_refreshes_base_without_recrawling(self):
        first, _ = self.prepare()
        config = json.loads((self.writer / "crawl_config.json").read_text())
        config["description"] = "Concurrent configuration update"
        write_json(self.writer / "crawl_config.json", config)
        latest = self.push_data()
        with patch.object(ft_sync, "_collect_batch", side_effect=AssertionError("must not recrawl")):
            second = ft_sync.prepare(self.repo, self.state, self.cookie, now=NOW + timedelta(days=1, minutes=5))
        self.assertEqual(second["batch_file"], first["batch_file"])
        self.assertEqual(second["batch_sha256"], first["batch_sha256"])
        self.assertEqual(second["main_sha"], latest)

    def test_beijing_guard_before_at_and_after_twenty(self):
        self.assertEqual(ft_sync.due_slot(NOW - timedelta(seconds=1)).isoformat(), "2030-01-03T20:00:00+08:00")
        self.assertEqual(ft_sync.due_slot(NOW).isoformat(), "2030-01-04T20:00:00+08:00")
        self.assertEqual(ft_sync.due_slot(NOW + timedelta(hours=3)).isoformat(), "2030-01-04T20:00:00+08:00")
        report = json.loads((self.writer / "data/source_reports/ft.json").read_text())
        report["health"] = {"execution_location": "local", "last_attempt_at": "2030-01-04T20:00:00+08:00", "status": "failed"}
        write_json(self.writer / "data/source_reports/ft.json", report)
        refresh(self.writer, now=NOW)
        self.push_data()
        plan, calls = self.prepare()
        self.assertEqual(calls, 0)
        self.assertEqual(plan["result_summary"]["reason"], "already_attempted_due_slot")
        forced, calls = self.prepare(force=True)
        self.assertEqual(calls, 1)
        self.assertEqual(forced["status"], "ready")

    def test_cloud_ft_attempt_does_not_satisfy_local_guard(self):
        report = json.loads((self.writer / "data/source_reports/ft.json").read_text())
        report["health"] = {"execution_location": "cloud", "last_attempt_at": "2030-01-04T20:00:00+08:00"}
        write_json(self.writer / "data/source_reports/ft.json", report)
        refresh(self.writer, now=NOW)
        self.push_data()
        _, calls = self.prepare()
        self.assertEqual(calls, 1)

    def test_target_reached_does_not_read_cookie_or_collect(self):
        config = json.loads((self.writer / "crawl_config.json").read_text())
        config["target_articles"] = 3
        write_json(self.writer / "crawl_config.json", config)
        refresh(self.writer, now=NOW)
        self.push_data()
        self.cookie.unlink()
        plan, calls = self.prepare()
        self.assertEqual(calls, 0)
        self.assertEqual(plan["result_summary"]["reason"], "target_reached")

    def test_state_cookie_and_existing_lock_safety(self):
        for state, cookie, reason in (
            (self.repo / "private", self.cookie, "state_must_be_outside_repository"),
            (self.state, self.repo / "private-cookie.txt", "cookie_must_be_outside_repository"),
        ):
            with self.assertRaisesRegex(ft_sync.SyncError, reason):
                ft_sync.prepare(self.repo, state, cookie, now=NOW)
        self.state.mkdir()
        lock = self.state / "prepare.lock"
        lock.write_text("stale-or-active-lock", encoding="utf-8")
        with self.assertRaisesRegex(ft_sync.SyncError, "state_locked"):
            self.prepare()
        self.assertEqual(lock.read_text(), "stale-or-active-lock")

    def test_failed_attempt_is_not_repeated_and_lock_is_released(self):
        with patch.object(ft_sync, "_collect_batch", side_effect=RuntimeError("mock failure")):
            with self.assertRaises(RuntimeError):
                ft_sync.prepare(self.repo, self.state, self.cookie, now=NOW)
        self.assertFalse((self.state / "prepare.lock").exists())
        plan, calls = self.prepare()
        self.assertEqual(calls, 0)
        self.assertEqual(plan["result_summary"]["reason"], "local_attempt_already_started")

    def test_acknowledge_requires_main_import_and_records_external_receipt(self):
        plan, _ = self.prepare()
        merge_batch(self.writer, Path(plan["batch_file"]), now=NOW + timedelta(minutes=1))
        imported = self.push_data("Import FT batch")
        awaiting, calls = self.prepare()
        self.assertEqual(calls, 0)
        self.assertEqual(awaiting["status"], "awaiting_acknowledgement")
        self.assertEqual(awaiting["import_commit_sha"], imported)
        receipt = ft_sync.acknowledge(self.repo, Path(plan["plan_file"]), imported, now=NOW + timedelta(minutes=2))
        self.assertEqual(receipt["status"], "imported")
        self.assertEqual(receipt["batch_id"], plan["batch_id"])
        self.assertEqual(receipt["total_articles"], 4)
        self.assertEqual(receipt["ft_articles"], 2)
        self.assertFalse(Path(receipt["receipt_file"]).is_relative_to(self.repo))
        self.assertTrue((self.state / "latest_receipt.json").is_file())
        again, calls = self.prepare()
        self.assertEqual(calls, 0)
        self.assertEqual(again["status"], "skipped")

    def test_inbox_upload_is_not_acknowledged_as_database_import(self):
        plan, _ = self.prepare()
        self.command(self.writer, "checkout", "-b", ft_sync.INBOX_BRANCH)
        destination = self.writer / ft_sync.PUBLISH_PATH
        destination.parent.mkdir()
        shutil.copyfile(plan["batch_file"], destination)
        self.command(self.writer, "add", "incoming")
        self.command(self.writer, "commit", "-m", "Upload inbox only")
        inbox = self.command(self.writer, "rev-parse", "HEAD")
        self.command(self.writer, "push", "origin", ft_sync.INBOX_BRANCH)
        self.command(self.repo, "fetch", "origin", ft_sync.INBOX_BRANCH)
        with self.assertRaisesRegex(ft_sync.SyncError, "ack_commit_not_in_main"):
            ft_sync.acknowledge(self.repo, Path(plan["plan_file"]), inbox, now=NOW)
        with self.assertRaisesRegex(ft_sync.SyncError, "ack_batch_not_imported"):
            ft_sync.acknowledge(self.repo, Path(plan["plan_file"]), self.initial, now=NOW)
        self.assertFalse((self.state / "latest_receipt.json").exists())

    def test_plan_cannot_publish_database_or_change_batch(self):
        plan, _ = self.prepare()
        plan_path = Path(plan["plan_file"])
        altered = json.loads(plan_path.read_text())
        altered["publish_files"][0]["path"] = "data/news.sqlite3"
        write_json(plan_path, altered)
        with self.assertRaisesRegex(ft_sync.SyncError, "invalid_publish_path"):
            ft_sync.acknowledge(self.repo, plan_path, self.initial, now=NOW)
        write_json(plan_path, plan)
        Path(plan["batch_file"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ft_sync.SyncError, "batch_changed"):
            ft_sync.acknowledge(self.repo, plan_path, self.initial, now=NOW)

    def test_git_failures_use_fixed_codes_without_remote_output(self):
        with patch.object(ft_sync.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, b"", b"secret-output")):
            with self.assertRaisesRegex(ft_sync.SyncError, "^git_fetch_failed$"):
                ft_sync._git(self.repo, "fetch", reason="git_fetch_failed")


if __name__ == "__main__":
    unittest.main()
