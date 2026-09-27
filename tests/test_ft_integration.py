"""Offline FT integration tests; no real credentials or network requests."""
from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.crawl_daily import normalize, run_daily


NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
FT = "Financial Times"
BBC = "BBC News"
SINA = "新浪财经"


def article(number, source):
    if source == FT:
        url = f"https://www.ft.com/content/{number:08x}-0000-0000-0000-000000000001"
    elif source == BBC:
        url = f"https://www.bbc.com/news/articles/{number}"
    else:
        url = f"https://finance.sina.com.cn/news/{number}.shtml"
    return {
        "article_id": hashlib.sha256(url.encode()).hexdigest()[:32],
        "source": source, "title": f"Fixture article {number}",
        "content": f"Fixture full article {number}. " * 40,
        "publish_time": "2026-09-27T10:00:00+08:00",
        "crawl_time": "2026-09-27T19:00:00+08:00",
        "url": url, "language": "zh-CN" if source == SINA else "en",
    }


class FTIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data").mkdir()
        self.database = self.root / "data/news.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE news (article_id TEXT PRIMARY KEY, source TEXT NOT NULL, "
                       "title TEXT NOT NULL, content TEXT NOT NULL, publish_time TEXT NOT NULL, "
                       "crawl_time TEXT NOT NULL, url TEXT NOT NULL UNIQUE, language TEXT NOT NULL)")
        (self.root / "crawl_config.json").write_text(json.dumps({
            "target_articles": 3000, "daily_lookback_hours": 48,
            "source_max_new": {FT: 100},
        }), encoding="utf-8")
        (self.root / "data/manifest.json").write_text(
            json.dumps({"latest_run": None}), encoding="utf-8")

    def collect(self, collectors, **kwargs):
        output = io.StringIO()
        with patch("scripts.crawl_daily.refresh") as refresh, redirect_stdout(output):
            result = run_daily(self.root, now=NOW, collectors=collectors, **kwargs)
        return result, refresh, output.getvalue()

    def source_counts(self):
        with closing(sqlite3.connect(self.database)) as db:
            return dict(db.execute("SELECT source, count(*) FROM news GROUP BY source"))

    def enable_metadata_refresh(self):
        """Supply the portable metadata fixture needed for real refresh writes."""
        schema = (
            "CREATE TABLE collection_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);\n"
            "CREATE VIEW source_summary AS SELECT source, count(*) AS article_count, "
            "min(publish_time) AS earliest_publish_time, "
            "max(publish_time) AS latest_publish_time FROM news GROUP BY source;\n"
        )
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript(schema)
        (self.root / "schema.sql").write_text(schema, encoding="utf-8")
        config_path = self.root / "crawl_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["schedule"] = {
            "timezone": "Asia/Shanghai", "local_time": "20:00", "cron_utc": "0 12 * * *",
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "data/manifest.json").write_text(json.dumps({
            "latest_run": None, "sources": {},
            "publication_window": {
                "start": "2026-09-25T20:00:00+08:00",
                "end": "2026-09-27T20:00:00+08:00", "inclusive": True,
            },
        }), encoding="utf-8")

    def manifest(self):
        return json.loads((self.root / "data/manifest.json").read_text(encoding="utf-8"))

    def test_ft_runs_first_and_configured_cap_is_enforced_by_accumulator(self):
        calls = []

        def collector(source, offered):
            def run(**kwargs):
                calls.append((source, kwargs["limit"]))
                # Deliberately ignore the advertised limit: the outer layer
                # must still enforce FT's cap before visiting other sources.
                for number in range(offered):
                    yield article(number, source)
            return run

        result, refresh, _ = self.collect({
            SINA: collector(SINA, 1), BBC: collector(BBC, 1), FT: collector(FT, 102),
        })
        self.assertEqual(calls, [(FT, 100), (BBC, 2900), (SINA, 2899)])
        self.assertEqual(self.source_counts(), {FT: 100, BBC: 1, SINA: 1})
        self.assertEqual(result["inserted"], 102)
        self.assertEqual(result["status"], "success")
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_expired_ft_auth_keeps_other_sources_and_marks_partial(self):
        def expired(**kwargs):
            kwargs["report"].update(status="failed", reason="auth_expired")
            return iter(())

        bbc = Mock(return_value=iter([article(1, BBC)]))
        sina = Mock(return_value=iter([article(1, SINA)]))
        result, refresh, _ = self.collect({FT: expired, BBC: bbc, SINA: sina})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"][FT]["reason"], "auth_expired")
        self.assertEqual(result["sources"][FT]["counts"]["inserted"], 0)
        self.assertEqual(self.source_counts(), {BBC: 1, SINA: 1})
        bbc.assert_called_once()
        sina.assert_called_once()
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_ft_exception_text_is_absent_from_reports_and_stdout(self):
        fake_secret = "FAKE-FT-SESSION-SHOULD-NEVER-APPEAR-12345"
        ft = Mock(side_effect=RuntimeError(f"Cookie: FTSession={fake_secret}"))
        result, refresh, output = self.collect({
            FT: ft, BBC: Mock(return_value=iter([article(1, BBC)])),
            SINA: Mock(return_value=iter([article(1, SINA)])),
        })
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sources"][FT]["status"], "failed")
        self.assertTrue(result["sources"][FT]["errors"])
        self.assertNotIn(fake_secret, output)
        self.assertNotIn(fake_secret, json.dumps(result))
        self.assertNotIn("Cookie: FTSession=", output)
        self.assertEqual(self.source_counts(), {BBC: 1, SINA: 1})
        refresh.assert_called_once()

    def test_ft_cleanup_exception_is_redacted_and_saved_rows_are_preserved(self):
        fake_secret = "FAKE-COOKIE-ON-GENERATOR-CLOSE-98765"

        def ft(**kwargs):
            try:
                yield article(1, FT)
                yield article(2, FT)
            finally:
                raise RuntimeError(fake_secret)

        result, refresh, output = self.collect({FT: ft}, max_new=1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.source_counts(), {FT: 1})
        self.assertNotIn(fake_secret, output)
        self.assertNotIn(fake_secret, json.dumps(result))
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_unknown_source_cannot_reuse_a_recognized_source_url(self):
        unknown = "Unrecognized source"
        candidate = article(1, SINA)
        candidate["source"] = unknown
        with self.assertRaisesRegex(ValueError, "Unknown news source"):
            normalize(candidate, unknown, NOW - timedelta(hours=48), NOW)

    def test_ft_rejects_non_article_and_non_ft_urls(self):
        for url in (
            article(1, SINA)["url"],
            "https://www.ft.com/login",
            "https://accounts.ft.com/content/00000001-0000-0000-0000-000000000001",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                candidate = article(1, FT)
                candidate["url"] = url
                normalize(candidate, FT, NOW - timedelta(hours=48), NOW)

    def test_three_source_probe_collects_one_each(self):
        calls = []

        def collector(source):
            def run(**kwargs):
                calls.append((source, kwargs["limit"]))
                # Root must stop the generator after one yielded article.
                for number in range(3):
                    yield article(number, source)
            return run

        result, refresh, _ = self.collect({
            BBC: collector(BBC), SINA: collector(SINA), FT: collector(FT),
        }, max_new=3, per_source_limit=1, force=True)
        self.assertEqual(calls, [(FT, 1), (BBC, 1), (SINA, 1)])
        self.assertEqual(self.source_counts(), {FT: 1, BBC: 1, SINA: 1})
        self.assertEqual(result["inserted"], 3)
        self.assertEqual(result["status"], "success")
        refresh.assert_called_once_with(self.root, run=result, now=NOW)

    def test_selected_ft_run_does_not_block_full_run_on_same_beijing_day(self):
        self.enable_metadata_refresh()
        ft_only = {FT: Mock(return_value=iter([article(1, FT)]))}
        with redirect_stdout(io.StringIO()):
            first = run_daily(self.root, now=NOW, collectors=ft_only)
        self.assertEqual(first["scope"], "selected")
        self.assertEqual(first["selected_sources"], [FT])
        self.assertIsNone(self.manifest()["last_full_run_at"])

        all_sources = {
            FT: Mock(return_value=iter([article(2, FT)])),
            BBC: Mock(return_value=iter([article(1, BBC)])),
            SINA: Mock(return_value=iter([article(1, SINA)])),
        }
        with redirect_stdout(io.StringIO()):
            full = run_daily(self.root, now=NOW + timedelta(hours=1), collectors=all_sources)
        self.assertEqual(full["status"], "success")
        self.assertEqual(full["scope"], "all")
        self.assertEqual(full["inserted"], 3)
        self.assertEqual(self.source_counts(), {FT: 2, BBC: 1, SINA: 1})
        for collector in all_sources.values():
            collector.assert_called_once()
        self.assertEqual(self.manifest()["last_full_run_at"], full["started_at"])

    def test_selected_retry_preserves_full_run_guard_for_same_beijing_day(self):
        self.enable_metadata_refresh()
        all_sources = {
            source: Mock(return_value=iter([article(1, source)]))
            for source in (FT, BBC, SINA)
        }
        with redirect_stdout(io.StringIO()):
            full = run_daily(self.root, now=NOW, collectors=all_sources)
            retry = run_daily(
                self.root, now=NOW + timedelta(hours=1), force=True,
                collectors={FT: Mock(return_value=iter([article(2, FT)]))},
            )
        manifest = self.manifest()
        self.assertEqual(full["scope"], "all")
        self.assertEqual(retry["scope"], "selected")
        self.assertEqual(manifest["latest_run"]["started_at"], retry["started_at"])
        self.assertEqual(manifest["last_full_run_at"], full["started_at"])
        with closing(sqlite3.connect(self.database)) as db:
            stored = db.execute("SELECT value FROM collection_info WHERE key='last_full_run_at'").fetchone()[0]
        self.assertEqual(stored, full["started_at"])

        guarded = {source: Mock() for source in (FT, BBC, SINA)}
        with redirect_stdout(io.StringIO()), patch("scripts.crawl_daily.refresh") as refresh:
            skipped = run_daily(self.root, now=NOW + timedelta(hours=2), collectors=guarded)
        self.assertEqual(skipped, {
            "status": "already_ran_today", "before": 4, "after": 4, "inserted": 0,
        })
        for collector in guarded.values():
            collector.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(self.manifest(), manifest)


if __name__ == "__main__":
    unittest.main()
