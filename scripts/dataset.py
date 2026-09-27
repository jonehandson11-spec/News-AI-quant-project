"""Regenerate portable cumulative SQLite/CSV metadata, using only the stdlib."""
from __future__ import annotations

from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

BEIJING = timezone(timedelta(hours=8))
FIELDS = ("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language")
SOURCES = {"BBC News": "bbc", "新浪财经": "sina"}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("A timezone-aware datetime is required")
    return value.astimezone(BEIJING).isoformat()


def next_scheduled(now: datetime) -> str:
    local = now.astimezone(BEIJING)
    upcoming = local.replace(hour=20, minute=0, second=0, microsecond=0)
    if upcoming <= local:
        upcoming += timedelta(days=1)
    return upcoming.isoformat()


def refresh(root: Path, *, run: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    manifest = read_json(root / "data/manifest.json")
    config = read_json(root / "crawl_config.json")
    target = config["target_articles"]
    seed = manifest.get("seed_window", manifest["publication_window"])
    previous_end = datetime.fromisoformat(manifest["publication_window"]["end"])
    new_end = max(previous_end, datetime.fromisoformat(run["end"])) if run else previous_end
    window = {"start": seed["start"], "end": iso(new_end), "inclusive": True}
    database = root / "data/news.sqlite3"
    reports = {}
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute("SELECT * FROM news ORDER BY publish_time DESC, url")]
        total = len(rows)
        if total > target:
            raise ValueError("Database already exceeds the configured target; no rows will be removed")
        for source, slug in SOURCES.items():
            source_rows = [row for row in rows if row["source"] == source]
            detail = manifest["sources"][source]
            detail.update(article_count=len(source_rows),
                          earliest_publish_time=min((row["publish_time"] for row in source_rows), default=None),
                          latest_publish_time=max((row["publish_time"] for row in source_rows), default=None))
            report_path = root / detail["report"]
            report = read_json(report_path)
            report["saved_full_text_articles"] = len(source_rows)
            report["collection_mode"] = "cumulative"
            report["publication_window"] = window
            if run and source in run.get("sources", {}):
                report["latest_run"] = run["sources"][source]
            write_json(report_path, report)
            reports[source] = dict(report, report_path=detail["report"])

        latest_run = run if run is not None else manifest.get("latest_run")
        completed = total >= target
        progress = {
            "target_articles": target, "total_articles": total,
            "remaining_articles": max(0, target - total), "completed": completed,
            "schedule": config["schedule"], "latest_run": latest_run,
            "next_scheduled_at": None if completed else next_scheduled(now),
        }
        summary = [dict(row) for row in db.execute("SELECT * FROM source_summary")]
        updates = {
            "requested_start": window["start"], "requested_end": window["end"],
            "collection_mode": "cumulative", "target_articles": target,
            "total_articles": total, "source_reports": reports, "summary": summary,
            "source_counts": {source: {"total_articles": reports[source]["saved_full_text_articles"]} for source in SOURCES},
            "filters": "Cumulative unique URLs; original page publication time; new daily articles must fall within the configured lookback window",
            "updated_at": iso(now), "latest_run": latest_run,
        }
        db.executemany("INSERT OR REPLACE INTO collection_info(key,value) VALUES (?,?)", [
            (key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
            for key, value in updates.items()
        ])
        db.commit()
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
    csv_path = root / "data/news.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    write_json(root / "data/progress.json", progress)
    manifest.update(
        format_version=2, collection_mode="cumulative", seed_window=seed,
        publication_window=window,
        window_hours=(new_end - datetime.fromisoformat(seed["start"])).total_seconds() / 3600,
        daily_lookback_hours=config["daily_lookback_hours"], target_articles=target,
        target_reached=completed, remaining_articles=max(0, target - total),
        automatic_refresh=not completed, total_articles=total,
        schedule=config["schedule"], latest_run=latest_run,
        package_created_at=now.isoformat(),
        title="BBC + Sina Finance cumulative news database",
        filters=updates["filters"],
        coverage_limit="Cumulative BBC World RSS and Sina Finance roll-feed articles; each daily run discovers recent articles, not a complete archive",
    )
    artifact_paths = ["data/news.sqlite3", "data/news.csv", "data/source_reports/bbc.json",
                      "data/source_reports/sina.json", "data/progress.json", "schema.sql"]
    manifest["artifacts"] = [
        {"path": relative, "bytes": (root / relative).stat().st_size,
         "sha256": hashlib.sha256((root / relative).read_bytes()).hexdigest()}
        for relative in artifact_paths
    ]
    write_json(root / "data/manifest.json", manifest)
    return progress
