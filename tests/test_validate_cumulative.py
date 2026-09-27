"""Check old snapshots and cumulative metadata without touching shared data."""
from __future__ import annotations

import csv
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from validate_database import FIELDS, validate


class ValidateCumulativeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / "data" / "source_reports").mkdir(parents=True)
        (self.root / "schema.sql").write_text((ROOT / "schema.sql").read_text(encoding="utf-8"), encoding="utf-8")
        self.window = {"start": "2026-09-25T20:00:00+08:00", "end": "2026-09-27T20:00:00+08:00", "inclusive": True}
        self.rows = []
        sources = {}
        stored_reports = {}
        for source, name, host, language in (("BBC News", "bbc", "www.bbc.com", "en"),
                                               ("新浪财经", "sina", "finance.sina.com.cn", "zh-CN")):
            url = f"https://{host}/article"
            row = dict(zip(FIELDS, (hashlib.sha256(url.encode()).hexdigest()[:32], source, "A title", "Article full text",
                                    "2026-09-26T20:00:00+08:00", "2026-09-27T20:00:00+08:00", url, language)))
            self.rows.append(row)
            report_path = f"data/source_reports/{name}.json"
            self.write_json(report_path, {"source": source, "saved_full_text_articles": 1})
            sources[source] = {"article_count": 1, "earliest_publish_time": row["publish_time"],
                               "latest_publish_time": row["publish_time"], "report": report_path}
            stored_reports[source] = {"report_path": report_path}
        self.rows.sort(key=lambda row: row["url"])
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.executescript((self.root / "schema.sql").read_text(encoding="utf-8"))
            db.executemany("INSERT INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [tuple(row[field] for field in FIELDS) for row in self.rows])
            db.executemany("INSERT INTO collection_info VALUES (?, ?)", [
                ("requested_start", self.window["start"]), ("requested_end", self.window["end"]),
                ("total_articles", "2"), ("source_reports", json.dumps(stored_reports))])
        with (self.root / "data/news.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)
        self.manifest = {"format_version": 1, "database": "data/news.sqlite3", "csv": "data/news.csv",
                         "publication_window": dict(self.window), "window_hours": 48, "total_articles": 2,
                         "sources": sources, "artifacts": []}
        self.save()

    def write_json(self, relative, value):
        (self.root / relative).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def save(self):
        paths = ["data/news.sqlite3", "data/news.csv", "schema.sql"]
        paths.extend(detail["report"] for detail in self.manifest["sources"].values())
        if (self.root / "data/progress.json").exists():
            paths.append("data/progress.json")
        self.manifest["artifacts"] = [{"path": path, "bytes": (self.root / path).stat().st_size,
                                      "sha256": hashlib.sha256((self.root / path).read_bytes()).hexdigest()} for path in paths]
        self.write_json("data/manifest.json", self.manifest)

    def cumulative(self, target=3000):
        total = self.manifest["total_articles"]
        self.manifest.update({"format_version": 2, "collection_mode": "cumulative", "seed_window": dict(self.window),
                              "daily_lookback_hours": 48, "target_articles": target, "target_reached": target == total,
                              "automatic_refresh": target != total, "remaining_articles": target - total, "latest_run": None})
        self.manifest["publication_window"]["end"] = "2026-09-28T20:00:00+08:00"
        self.manifest["window_hours"] = 72
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("UPDATE collection_info SET value=? WHERE key='requested_end'", (self.manifest["publication_window"]["end"],))
        self.write_json("data/progress.json", {"target_articles": target, "total_articles": total, "remaining_articles": target - total, "completed": target == total})
        self.save()

    def add_ft(self, *, empty=False, url="https://www.ft.com/content/12345678-abcd-4567-89ab-0123456789ab"):
        source = "Financial Times"
        published = None if empty else "2026-09-26T20:00:00+08:00"
        report_path = "data/source_reports/ft.json"
        self.manifest["sources"][source] = {"article_count": 0 if empty else 1,
                                            "earliest_publish_time": published,
                                            "latest_publish_time": published, "report": report_path}
        self.write_json(report_path, {"source": source, "saved_full_text_articles": 0 if empty else 1,
                                      "health": {"status": "auth_required" if empty else "healthy",
                                                 "needs_attention": empty}})
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("CREATE VIEW IF NOT EXISTS ft_news AS SELECT * FROM news WHERE source='Financial Times'")
            if not empty:
                row = dict(zip(FIELDS, (hashlib.sha256(url.encode()).hexdigest()[:32], source,
                                       "News about cookies and passwords", "The report discusses the word authorization.",
                                       published, "2026-09-27T20:00:00+08:00", url, "en")))
                self.rows.append(row)
                db.execute("INSERT INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row[field] for field in FIELDS))
            stored = json.loads(db.execute("SELECT value FROM collection_info WHERE key='source_reports'").fetchone()[0])
            stored[source] = {"report_path": report_path}
            db.execute("UPDATE collection_info SET value=? WHERE key='source_reports'", (json.dumps(stored),))
            db.execute("UPDATE collection_info SET value=? WHERE key='total_articles'", (str(len(self.rows)),))
        self.rows.sort(key=lambda row: row["url"])
        with (self.root / "data/news.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(self.rows)
        self.manifest["total_articles"] = len(self.rows)
        self.save()

    def test_original_snapshot_still_validates_read_only(self):
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("DROP VIEW IF EXISTS ft_news")
        self.save()
        before = (self.root / "data/news.sqlite3").read_bytes()
        self.assertEqual(validate(self.root)["total_articles"], 2)
        self.assertEqual((self.root / "data/news.sqlite3").read_bytes(), before)

    def test_cumulative_window_can_exceed_48_hours(self):
        self.cumulative()
        self.assertEqual(validate(self.root)["total_articles"], 2)

    def test_completed_target_disables_refresh(self):
        self.cumulative(target=2)
        self.assertEqual(validate(self.root)["status"], "ok")
        self.manifest["automatic_refresh"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "Refresh flag"):
            validate(self.root)

    def test_target_cannot_be_exceeded(self):
        self.cumulative(target=1)
        with self.assertRaisesRegex(ValueError, "exceeds target"):
            validate(self.root)

    def test_progress_must_match_even_when_hashes_match(self):
        self.cumulative()
        self.write_json("data/progress.json", {"target_articles": 3000, "total_articles": 3, "remaining_articles": 2997, "completed": False})
        self.save()
        with self.assertRaisesRegex(ValueError, "Progress total"):
            validate(self.root)

    def test_progress_must_be_hashed(self):
        self.cumulative()
        self.manifest["artifacts"] = [artifact for artifact in self.manifest["artifacts"] if artifact["path"] != "data/progress.json"]
        self.write_json("data/manifest.json", self.manifest)
        with self.assertRaisesRegex(ValueError, "not all hashed"):
            validate(self.root)

    def test_latest_run_counts_and_window_are_checked(self):
        self.cumulative()
        self.manifest["latest_run"] = {"status": "ok", "start": "2026-09-26T20:00:00+08:00",
                                      "end": "2026-09-28T20:00:00+08:00", "before": 1, "after": 2,
                                      "inserted": 1, "sources": {}}
        self.save()
        self.assertEqual(validate(self.root)["status"], "ok")
        self.manifest["latest_run"]["inserted"] = 2
        self.save()
        with self.assertRaisesRegex(ValueError, "Latest run counts"):
            validate(self.root)

    def test_three_source_cumulative_database(self):
        self.add_ft()
        self.cumulative()
        result = validate(self.root)
        self.assertEqual(result["total_articles"], 3)
        self.assertEqual(result["source_counts"], {"BBC News": 1, "新浪财经": 1, "Financial Times": 1})

    def test_enabled_ft_source_may_be_empty(self):
        self.add_ft(empty=True)
        self.cumulative()
        result = validate(self.root)
        self.assertEqual(result["total_articles"], 2)
        self.assertEqual(result["source_counts"]["Financial Times"], 0)

    def test_empty_ft_requires_null_dates(self):
        self.add_ft(empty=True)
        self.manifest["sources"]["Financial Times"]["earliest_publish_time"] = self.window["start"]
        self.save()
        with self.assertRaisesRegex(ValueError, "Empty source"):
            validate(self.root)

    def test_empty_ft_report_must_match_zero_count(self):
        self.add_ft(empty=True)
        self.write_json("data/source_reports/ft.json", {"source": "Financial Times", "saved_full_text_articles": 1})
        self.save()
        with self.assertRaisesRegex(ValueError, "Source report count"):
            validate(self.root)

    def test_ft_cannot_use_another_sources_domain(self):
        self.add_ft(url="https://www.bbc.com/content/12345678-abcd-4567-89ab-0123456789ab")
        with self.assertRaisesRegex(ValueError, "outside the article source"):
            validate(self.root)

    def test_ft_requires_content_article_path(self):
        self.add_ft(url="https://www.ft.com/subscription")
        with self.assertRaisesRegex(ValueError, "Invalid FT content URL"):
            validate(self.root)

    def test_ft_view_required_only_when_configured(self):
        self.add_ft(empty=True)
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("DROP VIEW ft_news")
        self.save()
        with self.assertRaisesRegex(ValueError, "Missing ft_news"):
            validate(self.root)

    def test_ft_view_count_must_match(self):
        self.add_ft(empty=True)
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("DROP VIEW ft_news")
            db.execute("CREATE VIEW ft_news AS SELECT * FROM news WHERE source='BBC News'")
        self.save()
        with self.assertRaisesRegex(ValueError, "ft_news count"):
            validate(self.root)

    def test_unknown_manifest_source_rejected_even_when_empty(self):
        self.add_ft(empty=True)
        self.manifest["sources"]["Unknown publisher"] = self.manifest["sources"].pop("Financial Times")
        self.save()
        with self.assertRaisesRegex(ValueError, "Unknown configured source"):
            validate(self.root)

    def test_unconfigured_article_source_rejected(self):
        self.add_ft()
        del self.manifest["sources"]["Financial Times"]
        self.save()
        with self.assertRaisesRegex(ValueError, "unconfigured article source"):
            validate(self.root)

    def test_credentials_rejected_in_metadata_but_words_allowed_in_news(self):
        self.add_ft()
        self.assertEqual(validate(self.root)["status"], "ok")
        self.write_json("data/source_reports/ft.json", {"source": "Financial Times", "saved_full_text_articles": 1,
                                                       "health": {"cookie": "dummy-secret"}})
        self.save()
        with self.assertRaisesRegex(ValueError, "Credential value"):
            validate(self.root)


if __name__ == "__main__":
    unittest.main()
