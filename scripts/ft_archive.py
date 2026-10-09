"""Prepare an offline FT archive batch and append it to the latest cloud snapshot."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dataset import BEIJING, FIELDS, iso, read_json, refresh, write_json
from scripts.validate_database import validate

SOURCE = "Financial Times"
MAX_RECORDS = 2000
MAX_BATCH_BYTES = 25 * 1024 * 1024
SOURCE_FIELDS = ("title", "publish_time", "crawled_time", "source_website", "url", "content")
CONTENT_PATH = re.compile(r"/content/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
HASH = re.compile(r"[0-9a-f]{64}")
# Match the live parser's prose checks without importing its HTTP/parser dependencies.
PAYWALL_PHRASES = ("subscribe to unlock", "range of subscriptions", "discover all the plans",
                   "sign in to continue", "what our readers say", "brief with no waffle")
REJECTION_REASONS = frozenset(("invalid_fields", "invalid_source", "invalid_timestamp",
                               "invalid_url", "short_content", "promotional_content",
                               "duplicate_url", "duplicate_content"))
PUBLICATION_BASIS = "Supplied archive RSS timestamps in Asia/Shanghai; not reverified against article pages"
BATCH_FIELDS = frozenset(("format_version", "kind", "source", "source_file", "source_sha256",
                          "source_records", "rejected_counts", "rows", "batch_id"))


class ArchiveError(ValueError):
    """Only fixed reason codes may leave the command-line boundary."""


def _require(condition, reason):
    if not condition:
        raise ArchiveError(reason)


def _clock(now=None):
    now = now or datetime.now(timezone.utc)
    _require(isinstance(now, datetime) and now.tzinfo is not None, "invalid_clock")
    return now


def _external(path, root):
    path = Path(path).resolve()
    _require(not path.is_relative_to(root.resolve()), "archive_file_must_be_external")
    return path


def _digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def batch_id(batch):
    payload = {key: value for key, value in batch.items() if key != "batch_id"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _content_key(content):
    return hashlib.sha256(" ".join(content.split()).encode("utf-8")).digest()


def _timestamp(value, *, allow_naive=False):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None and allow_naive:
            parsed = parsed.replace(tzinfo=BEIJING)
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None, "invalid_timestamp")
        return parsed
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise ArchiveError("invalid_timestamp") from None


def _url(value):
    try:
        parts = urlsplit(value)
        _require(parts.scheme == "https" and parts.hostname == "www.ft.com"
                 and parts.username is None and parts.password is None and parts.port is None
                 and CONTENT_PATH.fullmatch(parts.path) is not None, "invalid_url")
        return urlunsplit(("https", "www.ft.com", parts.path.lower(), "", ""))
    except ValueError:
        raise ArchiveError("invalid_url") from None


def _row(candidate, now, *, archive=False):
    keys = SOURCE_FIELDS if archive else FIELDS
    _require(isinstance(candidate, dict) and set(candidate) == set(keys), "invalid_fields")
    _require(all(isinstance(candidate[key], str) and candidate[key].strip()
                 and "\ufffd" not in candidate[key] for key in keys), "invalid_fields")
    source = candidate["source_website" if archive else "source"]
    _require(source == SOURCE and (archive or candidate["language"] == "en"), "invalid_source")
    content = candidate["content"]
    _require(len(content.strip()) >= 600, "short_content")
    lowered = " ".join(content.lower().split())
    hits = sum(phrase in lowered for phrase in PAYWALL_PHRASES)
    _require(hits < 2 and "subscribe to unlock" not in lowered and "sign in to continue" not in lowered,
             "promotional_content")
    published = _timestamp(candidate["publish_time"], allow_naive=archive)
    crawled = _timestamp(candidate["crawled_time" if archive else "crawl_time"], allow_naive=archive)
    _require(published <= crawled <= now, "invalid_timestamp")
    url = _url(candidate["url"])
    row = {"article_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32], "source": SOURCE,
           "title": candidate["title"], "content": content, "publish_time": iso(published),
           "crawl_time": iso(crawled), "url": url, "language": "en"}
    if not archive:
        _require(candidate == row and published.utcoffset() == timedelta(hours=8)
                 and crawled.utcoffset() == timedelta(hours=8), "invalid_batch_row")
    return row


def prepare_batch(root, database, batch_file, *, now=None):
    """Read the supplied archive in SQLite read-only mode; write only an external batch."""
    root, now = Path(root).resolve(), _clock(now)
    database, batch_file = _external(database, root), _external(batch_file, root)
    _require(database != batch_file, "batch_cannot_replace_source")
    validate(root)
    source_sha256 = _digest_file(database)
    rejected = Counter()
    rows, urls, content_keys = [], set(), set()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        _require([row[0] for row in db.execute("PRAGMA integrity_check")] == ["ok"],
                 "source_integrity_check_failed")
        db.row_factory = sqlite3.Row
        _require([row["name"] for row in db.execute("PRAGMA table_info(articles)")] == list(SOURCE_FIELDS),
                 "invalid_source_schema")
        records = db.execute("SELECT count(*) FROM articles").fetchone()[0]
        _require(0 < records <= MAX_RECORDS, "invalid_source_record_count")
        for original in db.execute("SELECT * FROM articles ORDER BY url"):
            try:
                row = _row(dict(original), now, archive=True)
                key = _content_key(row["content"])
                _require(row["url"] not in urls, "duplicate_url")
                _require(key not in content_keys, "duplicate_content")
            except ArchiveError as error:
                rejected[str(error)] += 1
                continue
            urls.add(row["url"])
            content_keys.add(key)
            rows.append(row)
    _require(_digest_file(database) == source_sha256, "source_changed_during_read")
    _require(bool(rows), "no_acceptable_articles")
    batch = {"format_version": 1, "kind": "ft_archive", "source": SOURCE, "source_file": "ft_news.db",
             "source_sha256": source_sha256, "source_records": records,
             "rejected_counts": dict(sorted(rejected.items())), "rows": rows}
    batch["batch_id"] = batch_id(batch)
    _require(len(json.dumps(batch, ensure_ascii=False, indent=2).encode("utf-8")) + 1 <= MAX_BATCH_BYTES,
             "batch_too_large")
    write_json(batch_file, batch)
    return {"status": "prepared", "batch_id": batch["batch_id"], "source_records": records,
            "accepted_records": len(rows), "rejected_counts": batch["rejected_counts"],
            "source_sha256": source_sha256}


def _validated_batch(batch_file, now):
    _require(batch_file.stat().st_size <= MAX_BATCH_BYTES, "batch_too_large")
    batch = read_json(batch_file)
    _require(isinstance(batch, dict) and set(batch) == BATCH_FIELDS, "invalid_batch")
    _require(type(batch["format_version"]) is int and batch["format_version"] == 1
             and batch["kind"] == "ft_archive" and batch["source"] == SOURCE
             and batch["source_file"] == "ft_news.db", "invalid_batch")
    _require(isinstance(batch["source_sha256"], str) and HASH.fullmatch(batch["source_sha256"]) is not None
             and isinstance(batch["batch_id"], str) and HASH.fullmatch(batch["batch_id"]) is not None
             and batch["batch_id"] == batch_id(batch), "invalid_batch_hash")
    rejected = batch["rejected_counts"]
    _require(isinstance(rejected, dict) and set(rejected) <= REJECTION_REASONS
             and all(type(count) is int and count > 0 for count in rejected.values()), "invalid_rejection_counts")
    records, rows = batch["source_records"], batch["rows"]
    _require(type(records) is int and 0 < records <= MAX_RECORDS and isinstance(rows, list)
             and 0 < len(rows) <= MAX_RECORDS and len(rows) + sum(rejected.values()) == records,
             "invalid_batch_counts")
    rows = [_row(row, now) for row in rows]
    return batch, rows


def merge_batch(root, batch_file, *, now=None):
    """Append a validated archive batch in cloud staging, preserving every existing row."""
    root, now = Path(root).resolve(), _clock(now)
    batch_file = _external(batch_file, root)
    validate(root)
    batch, rows = _validated_batch(batch_file, now)
    manifest, config = read_json(root / "data/manifest.json"), read_json(root / "crawl_config.json")
    imports = manifest.get("archive_imports", [])
    previous = next((entry for entry in imports if entry["import_id"] == batch["batch_id"]), None)
    if previous is not None:
        return {"status": "already_imported", "batch_id": batch["batch_id"],
                "before": manifest["total_articles"], "after": manifest["total_articles"],
                "inserted": 0, "duplicates": len(rows), "health_preserved": True}
    inserted_ids, duplicates = [], 0
    with closing(sqlite3.connect(root / "data/news.sqlite3")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT url, article_id, content FROM news").fetchall()
        before = len(existing)
        urls = {url for url, _, _ in existing}
        identifiers = {identifier for _, identifier, _ in existing}
        contents = {_content_key(content) for _, _, content in existing}
        for row in rows:
            content_key = _content_key(row["content"])
            if row["url"] in urls or row["article_id"] in identifiers or content_key in contents:
                duplicates += 1
                continue
            db.execute(f"INSERT INTO news({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})",
                       tuple(row[field] for field in FIELDS))
            inserted_ids.append(row["article_id"])
            urls.add(row["url"])
            identifiers.add(row["article_id"])
            contents.add(content_key)
    inserted, after = len(inserted_ids), before + len(inserted_ids)
    window = {"start": min(row["publish_time"] for row in rows),
              "end": max(row["publish_time"] for row in rows), "inclusive": True}
    report_path = f"data/import_reports/ft_archive_{batch['batch_id']}.json"
    entry = {"import_id": batch["batch_id"], "source": SOURCE, "source_file": batch["source_file"],
             "source_sha256": batch["source_sha256"], "source_records": batch["source_records"],
             "accepted_records": len(rows), "inserted": inserted, "duplicates": duplicates,
             "rejected_counts": batch["rejected_counts"], "publication_window": window,
             "imported_at": iso(now), "report": report_path, "publication_basis": PUBLICATION_BASIS}
    write_json(root / report_path, dict(entry, inserted_article_ids=inserted_ids))
    manifest["archive_imports"] = [*imports, entry]
    write_json(root / "data/manifest.json", manifest)
    config["target_articles"] = max(config["target_articles"], after)
    write_json(root / "crawl_config.json", config)
    run = {"collection_mode": "archive_import", "scope": "selected", "selected_sources": [SOURCE],
           "execution_location": "archive_import", "sources": {}, "health_preserved": True,
           "archive_import_id": batch["batch_id"], "started_at": iso(now), "finished_at": iso(now),
           "merged_at": iso(now), "start": window["start"], "end": window["end"],
           "before": before, "after": after, "inserted": inserted, "duplicates": duplicates,
           "status": "success", "target_reached": after >= config["target_articles"]}
    refresh(root, run=run, now=now)
    validate(root)
    return {"status": "success", "batch_id": batch["batch_id"], "before": before, "after": after,
            "inserted": inserted, "duplicates": duplicates, "health_preserved": True,
            "source_records": batch["source_records"], "accepted_records": len(rows),
            "rejected_counts": batch["rejected_counts"], "report": report_path}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "merge"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, default=ROOT)
        command.add_argument("--batch-file", type=Path, required=True)
        command.add_argument("--result-file", type=Path)
        if name == "prepare":
            command.add_argument("--database", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result_path = _external(args.result_file, args.root.resolve()) if args.result_file else None
        if result_path is not None:
            _require(result_path != args.batch_file.resolve()
                     and (args.command != "prepare" or result_path != args.database.resolve()),
                     "result_cannot_replace_input")
        result = (prepare_batch(args.root, args.database, args.batch_file) if args.command == "prepare"
                  else merge_batch(args.root, args.batch_file))
        if result_path is not None:
            write_json(result_path, result)
    except Exception as error:
        reason = str(error) if isinstance(error, ArchiveError) else "archive_operation_failed"
        print(json.dumps({"status": "failed", "reason": reason}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
