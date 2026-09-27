"""Validate the shared SQLite snapshot without changing it (standard library only)."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import csv
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit

FIELDS = ("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language")
LOCAL_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\\\[^\\]+\\|/(?:Users|home)/)")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, f"Timezone missing: {value!r}")
    return parsed


def file_in_root(root: Path, relative: str) -> Path:
    require(not LOCAL_PATH.search(relative), "Artifact path must be relative")
    path = (root / relative).resolve()
    require(path.is_relative_to(root), f"Artifact escapes repository: {relative}")
    require(path.is_file(), f"Missing artifact: {relative}")
    return path


def validate(root: Path) -> dict:
    manifest_path = root / "data" / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    require(not LOCAL_PATH.search(manifest_text), "Local absolute path found in manifest")
    manifest = json.loads(manifest_text)
    require(manifest["format_version"] in (1, 2), "Unsupported manifest format")
    for artifact in manifest["artifacts"]:
        path = file_in_root(root, artifact["path"])
        require(path.stat().st_size == artifact["bytes"], f"Size mismatch: {artifact['path']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        require(digest == artifact["sha256"], f"SHA-256 mismatch: {artifact['path']}")

    database = file_in_root(root, manifest["database"])
    csv_path = file_in_root(root, manifest["csv"])
    require(not LOCAL_PATH.search(database.read_bytes().decode("utf-8", errors="ignore")),
            "Local absolute path found in SQLite file")
    window = manifest["publication_window"]
    start, end = timestamp(window["start"]), timestamp(window["end"])
    require(start < end and window["inclusive"] is True, "Invalid publication window")
    require(end - start == timedelta(hours=manifest["window_hours"]), "Window duration mismatch")
    if manifest["format_version"] == 2:
        require(manifest["collection_mode"] == "cumulative", "Invalid collection mode")
        require(manifest["daily_lookback_hours"] == 48, "Unexpected daily lookback")
        seed = manifest["seed_window"]
        seed_start, seed_end = timestamp(seed["start"]), timestamp(seed["end"])
        require(seed["inclusive"] is True and seed_end - seed_start == timedelta(hours=48),
                "Invalid seed window")
        require(start == seed_start and end >= seed_end, "Cumulative window excludes seed window")
        target, total = manifest["target_articles"], manifest["total_articles"]
        require(type(target) is int and target > 0, "Invalid article target")
        require(type(total) is int and 0 <= total <= target, "Article count exceeds target or is invalid")
        completed = total == target
        require(manifest["target_reached"] is completed, "Target flag differs from article count")
        require(manifest["automatic_refresh"] is (not completed), "Refresh flag differs from article count")
        require(type(manifest["remaining_articles"]) is int and manifest["remaining_articles"] == target - total,
                "Remaining article count differs")
        required_artifacts = {manifest["database"], manifest["csv"], "data/progress.json"}
        required_artifacts.update(detail["report"] for detail in manifest["sources"].values())
        require(required_artifacts <= {artifact["path"] for artifact in manifest["artifacts"]},
                "Cumulative artifacts are not all hashed")
        progress = json.loads(file_in_root(root, "data/progress.json").read_text(encoding="utf-8"))
        for field, expected in (("target_articles", target), ("total_articles", total), ("remaining_articles", target - total)):
            require(type(progress[field]) is int and progress[field] == expected,
                    f"Progress {field} differs from manifest")
        require(progress["completed"] is completed, "Progress completion differs from manifest")
        latest = manifest["latest_run"]
        require(latest is None or isinstance(latest, dict), "Invalid latest run")
        if latest is not None:
            require(isinstance(latest["status"], str) and bool(latest["status"].strip()), "Missing latest run status")
            run_start, run_end = timestamp(latest["start"]), timestamp(latest["end"])
            require(run_end - run_start == timedelta(hours=manifest["daily_lookback_hours"]) and run_end <= end,
                    "Invalid latest run window")
            before, after, inserted = latest["before"], latest["after"], latest["inserted"]
            require(all(type(value) is int for value in (before, after, inserted)) and
                    0 <= before <= after == total and inserted == after - before,
                    "Latest run counts differ from manifest")
            require(isinstance(latest["sources"], dict), "Invalid latest source reports")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        require([row[0] for row in db.execute("PRAGMA integrity_check")] == ["ok"], "SQLite integrity check failed")
        require(tuple(row["name"] for row in db.execute("PRAGMA table_info(news)")) == FIELDS, "Unexpected news columns")
        require(all(row["type"].upper() == "TEXT" for row in db.execute("PRAGMA table_info(news)")), "Unexpected column types")
        rows = [dict(row) for row in db.execute("SELECT * FROM news ORDER BY publish_time DESC, url")]
        require(len(rows) == manifest["total_articles"], "Article count differs from manifest")
        require(len({row["url"] for row in rows}) == len(rows), "Duplicate URLs")
        require(len({row["article_id"] for row in rows}) == len(rows), "Duplicate article identifiers")
        counts = dict(Counter(row["source"] for row in rows))
        expected_counts = {source: detail["article_count"] for source, detail in manifest["sources"].items()}
        require(counts == expected_counts, "Source counts differ from manifest")
        for row in rows:
            require(all(isinstance(row[field], str) and row[field].strip() for field in FIELDS),
                    f"Empty field in article {row['article_id']}")
            require(all("\ufffd" not in row[field] for field in FIELDS), f"Replacement character in {row['article_id']}")
            parts = urlsplit(row["url"])
            require(parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password,
                    f"Invalid URL: {row['url']}")
            canonical = urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))
            require(canonical == row["url"], f"Noncanonical URL: {row['url']}")
            require(row["article_id"] == hashlib.sha256(row["url"].encode("utf-8")).hexdigest()[:32], "Article identifier mismatch")
            published, crawled = timestamp(row["publish_time"]), timestamp(row["crawl_time"])
            require(start <= published <= end, f"Publication outside window: {row['url']}")
            require(published.utcoffset() == timedelta(hours=8) and crawled.utcoffset() == timedelta(hours=8), "Unexpected time zone")
            require(crawled >= published, f"Crawl precedes publication: {row['url']}")
        info = dict(db.execute("SELECT key, value FROM collection_info"))
        require(not any(LOCAL_PATH.search(value) for value in info.values()), "Local absolute path in collection_info")
        require(timestamp(info["requested_start"]) == start and timestamp(info["requested_end"]) == end, "Stored window differs from manifest")
        require(int(info["total_articles"]) == len(rows), "Stored article count differs")
        stored_reports = json.loads(info["source_reports"])
        for source, detail in manifest["sources"].items():
            report_path = file_in_root(root, detail["report"])
            require(stored_reports[source]["report_path"] == detail["report"], "Source report path mismatch")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            require(report["source"] == source and report["saved_full_text_articles"] == counts[source], "Source report count mismatch")
            source_rows = [row for row in rows if row["source"] == source]
            earliest = min(timestamp(row["publish_time"]) for row in source_rows)
            latest = max(timestamp(row["publish_time"]) for row in source_rows)
            require(earliest == timestamp(detail["earliest_publish_time"]) and latest == timestamp(detail["latest_publish_time"]), "Source time range mismatch")
        summaries = {row["source"]: row["article_count"] for row in db.execute("SELECT * FROM source_summary")}
        require(summaries == counts, "source_summary view mismatch")
        for view, source in (("bbc_news", "BBC News"), ("sina_news", "新浪财经")):
            require(db.execute(f"SELECT count(*) FROM {view}").fetchone()[0] == counts[source], f"{view} count mismatch")
        require(db.execute("SELECT count(*) FROM raw_news").fetchone()[0] == len(rows), "raw_news count mismatch")

    require(csv_path.read_bytes().startswith(b"\xef\xbb\xbf"), "CSV requires UTF-8 BOM")
    csv.field_size_limit(10_000_000)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        require(tuple(reader.fieldnames or ()) == FIELDS, "CSV header mismatch")
        csv_rows = list(reader)
    require(csv_rows == rows, "CSV records differ from SQLite")
    return {"status": "ok", "total_articles": len(rows), "source_counts": counts,
            "publication_window": window, "integrity_check": "ok", "csv_matches_database": True,
            "artifact_hashes_verified": len(manifest["artifacts"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = validate(args.root.resolve())
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, csv.Error) as exc:
        print(f"Validation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
