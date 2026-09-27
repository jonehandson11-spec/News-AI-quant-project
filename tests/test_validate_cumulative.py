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
        paths = ["data/news.sqlite3", "data/news.csv", "data/source_reports/bbc.json", "data/source_reports/sina.json", "schema.sql"]
        if (self.root / "data/progress.json").exists():
            paths.append("data/progress.json")
        self.manifest["artifacts"] = [{"path": path, "bytes": (self.root / path).stat().st_size,
                                      "sha256": hashlib.sha256((self.root / path).read_bytes()).hexdigest()} for path in paths]
        self.write_json("data/manifest.json", self.manifest)

    def cumulative(self, target=3000):
        self.manifest.update({"format_version": 2, "collection_mode": "cumulative", "seed_window": dict(self.window),
                              "daily_lookback_hours": 48, "target_articles": target, "target_reached": target == 2,
                              "automatic_refresh": target != 2, "remaining_articles": target - 2, "latest_run": None})
        self.manifest["publication_window"]["end"] = "2026-09-28T20:00:00+08:00"
        self.manifest["window_hours"] = 72
        with closing(sqlite3.connect(self.root / "data/news.sqlite3")) as db, db:
            db.execute("UPDATE collection_info SET value=? WHERE key='requested_end'", (self.manifest["publication_window"]["end"],))
        self.write_json("data/progress.json", {"target_articles": target, "total_articles": 2, "remaining_articles": target - 2, "completed": target == 2})
        self.save()

    def test_original_snapshot_still_validates_read_only(self):
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


if __name__ == "__main__":
    unittest.main()
