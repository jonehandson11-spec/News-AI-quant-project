"""Offline accumulator invariants; fixtures never depend on published news data."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.crawl_daily import run_daily
from scripts.dataset import FIELDS

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
BBC = "BBC News"
SINA = "新浪财经"


def article(number, source=BBC, *, old=False):
    domain = "www.bbc.com" if source == BBC else "finance.sina.com.cn"
    url = f"https://{domain}/news/articles/{number}"
    return {"article_id": hashlib.sha256(url.encode()).hexdigest()[:32],
            "source": source, "title": f"Original title {number}",
            "content": f"Original full article text {number}. " * 30,
            "publish_time": "2020-01-01T10:00:00+08:00" if old else "2026-09-27T10:00:00+08:00",
            "crawl_time": "2020-01-02T10:00:00+08:00" if old else "2026-09-27T19:00:00+08:00",
            "url": url, "language": "en" if source == BBC else "zh"}


class AccumulatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "data").mkdir()
        self.database = self.root / "data/news.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE news (article_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                       "title TEXT NOT NULL, content TEXT NOT NULL, publish_time TEXT NOT NULL, "
                       "crawl_time TEXT NOT NULL, url TEXT NOT NULL UNIQUE, language TEXT NOT NULL)")
        (self.root / "crawl_config.json").write_text(json.dumps({
            "target_articles": 3000, "daily_lookback_hours": 48}), encoding="utf-8")
        self.write_manifest()

    def write_manifest(self, last_run=None):
        (self.root / "data/manifest.json").write_text(
            json.dumps({"latest_run": last_run}), encoding="utf-8")

    def seed(self, count):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executemany("INSERT INTO news VALUES (?,?,?,?,?,?,?,?)", [
                tuple(article(i, old=True)[key] for key in FIELDS) for i in range(count)])

    def rows(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            return {row["url"]: dict(row) for row in db.execute("SELECT * FROM news")}

    def collect(self, collectors, **kwargs):
        with patch("scripts.crawl_daily.refresh") as refresh, patch("builtins.print"):
            result = run_daily(self.root, now=NOW, collectors=collectors, **kwargs)
        return result, refresh

    def test_2999_reaches_exactly_3000_and_preserves_every_old_article(self):
        self.seed(2999)
        original = self.rows()
        visited = []
        closed = []

        def bbc(**kwargs):
            self.assertEqual(kwargs["limit"], 1)
            self.assertEqual(len(kwargs["known_urls"]), 2999)
            try:
                for number in (9000, 9001, 9002):
                    visited.append(number)
                    yield article(number)
            finally:
                closed.append(True)

        sina = Mock(return_value=iter(()))
        result, refresh = self.collect({BBC: bbc, SINA: sina})
        rows = self.rows()
        self.assertEqual((result["before"], result["after"], result["inserted"]), (2999, 3000, 1))
        self.assertTrue(result["target_reached"])
        self.assertEqual(len(rows), 3000)
        self.assertTrue(all(rows[url] == row for url, row in original.items()))
        self.assertEqual(visited, [9000])
        self.assertEqual(closed, [True])
        sina.assert_not_called()
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_existing_3000_does_not_call_either_collector(self):
        self.seed(3000)
        bbc, sina = Mock(), Mock()
        result, refresh = self.collect({BBC: bbc, SINA: sina})
        self.assertEqual(result, {"status": "target_reached", "before": 3000, "after": 3000, "inserted": 0})
        bbc.assert_not_called()
        sina.assert_not_called()
        refresh.assert_not_called()

    def test_duplicates_do_not_consume_allowance_or_replace_old_content(self):
        self.seed(2998)
        duplicate = article(0)
        duplicate["title"] = "A changed title that must not overwrite the original"
        bbc = Mock(return_value=iter([duplicate, article(9000), article(9001)]))
        result, _ = self.collect({BBC: bbc, SINA: Mock(return_value=iter(()))}, max_new=2)
        rows = self.rows()
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["after"], 3000)
        self.assertEqual(bbc.call_args.kwargs["limit"], 2)
        self.assertEqual(rows[article(0)["url"]], article(0, old=True))

    def test_generator_failure_keeps_committed_rows_and_collects_other_source(self):
        def bbc(**kwargs):
            yield article(9000)
            raise RuntimeError("BBC request failed after a successful article")

        sina = Mock(return_value=iter([article(9001, SINA)]))
        result, refresh = self.collect({BBC: bbc, SINA: sina})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["sources"][BBC]["status"], "partial")
        self.assertEqual(result["sources"][SINA]["counts"]["inserted"], 1)
        self.assertEqual(set(self.rows()), {article(9000)["url"], article(9001, SINA)["url"]})
        sina.assert_called_once()
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_failed_source_before_any_yield_does_not_block_other_source(self):
        bbc = Mock(side_effect=OSError("BBC unavailable"))
        sina = Mock(return_value=iter([article(9000, SINA)]))
        result, refresh = self.collect({BBC: bbc, SINA: sina})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"][BBC]["status"], "failed")
        self.assertEqual(result["sources"][SINA]["counts"]["inserted"], 1)
        self.assertEqual(len(self.rows()), 1)
        refresh.assert_called_once()

    def test_same_beijing_day_skips_even_when_utc_date_differs(self):
        self.seed(1)
        self.write_manifest({"started_at": "2026-09-26T23:00:00+00:00"})
        bbc, sina = Mock(), Mock()
        result, refresh = self.collect({BBC: bbc, SINA: sina})
        self.assertEqual(result["status"], "already_ran_today")
        self.assertEqual((result["before"], result["after"], result["inserted"]), (1, 1, 0))
        bbc.assert_not_called()
        sina.assert_not_called()
        refresh.assert_not_called()

    def test_force_allows_explicit_retry_on_same_beijing_day(self):
        self.write_manifest({"started_at": "2026-09-27T07:00:00+08:00"})
        bbc = Mock(return_value=iter([article(9000)]))
        result, refresh = self.collect({BBC: bbc, SINA: Mock(return_value=iter(()))}, force=True)
        self.assertEqual(result["inserted"], 1)
        bbc.assert_called_once()
        refresh.assert_called_once()

    def test_close_failure_cannot_prevent_refresh_of_saved_rows(self):
        def bbc(**kwargs):
            try:
                yield article(9000)
                yield article(9001)
            finally:
                raise RuntimeError("Cache cleanup failed")

        result, refresh = self.collect({BBC: bbc, SINA: Mock(return_value=iter(()))}, max_new=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(len(self.rows()), 1)
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_rejected_row_remains_partial_after_source_overwrites_status(self):
        def bbc(**kwargs):
            invalid = article(9000)
            invalid["publish_time"] = "2020-01-01T00:00:00Z"
            yield invalid
            # BBC sets status before each successful yield; merge rejections
            # must survive a later source-local success.
            kwargs["report"]["status"] = "complete"
            yield article(9001)

        result, _ = self.collect({BBC: bbc, SINA: Mock(return_value=iter(()))})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"][BBC]["status"], "partial")
        self.assertEqual(result["sources"][BBC]["counts"]["rejected"], 1)
        self.assertEqual(result["inserted"], 1)
        self.assertNotIn(article(9000)["url"], self.rows())

    def test_probe_per_source_limit_leaves_budget_for_both_sources(self):
        calls = []

        def collector(source):
            def run(**kwargs):
                calls.append((source, kwargs["limit"]))
                for number in range(kwargs["limit"]):
                    yield article(9000 + number, source)
            return run

        result, refresh = self.collect({BBC: collector(BBC), SINA: collector(SINA)},
                                      max_new=2, per_source_limit=1)
        self.assertEqual(calls, [(BBC, 1), (SINA, 1)])
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["sources"][BBC]["counts"]["inserted"], 1)
        self.assertEqual(result["sources"][SINA]["counts"]["inserted"], 1)
        refresh.assert_called_once()


if __name__ == "__main__":
    unittest.main()
