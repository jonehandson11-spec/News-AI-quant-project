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

CLOUD_SOURCES = frozenset(("BBC News", "新浪财经"))


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
    permitted = {"BBC News": ("bbc.com", "bbc.co.uk"), "新浪财经": ("sina.com.cn", "sina.cn"),
                 "Financial Times": ("ft.com",)}.get(source)
    if permitted is None:
        raise ValueError("Unknown news source")
    if parts.scheme not in {"https", "http"} or parts.username or parts.password or not any(host == domain or host.endswith("." + domain) for domain in permitted):
        raise ValueError("URL is outside the requested news source")
    if source == "Financial Times" and (host != "www.ft.com" or parts.port is not None or not re.fullmatch(r"/content/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", parts.path)):
        raise ValueError("Invalid FT article URL")
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


def source_error(error: Exception, source: str) -> str:
    # FT credentials must never enter public reports, even if a dependency raises
    # an exception containing a request, response, environment, or cookie value.
    return "FT collector error; check source health and renew FT_COOKIE if needed" if source == "Financial Times" else safe_error(error)


def run_daily(root: Path, *, now: datetime | None = None, collectors=None,
              force: bool = False, max_new: int | None = None,
              per_source_limit: int | None = None, cloud_only: bool = False,
              execution_location: str | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    if now.tzinfo is None:
        raise ValueError("Current time must be timezone-aware")
    manifest = read_json(root / "data/manifest.json")
    config = read_json(root / "crawl_config.json")
    target = config["target_articles"]
    start = now - timedelta(hours=config["daily_lookback_hours"])
    if cloud_only and collectors is not None:
        collectors = {source: collector for source, collector in collectors.items() if source in CLOUD_SOURCES}
    selected = (set(CLOUD_SOURCES) if cloud_only else set(SOURCES)) if collectors is None else set(collectors)
    execution_location = execution_location or ("github_actions" if cloud_only else "local")
    if execution_location not in {"github_actions", "local"}:
        raise ValueError("Unsupported execution location")
    run = {"started_at": iso(now), "start": iso(start), "end": iso(now), "sources": {},
           "scope": "all" if cloud_only or selected >= set(SOURCES) else "selected",
           "selected_sources": sorted(selected), "execution_location": execution_location}
    with closing(sqlite3.connect(root / "data/news.sqlite3")) as db:
        before = db.execute("SELECT count(*) FROM news").fetchone()[0]
        if before >= target:
            return {"status": "target_reached", "before": before, "after": before, "inserted": 0}
        last_run = manifest.get("latest_run")
        last_full_run = manifest.get("last_full_run_at")
        if not last_full_run and last_run and last_run.get("scope", "all") == "all":
            last_full_run = last_run["started_at"]
        # Local FT runs have their own scheduling. A completed cloud run must
        # not prevent them from collecting and merging on the same Beijing day.
        if selected != {"Financial Times"} and not force and last_full_run and datetime.fromisoformat(last_full_run).astimezone(BEIJING).date() == now.astimezone(BEIJING).date():
            return {"status": "already_ran_today", "before": before, "after": before, "inserted": 0}
        if collectors is None:
            from crawler.bbc import collect as collect_bbc
            from crawler.sina import collect as collect_sina
            collectors = {"BBC News": collect_bbc, "新浪财经": collect_sina}
            if not cloud_only:
                from crawler.ft import collect as collect_ft
                collectors["Financial Times"] = collect_ft
        known = {row[0] for row in db.execute("SELECT url FROM news")}
        inserted = 0
        budget = min(target - before, max_new) if max_new is not None else target - before
        if budget < 1:
            raise ValueError("max_new must be positive")
        with tempfile.TemporaryDirectory(prefix="news-collect-") as cache:
            for source, slug in SOURCES.items():
                if cloud_only and source == "Financial Times":
                    run["sources"][source] = {"status": "not_needed", "counts": {"inserted": 0}, "reason": "collected_locally"}
                    continue
                if source not in collectors:
                    run["sources"][source] = {"status": "not_needed", "counts": {"inserted": 0}, "reason": "Source not selected for this run"}
                    continue
                if inserted >= budget:
                    run["sources"][source] = {"status": "not_needed", "counts": {"inserted": 0}, "reason": "Target or probe limit reached"}
                    continue
                report = {"status": "complete", "counts": {}, "errors": []}
                source_inserted = 0
                generator = None
                source_budget = min(budget - inserted, per_source_limit) if per_source_limit is not None else budget - inserted
                source_budget = min(source_budget, config.get("source_max_new", {}).get(source, source_budget))
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
                                report["errors"].append({"reason": source_error(error, source)})
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
                        report["errors"].append({"reason": source_error(error, source)})
                finally:
                    if generator is not None and hasattr(generator, "close"):
                        try:
                            generator.close()
                        except Exception as error:
                            report["status"] = "partial" if source_inserted else "failed"
                            if len(report["errors"]) < 20:
                                report["errors"].append({"reason": source_error(error, source)})
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
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument("--ft-only", action="store_true", help="Collect only FT on this computer")
    source_mode.add_argument("--cloud-only", action="store_true", help="Collect BBC and Sina in the cloud; FT is collected locally")
    parser.add_argument("--result-file", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    collectors = None
    if args.ft_only:
        from crawler.ft import collect as collect_ft
        collectors = {"Financial Times": collect_ft}
    if args.prepare:
        result = refresh(root)
    elif args.probe:
        import shutil
        with tempfile.TemporaryDirectory(prefix="news-probe-") as directory:
            isolated = Path(directory)
            shutil.copytree(root / "data", isolated / "data")
            for filename in ("schema.sql", "crawl_config.json"):
                shutil.copy2(root / filename, isolated / filename)
            probe_sources = len(CLOUD_SOURCES) if args.cloud_only else len(collectors or SOURCES)
            result = run_daily(isolated, collectors=collectors, force=True,
                               max_new=args.max_new or probe_sources, per_source_limit=1,
                               cloud_only=args.cloud_only)
            import subprocess
            subprocess.run([sys.executable, str(ROOT / "scripts/validate_database.py"), "--root", str(isolated)], check=True)
            result["probe"] = True
    else:
        result = run_daily(root, collectors=collectors, force=args.force, max_new=args.max_new,
                           cloud_only=args.cloud_only)
    if args.result_file:
        write_json(args.result_file, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    # Failures are signalled after GitHub saves successful articles and diagnostics.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
