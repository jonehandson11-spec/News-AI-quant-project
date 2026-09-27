"""Collect one daily batch, keeping existing articles and stopping at the target."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.dataset import BEIJING, FIELDS, SOURCES, read_json, refresh, iso, write_json


def normalize(row: dict, source: str, start: datetime, end: datetime) -> dict:
    row = {key: row[key] for key in FIELDS}
    if row["source"] != source or any(not isinstance(value, str) or not value.strip() for value in row.values()):
        raise ValueError("Wrong source or empty article fields")
    published = datetime.fromisoformat(row["publish_time"].replace("Z", "+00:00"))
    crawled = datetime.fromisoformat(row["crawl_time"].replace("Z", "+00:00"))
    if published.tzinfo is None or crawled.tzinfo is None or not start <= published <= end:
        raise ValueError("Missing timezone or publication outside the daily window")
    if crawled < published or any("\ufffd" in value for value in row.values()):
        raise ValueError("Invalid crawl time or replacement characters")
    parts = urlsplit(row["url"])
    host = (parts.hostname or "").lower()
    permitted = ("bbc.com", "bbc.co.uk") if source == "BBC News" else ("sina.com.cn", "sina.cn")
    if parts.scheme not in {"https", "http"} or parts.username or parts.password or not any(host == domain or host.endswith("." + domain) for domain in permitted):
        raise ValueError("URL is outside the requested news source")
    row["url"] = urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))
    row["article_id"] = hashlib.sha256(row["url"].encode("utf-8")).hexdigest()[:32]
    row["publish_time"], row["crawl_time"] = iso(published), iso(crawled)
    return row


def safe_error(error: Exception) -> str:
    # Source reports are published; strip accidental local runner/workstation paths.
    message = str(error)[:500]
    message = re.sub(r"[A-Za-z]:[\\/][^\s'\"]+", "[local path]", message)
    message = re.sub(r"/(?:home/runner|Users|tmp)/[^\s'\"]+", "[local path]", message)
    return f"{type(error).__name__}: {message}"


def run_daily(root: Path, *, now: datetime | None = None, collectors=None,
              force: bool = False, max_new: int | None = None,
              per_source_limit: int | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    if now.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    manifest = read_json(root / "data/manifest.json")
    config = read_json(root / "crawl_config.json")
    target = config["target_articles"]
    start = now - timedelta(hours=config["daily_lookback_hours"])
    run = {"started_at": iso(now), "start": iso(start), "end": iso(now), "sources": {}}
    with closing(sqlite3.connect(root / "data/news.sqlite3")) as db:
        before = db.execute("SELECT count(*) FROM news").fetchone()[0]
        if before >= target:
            return {"status": "target_reached", "before": before, "after": before, "inserted": 0}
        last_run = manifest.get("latest_run")
        if not force and last_run and datetime.fromisoformat(last_run["started_at"]).astimezone(BEIJING).date() == now.astimezone(BEIJING).date():
            return {"status": "already_ran_today", "before": before, "after": before, "inserted": 0}
        if collectors is None:
            from crawler.bbc import collect as collect_bbc
            from crawler.sina import collect as collect_sina
            collectors = {"BBC News": collect_bbc, "新浪财经": collect_sina}
        known = {row[0] for row in db.execute("SELECT url FROM news")}
        inserted = 0
        budget = min(target - before, max_new) if max_new is not None else target - before
        if budget < 1:
            raise ValueError("max_new must be positive")
        with tempfile.TemporaryDirectory(prefix="news-collect-") as cache:
            for source, slug in SOURCES.items():
                if inserted >= budget:
                    run["sources"][source] = {"status": "not_needed", "counts": {"inserted": 0}, "reason": "Target or probe limit reached"}
                    continue
                report = {"status": "complete", "counts": {}, "errors": []}
                source_inserted = 0
                generator = None
                source_budget = min(budget - inserted, per_source_limit) if per_source_limit is not None else budget - inserted
                try:
                    generator = iter(collectors[source](start=start, end=now, known_urls=set(known),
                        limit=source_budget, workdir=Path(cache) / slug, report=report))
                    for candidate in generator:
                        try:
                            row = normalize(candidate, source, start, now)
                        except (ValueError, KeyError, TypeError) as error:
                            report["status"] = "partial"
                            report["counts"]["rejected"] = report["counts"].get("rejected", 0) + 1
                            if len(report["errors"]) < 20:
                                report["errors"].append({"reason": safe_error(error)})
                            continue
                        if row["url"] in known:
                            continue
                        # A single writer is protected by BEGIN IMMEDIATE. Only an actual
                        # insert consumes the target allowance; no existing news is updated.
                        with db:
                            db.execute("BEGIN IMMEDIATE")
                            if db.execute("SELECT count(*) FROM news").fetchone()[0] >= target:
                                break
                            cursor = db.execute(f"INSERT OR IGNORE INTO news({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})", tuple(row[key] for key in FIELDS))
                        known.add(row["url"])
                        if cursor.rowcount:
                            inserted += 1
                            source_inserted += 1
                        if inserted >= budget or source_inserted >= source_budget:
                            break
                except Exception as error:
                    report["status"] = "partial" if source_inserted else "failed"
                    if len(report["errors"]) < 20:
                        report["errors"].append({"reason": safe_error(error)})
                finally:
                    if generator is not None and hasattr(generator, "close"):
                        try:
                            generator.close()
                        except Exception as error:
                            report["status"] = "partial" if source_inserted else "failed"
                            if len(report["errors"]) < 20:
                                report["errors"].append({"reason": safe_error(error)})
                if report["counts"].get("rejected") and report["status"] == "complete":
                    report["status"] = "partial"
                report["counts"]["inserted"] = source_inserted
                run["sources"][source] = report
                print(json.dumps({"source": source, **report}, ensure_ascii=False), flush=True)
        after = db.execute("SELECT count(*) FROM news").fetchone()[0]
    unhealthy = [value for value in run["sources"].values() if value["status"] in {"failed", "partial"}]
    run.update(before=before, after=after, inserted=after - before,
               status="partial" if unhealthy and inserted else "failed" if unhealthy else "success",
               target_reached=after >= target,
               finished_at=iso(datetime.now(timezone.utc).replace(microsecond=0)))
    refresh(root, run=run, now=now)
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--prepare", action="store_true", help="Upgrade existing data metadata without network requests")
    parser.add_argument("--probe", action="store_true", help="Use a temporary database and do not change published files")
    parser.add_argument("--force", action="store_true", help="Allow an explicit retry on the same Beijing date")
    parser.add_argument("--max-new", type=int, help="Cap this run, primarily for smoke tests")
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.prepare:
        result = refresh(root)
    elif args.probe:
        import shutil
        with tempfile.TemporaryDirectory(prefix="news-probe-") as directory:
            isolated = Path(directory)
            shutil.copytree(root / "data", isolated / "data")
            for filename in ("schema.sql", "crawl_config.json"):
                shutil.copy2(root / filename, isolated / filename)
            result = run_daily(isolated, force=True, max_new=args.max_new or 2, per_source_limit=1)
            import subprocess
            subprocess.run([sys.executable, str(ROOT / "scripts/validate_database.py"), "--root", str(isolated)], check=True)
            result["probe"] = True
    else:
        result = run_daily(root, force=args.force, max_new=args.max_new)
    if args.result_file:
        write_json(args.result_file, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Failures are signalled after GitHub saves successful articles and diagnostics.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
