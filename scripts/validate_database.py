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
SOURCE_DOMAINS = {
    "BBC News": ("bbc.com", "bbc.co.uk"),
    "新浪财经": ("sina.com.cn", "sina.cn"),
    "Financial Times": ("www.ft.com",),
}
FT_CONTENT_PATH = re.compile(r"/content/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
SECRET_FIELDS = {"cookie", "cookies", "cookie_header", "authorization", "password",
                 "access_token", "refresh_token", "session_token", "credentials", "ft_cookie", "ft_cookies"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, f"Timezone missing: {value!r}")
    return parsed


def check_metadata(value) -> None:
    """Reject credential fields without searching article text for ordinary words."""
    if isinstance(value, dict):
        for key, item in value.items():
            require(str(key).lower().replace("-", "_") not in SECRET_FIELDS or not item,
                    "Credential value found in metadata")
            check_metadata(item)
    elif isinstance(value, list):
        for item in value:
            check_metadata(item)


def file_in_root(root: Path, relative: str) -> Path:
    require(not LOCAL_PATH.search(relative), "Artifact path must be relative")
    path = (root / relative).resolve()
    require(path.is_relative_to(root), f"Artifact escapes repository: {relative}")
    require(path.is_file(), f"Missing artifact: {relative}")
    return path


def run_lookback(latest: dict) -> int:
    """Only explicitly marked local FT backfills may extend the daily window."""
    hours = latest.get("lookback_hours", 48)
    mode = latest.get("collection_mode", "daily")
    require(type(hours) is int and hours in (48, 120), "Invalid latest run lookback")
    if hours == 120 or mode == "backfill":
        require(hours == 120 and mode == "backfill", "FT backfill requires explicit 120-hour markers")
        require(latest.get("scope") == "selected"
                and latest.get("selected_sources") == ["Financial Times"]
                and latest.get("execution_location") == "local",
                "FT backfill must select only local Financial Times")
        batch = latest.get("local_batch")
        require(isinstance(batch, dict) and type(batch.get("lookback_hours")) is int
                and batch["lookback_hours"] == 120 and batch.get("collection_mode") == "backfill",
                "FT backfill local batch markers differ")
        sources = latest["sources"]
        require(set(sources) <= {"Financial Times"}, "FT backfill contains another source")
        require(bool(sources) or latest.get("health_preserved") is True,
                "FT backfill source report is missing")
        if sources:
            report = sources["Financial Times"]
            require(isinstance(report, dict) and report.get("source") == "Financial Times"
                    and report.get("execution_location") == "local"
                    and type(report.get("lookback_hours")) is int
                    and report["lookback_hours"] == 120
                    and report.get("collection_mode") == "backfill",
                    "FT backfill source markers differ")
    else:
        require(mode == "daily", "Invalid latest run collection mode")
    return hours


def validate(root: Path) -> dict:
    manifest_path = root / "data" / "manifest.json"
    manifest_text = manifest_path.read_text(encoding="utf-8")
    require(not LOCAL_PATH.search(manifest_text), "Local absolute path found in manifest")
    manifest = json.loads(manifest_text)
    check_metadata(manifest)
    require(manifest["format_version"] in (1, 2), "Unsupported manifest format")
    require(isinstance(manifest["sources"], dict) and bool(manifest["sources"]), "No configured sources")
    require(set(manifest["sources"]) <= SOURCE_DOMAINS.keys(), "Unknown configured source")
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
        check_metadata(progress)
        for field, expected in (("target_articles", target), ("total_articles", total), ("remaining_articles", target - total)):
            require(type(progress[field]) is int and progress[field] == expected,
                    f"Progress {field} differs from manifest")
        require(progress["completed"] is completed, "Progress completion differs from manifest")
        latest = manifest["latest_run"]
        require(latest is None or isinstance(latest, dict), "Invalid latest run")
        if latest is not None:
            require(isinstance(latest["status"], str) and bool(latest["status"].strip()), "Missing latest run status")
            require(isinstance(latest["sources"], dict), "Invalid latest source reports")
            run_start, run_end = timestamp(latest["start"]), timestamp(latest["end"])
            require(run_end - run_start == timedelta(hours=run_lookback(latest)) and run_end <= end,
                    "Invalid latest run window")
            before, after, inserted = latest["before"], latest["after"], latest["inserted"]
            require(all(type(value) is int for value in (before, after, inserted)) and
                    0 <= before <= after == total and inserted == after - before,
                    "Latest run counts differ from manifest")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        require([row[0] for row in db.execute("PRAGMA integrity_check")] == ["ok"], "SQLite integrity check failed")
        require(tuple(row["name"] for row in db.execute("PRAGMA table_info(news)")) == FIELDS, "Unexpected news columns")
        require(all(row["type"].upper() == "TEXT" for row in db.execute("PRAGMA table_info(news)")), "Unexpected column types")
        rows = [dict(row) for row in db.execute("SELECT * FROM news ORDER BY publish_time DESC, url")]
        require(len(rows) == manifest["total_articles"], "Article count differs from manifest")
        require(len({row["url"] for row in rows}) == len(rows), "Duplicate URLs")
        require(len({row["article_id"] for row in rows}) == len(rows), "Duplicate article identifiers")
        actual_counts = dict(Counter(row["source"] for row in rows))
        expected_counts = {source: detail["article_count"] for source, detail in manifest["sources"].items()}
        require(set(actual_counts) <= expected_counts.keys(), "Unknown or unconfigured article source")
        require(all(type(count) is int and count >= 0 for count in expected_counts.values()), "Invalid source article count")
        counts = {source: actual_counts.get(source, 0) for source in expected_counts}
        require(counts == expected_counts, "Source counts differ from manifest")
        for row in rows:
            require(all(isinstance(row[field], str) and row[field].strip() for field in FIELDS),
                    f"Empty field in article {row['article_id']}")
            require(all("\ufffd" not in row[field] for field in FIELDS), f"Replacement character in {row['article_id']}")
            parts = urlsplit(row["url"])
            require(parts.scheme == "https" and bool(parts.hostname) and not parts.username and not parts.password,
                    f"Invalid URL: {row['url']}")
            domains = SOURCE_DOMAINS[row["source"]]
            host = parts.hostname.lower()
            allowed_host = host == "www.ft.com" if row["source"] == "Financial Times" else any(
                host == domain or host.endswith("." + domain) for domain in domains)
            require(allowed_host and parts.port is None, f"URL is outside the article source: {row['url']}")
            if row["source"] == "Financial Times":
                require(FT_CONTENT_PATH.fullmatch(parts.path) is not None, f"Invalid FT content URL: {row['url']}")
            canonical = urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))
            require(canonical == row["url"], f"Noncanonical URL: {row['url']}")
            require(row["article_id"] == hashlib.sha256(row["url"].encode("utf-8")).hexdigest()[:32], "Article identifier mismatch")
            published, crawled = timestamp(row["publish_time"]), timestamp(row["crawl_time"])
            require(start <= published <= end, f"Publication outside window: {row['url']}")
            require(published.utcoffset() == timedelta(hours=8) and crawled.utcoffset() == timedelta(hours=8), "Unexpected time zone")
            require(crawled >= published, f"Crawl precedes publication: {row['url']}")
        info = dict(db.execute("SELECT key, value FROM collection_info"))
        check_metadata(info)
        require(not any(LOCAL_PATH.search(value) for value in info.values()), "Local absolute path in collection_info")
        require(timestamp(info["requested_start"]) == start and timestamp(info["requested_end"]) == end, "Stored window differs from manifest")
        require(int(info["total_articles"]) == len(rows), "Stored article count differs")
        stored_reports = json.loads(info["source_reports"])
        check_metadata(stored_reports)
        for source, detail in manifest["sources"].items():
            report_path = file_in_root(root, detail["report"])
            require(stored_reports[source]["report_path"] == detail["report"], "Source report path mismatch")
            report = json.loads(report_path.read_text(encoding="utf-8"))
            check_metadata(report)
            require(report["source"] == source and report["saved_full_text_articles"] == counts[source], "Source report count mismatch")
            source_rows = [row for row in rows if row["source"] == source]
            if source_rows:
                earliest = min(timestamp(row["publish_time"]) for row in source_rows)
                latest = max(timestamp(row["publish_time"]) for row in source_rows)
                require(earliest == timestamp(detail["earliest_publish_time"]) and latest == timestamp(detail["latest_publish_time"]), "Source time range mismatch")
            else:
                require(detail["earliest_publish_time"] is None and detail["latest_publish_time"] is None,
                        "Empty source must have null publication range")
        summaries = {row["source"]: row["article_count"] for row in db.execute("SELECT * FROM source_summary")}
        require(summaries == {source: count for source, count in counts.items() if count}, "source_summary view mismatch")
        views = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
        for view, source in (("bbc_news", "BBC News"), ("sina_news", "新浪财经"), ("ft_news", "Financial Times")):
            if source in counts:
                require(view in views, f"Missing {view} view")
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
