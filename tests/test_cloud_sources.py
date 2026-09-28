"""Cloud/local source separation, with no network or credentials."""
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from scripts.crawl_daily import main, run_daily

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
FT, BBC, SINA = "Financial Times", "BBC News", "新浪财经"


def article(source):
    urls = {
        FT: "https://www.ft.com/content/00000001-0000-0000-0000-000000000001",
        BBC: "https://www.bbc.com/news/articles/fixture1",
        SINA: "https://finance.sina.com.cn/news/fixture1.shtml",
    }
    url = urls[source]
    return {"article_id": hashlib.sha256(url.encode()).hexdigest()[:32],
            "source": source, "title": "A fixture article", "content": "Fixture body. " * 70,
            "publish_time": "2026-09-28T10:00:00+08:00",
            "crawl_time": "2026-09-28T19:00:00+08:00", "url": url,
            "language": "zh-CN" if source == SINA else "en"}


class CloudSourcesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "data").mkdir()
        schema = """
        CREATE TABLE news (article_id TEXT PRIMARY KEY, source TEXT NOT NULL,
            title TEXT NOT NULL, content TEXT NOT NULL, publish_time TEXT NOT NULL,
            crawl_time TEXT NOT NULL, url TEXT NOT NULL UNIQUE, language TEXT NOT NULL);
        CREATE TABLE collection_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIEW source_summary AS SELECT source, count(*) AS article_count,
            min(publish_time) AS earliest_publish_time,
            max(publish_time) AS latest_publish_time FROM news GROUP BY source;
        """
        self.database = self.root / "data/news.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript(schema)
        (self.root / "schema.sql").write_text(schema, encoding="utf-8")
        (self.root / "crawl_config.json").write_text(json.dumps({
            "target_articles": 3000, "daily_lookback_hours": 48,
            "source_max_new": {FT: 100},
            "schedule": {"timezone": "Asia/Shanghai", "local_time": "20:00", "cron_utc": "0 12 * * *"},
        }), encoding="utf-8")
        (self.root / "data/manifest.json").write_text(json.dumps({
            "latest_run": None, "sources": {},
            "publication_window": {"start": "2026-09-26T20:00:00+08:00",
                                   "end": "2026-09-28T20:00:00+08:00", "inclusive": True},
        }), encoding="utf-8")

    def manifest(self):
        return json.loads((self.root / "data/manifest.json").read_text(encoding="utf-8"))

    def test_cloud_default_does_not_import_ft_and_probes_both_public_sources(self):
        modules = {}
        collectors = {}
        for name, source in (("bbc", BBC), ("sina", SINA)):
            module = ModuleType(f"crawler.{name}")
            module.collect = Mock(return_value=iter([article(source)]))
            modules[module.__name__] = module
            collectors[source] = module.collect
        # Importing FT under this context would raise ModuleNotFoundError.
        modules["crawler.ft"] = None
        with patch.dict(sys.modules, modules), patch("scripts.crawl_daily.refresh"), redirect_stdout(io.StringIO()):
            result = run_daily(self.root, now=NOW, cloud_only=True, max_new=2, per_source_limit=1)
        self.assertEqual(result["scope"], "all")
        self.assertEqual(result["execution_location"], "github_actions")
        self.assertEqual(result["selected_sources"], sorted([BBC, SINA]))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(result["sources"][FT], {
            "status": "not_needed", "counts": {"inserted": 0}, "reason": "collected_locally",
        })
        for collector in collectors.values():
            collector.assert_called_once()
            self.assertEqual(collector.call_args.kwargs["limit"], 1)

    def test_cloud_mode_never_calls_an_injected_ft_collector(self):
        ft = Mock(side_effect=AssertionError("FT must never be contacted from this run"))
        with patch("scripts.crawl_daily.refresh"), redirect_stdout(io.StringIO()):
            result = run_daily(self.root, now=NOW, cloud_only=True, collectors={
                FT: ft, BBC: Mock(return_value=iter([article(BBC)])),
                SINA: Mock(return_value=iter([article(SINA)])),
            })
        ft.assert_not_called()
        self.assertEqual(result["inserted"], 2)
        self.assertNotIn(FT, result["selected_sources"])

    def test_cloud_daily_guard_survives_independent_local_ft_run(self):
        cloud = {source: Mock(return_value=iter([article(source)])) for source in (BBC, SINA)}
        with redirect_stdout(io.StringIO()):
            first = run_daily(self.root, now=NOW, cloud_only=True, collectors=cloud)
            local = run_daily(self.root, now=NOW + timedelta(hours=1),
                              collectors={FT: Mock(return_value=iter([article(FT)]))})
        self.assertEqual(first["scope"], "all")
        self.assertEqual(first["execution_location"], "github_actions")
        self.assertEqual(local["status"], "success")
        self.assertEqual(local["scope"], "selected")
        self.assertEqual(local["execution_location"], "local")
        self.assertEqual(local["inserted"], 1)
        self.assertEqual(self.manifest()["last_full_run_at"], first["started_at"])
        guarded = {source: Mock() for source in (BBC, SINA)}
        with redirect_stdout(io.StringIO()):
            skipped = run_daily(self.root, now=NOW + timedelta(hours=2),
                                cloud_only=True, collectors=guarded)
        self.assertEqual(skipped["status"], "already_ran_today")
        for collector in guarded.values():
            collector.assert_not_called()

    def test_local_ft_run_does_not_suppress_same_day_cloud_collection(self):
        with redirect_stdout(io.StringIO()):
            local = run_daily(self.root, now=NOW, collectors={
                FT: Mock(return_value=iter([article(FT)])),
            })
            cloud = run_daily(self.root, now=NOW + timedelta(hours=1), cloud_only=True, collectors={
                BBC: Mock(return_value=iter([article(BBC)])),
                SINA: Mock(return_value=iter([article(SINA)])),
            })
        self.assertEqual(local["scope"], "selected")
        self.assertEqual(cloud["status"], "success")
        self.assertEqual(cloud["inserted"], 2)
        self.assertEqual(self.manifest()["last_full_run_at"], cloud["started_at"])

    def test_cli_rejects_combined_cloud_and_ft_modes_before_collection(self):
        with patch.object(sys, "argv", ["crawl_daily.py", "--cloud-only", "--ft-only"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main()
        self.assertEqual(error.exception.code, 2)

    def test_next_day_cloud_run_preserves_local_ft_health_and_batch_receipt(self):
        batch_id = "b" * 64

        def local_ft(**kwargs):
            kwargs["report"].update(batch_id=batch_id, execution_location="local")
            yield article(FT)

        with redirect_stdout(io.StringIO()):
            local = run_daily(self.root, now=NOW, collectors={FT: local_ft})
        ft_path = self.root / "data/source_reports/ft.json"
        before = json.loads(ft_path.read_text(encoding="utf-8"))
        self.assertEqual(local["status"], "success")
        self.assertEqual(before["latest_run"]["batch_id"], batch_id)
        self.assertEqual(before["health"]["execution_location"], "local")
        self.assertEqual(before["health"]["status"], "complete")
        self.assertFalse(before["health"]["needs_attention"])

        with redirect_stdout(io.StringIO()):
            cloud = run_daily(self.root, now=NOW + timedelta(days=1), cloud_only=True, collectors={
                BBC: Mock(return_value=iter([article(BBC)])),
                SINA: Mock(return_value=iter([article(SINA)])),
            })
        after = json.loads(ft_path.read_text(encoding="utf-8"))
        self.assertEqual(cloud["status"], "success")
        self.assertEqual(cloud["sources"][FT]["reason"], "collected_locally")
        self.assertEqual(after["latest_run"], before["latest_run"])
        self.assertEqual(after["health"], before["health"])
        with closing(sqlite3.connect(self.database)) as db:
            stored = json.loads(db.execute("SELECT value FROM collection_info WHERE key='source_reports'").fetchone()[0])
        self.assertEqual(stored[FT]["latest_run"], before["latest_run"])
        self.assertEqual(stored[FT]["health"], before["health"])


if __name__ == "__main__":
    unittest.main()
