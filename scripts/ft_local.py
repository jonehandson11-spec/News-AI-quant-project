"""Collect FT locally, then append a validated batch to a fresh cloud snapshot."""
from __future__ import annotations

import argparse
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.crawl_daily import normalize
from scripts.dataset import FIELDS, iso, read_json, refresh, write_json
from scripts.validate_database import validate

SOURCE = "Financial Times"
REASONS = {
    "auth_required", "auth_expired", "access_denied", "rate_limited",
    "login_or_subscription_required", "unsafe_destination", "unsupported_method",
    "redirect_limit", "robots_unavailable", "robots_disallowed",
    "consecutive_network_failures", "feed_request_failed", "invalid_feed",
    "category_request_failed", "invalid_category_listing",
    "article_request_failed", "invalid_or_incomplete_article", "collector_failed",
    "credential_unavailable", "invalid_candidate", "cleanup_failed", "target_reached",
}
COUNT_KEYS = {"discovered", "known", "skipped", "outside_window", "success", "failed",
              "deferred", "rejected", "collected", "duplicates", "inserted", "deferred_cap"}
DISCOVERY_COUNTS = {"pages_fetched", "page_limit", "category_page_limit", "categories_completed", "categories_total"}
DISCOVERY_REASONS = {"window_boundary", "no_next_page", "page_limit", "total_page_limit",
                     "request_failed", "invalid_listing", "no_progress", "collection_stopped"}
DISCOVERY_URLS = {"https://www.ft.com/" + category for category in (
    "world", "global-economy", "europe", "us", "asia-pacific", "markets", "central-banks",
    "equities", "commodities", "currencies", "technology", "companies", "energy")}


class LocalFTError(ValueError):
    """Only fixed reason codes leave this module's command-line boundary."""


def timestamp(value):
    if not isinstance(value, str):
        raise LocalFTError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LocalFTError("invalid_timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise LocalFTError("invalid_timestamp")
    return result


def _external(path, root):
    path = Path(path).resolve()
    if path.is_relative_to(root.resolve()):
        raise LocalFTError("private_file_must_be_external")
    return path


def _clock(now=None):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise LocalFTError("invalid_timestamp")
    return now


def _batch_id(batch):
    payload = {key: value for key, value in batch.items() if key != "batch_id"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _report(value):
    """Allowlist diagnostic fields; never copy exception text or arbitrary keys."""
    value = value if isinstance(value, dict) else {}
    status = value.get("status")
    status = status if isinstance(status, str) and status in {"complete", "partial", "failed", "not_needed"} else "failed"
    counts = value.get("counts", {})
    counts = counts if isinstance(counts, dict) else {}
    result = {"source": SOURCE, "execution_location": "local", "status": status,
              "counts": {key: number for key, number in counts.items()
                         if key in COUNT_KEYS and type(number) is int and number >= 0},
              "errors": [], "stopped": value.get("stopped") is True}
    skips = value.get("skipped_reasons")
    if isinstance(skips, dict):
        result["skipped_reasons"] = {
            key: count for key, count in skips.items()
            if key == "unsafe_destination" and type(count) is int and count > 0}
    if isinstance(value.get("reason"), str) and value["reason"] in REASONS:
        result["reason"] = value["reason"]
    if result.get("reason") == "rate_limited":
        retry = value.get("retry_after_seconds")
        if type(retry) is int and 0 <= retry <= 2_147_483_647:
            result["retry_after_seconds"] = retry
        if value.get("endpoint_kind") in ("robots", "feed", "category", "article"):
            result["endpoint_kind"] = value["endpoint_kind"]
    errors = value.get("errors", [])
    for error in errors[:20] if isinstance(errors, list) else []:
        reason = error.get("reason") if isinstance(error, dict) else None
        safe_error = {"reason": reason if isinstance(reason, str) and reason in REASONS else "collector_failed"}
        url = error.get("url") if isinstance(error, dict) else None
        # Public article identifiers let the owner check the exact failing page.
        # Never retain queries, fragments, userinfo, login URLs or other hosts.
        if isinstance(url, str) and re.fullmatch(
                r"https://www\.ft\.com/content/[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", url):
            safe_error["url"] = url
        result["errors"].append(safe_error)
    if status in {"failed", "partial"} and "reason" not in result:
        result["reason"] = result["errors"][0]["reason"] if result["errors"] else "collector_failed"
    discovery = value.get("discovery")
    if isinstance(discovery, dict) and discovery.get("mode") in ("rss", "rss_and_category_pages"):
        summary = {"mode": discovery["mode"], "coverage_limited": True,
                   "coverage_note": "FT RSS and selected category listings do not guarantee a complete archive."}
        summary.update({key: count for key, count in discovery.items()
                        if key in DISCOVERY_COUNTS and type(count) is int and count >= 0})
        outcomes = discovery.get("category_outcomes", [])
        summary["category_outcomes"] = []
        for outcome in outcomes[:13] if isinstance(outcomes, list) else []:
            if (isinstance(outcome, dict) and isinstance(outcome.get("url"), str)
                    and outcome["url"] in DISCOVERY_URLS and isinstance(outcome.get("reason"), str)
                    and outcome["reason"] in DISCOVERY_REASONS
                    and type(outcome.get("pages")) is int and outcome["pages"] >= 0):
                summary["category_outcomes"].append({key: outcome[key] for key in ("url", "pages", "reason")})
        result["discovery"] = summary
    return result


def _failure(report, reason, collected):
    report.update(status="partial" if collected else "failed", reason=reason)
    report.setdefault("errors", []).append({"reason": reason})


def _row(candidate, start, end, seed_start, finished=None):
    if not isinstance(candidate, dict) or set(candidate) != set(FIELDS):
        raise LocalFTError("invalid_batch_row")
    try:
        if not isinstance(candidate["url"], str) or not candidate["url"].startswith("https://"):
            raise LocalFTError("invalid_batch_row")
        # Identifiers are derived from the canonical URL, never trusted from input.
        row = normalize(dict(candidate, article_id="derive-from-url"), SOURCE, start, end)
        published, crawled = timestamp(row["publish_time"]), timestamp(row["crawl_time"])
        if (published < seed_start or row["language"] != "en" or len(row["content"]) < 600
                or (finished is not None and crawled > finished)):
            raise LocalFTError("invalid_batch_row")
        return row
    except (KeyError, TypeError, ValueError, OverflowError):
        raise LocalFTError("invalid_batch_row") from None


def _batch_window_settings(batch):
    """Old unmarked batches mean 48 hours; extended windows require both markers."""
    if "lookback_hours" not in batch and "collection_mode" not in batch:
        return 48, "daily"
    lookback = batch.get("lookback_hours")
    if type(lookback) is not int or lookback not in (48, 120):
        raise LocalFTError("invalid_batch_lookback")
    mode = "backfill" if lookback == 120 else "daily"
    if batch.get("collection_mode") != mode:
        raise LocalFTError("invalid_batch_collection_mode")
    return lookback, mode


def collect_batch(root, cookie_file, batch_file, *, max_new=100, lookback_hours=48, now=None, collector=None):
    """Read a verified snapshot without changing it; persist only a private batch."""
    root = Path(root).resolve()
    cookie_file, batch_file = _external(cookie_file, root), _external(batch_file, root)
    if cookie_file == batch_file:
        raise LocalFTError("batch_cannot_replace_credential")
    if type(max_new) is not int or not 1 <= max_new <= 100:
        raise LocalFTError("max_new_must_be_between_1_and_100")
    if type(lookback_hours) is not int or lookback_hours not in (48, 120):
        raise LocalFTError("lookback_hours_must_be_48_or_120")
    collection_mode = "backfill" if lookback_hours == 120 else "daily"
    checked = validate(root)
    config, manifest = read_json(root / "crawl_config.json"), read_json(root / "data/manifest.json")
    started = _clock(now)
    start = started - timedelta(hours=lookback_hours)
    seed_start = timestamp(manifest.get("seed_window", manifest["publication_window"])["start"])
    before = checked["total_articles"]
    target = config["target_articles"]
    report = {"status": "complete", "counts": {}, "errors": []}
    rows = []
    if before >= target:
        report.update(status="not_needed", reason="target_reached")
    else:
        with closing(sqlite3.connect((root / "data/news.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
            known = {row[0] for row in db.execute("SELECT url FROM news")}
        budget = min(max_new, target - before, config.get("source_max_new", {}).get(SOURCE, 100))
        original_cookie = os.environ.get("FT_COOKIE")
        generator = None
        try:
            try:
                credential = cookie_file.read_text(encoding="utf-8")
            except Exception:
                raise LocalFTError("credential_unavailable") from None
            os.environ["FT_COOKIE"] = credential
            if collector is None:
                from crawler.ft import collect as collector
            # A future dependency must not accidentally print response or cookie
            # details; only the safe summary at the CLI boundary is emitted.
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), tempfile.TemporaryDirectory(prefix="ft-local-") as cache:
                generator = iter(collector(start=start, end=started, known_urls=set(known),
                                           limit=budget, workdir=Path(cache), report=report))
                for candidate in generator:
                    try:
                        # Collector extras are discarded before validation/storage.
                        selected = {key: candidate[key] for key in FIELDS}
                        row = _row(selected, start, started, seed_start)
                    except (KeyError, TypeError, ValueError):
                        _failure(report, "invalid_candidate", len(rows))
                        report.setdefault("counts", {})["rejected"] = report.get("counts", {}).get("rejected", 0) + 1
                        continue
                    if row["url"] in known:
                        continue
                    known.add(row["url"])
                    rows.append(row)
                    if len(rows) >= budget:
                        break
        except Exception as error:
            reason = "credential_unavailable" if isinstance(error, LocalFTError) else "collector_failed"
            _failure(report, reason, len(rows))
        finally:
            if generator is not None and hasattr(generator, "close"):
                try:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        generator.close()
                except Exception:
                    _failure(report, "cleanup_failed", len(rows))
            if original_cookie is None:
                os.environ.pop("FT_COOKIE", None)
            else:
                os.environ["FT_COOKIE"] = original_cookie
    finished = max(started, datetime.now(timezone.utc))
    # Final validation prevents a malformed capture timestamp entering a batch.
    valid = []
    for candidate in rows:
        try:
            valid.append(_row(candidate, start, started, seed_start, finished))
        except LocalFTError:
            _failure(report, "invalid_candidate", len(valid))
    report = _report(report)
    report.update(lookback_hours=lookback_hours, collection_mode=collection_mode)
    report["counts"].update(collected=len(valid), inserted=0)
    if report["status"] in {"failed", "partial"} or report["counts"].get("rejected"):
        report["status"] = "partial" if valid else "failed"
    batch = {"format_version": 1, "source": SOURCE, "execution_location": "local",
             "lookback_hours": lookback_hours, "collection_mode": collection_mode,
             "started_at": iso(started), "finished_at": iso(finished),
             "window": {"start": iso(start), "end": iso(started)},
             "snapshot_total": before, "rows": valid, "report": report}
    batch["batch_id"] = _batch_id(batch)
    write_json(batch_file, batch)
    result = {"status": report["status"], "reason": report.get("reason"),
            "collected": len(valid), "snapshot_total": before,
            "batch_id": batch["batch_id"],
            "lookback_hours": lookback_hours, "collection_mode": collection_mode,
            "batch_file": str(batch_file), "execution_location": "local"}
    result.update({key: report[key] for key in ("retry_after_seconds", "endpoint_kind") if key in report})
    return result


def _validated_batch(batch_file, manifest, now):
    batch = read_json(batch_file)
    if (not isinstance(batch, dict) or batch.get("format_version") != 1
            or batch.get("source") != SOURCE or batch.get("execution_location") != "local"):
        raise LocalFTError("invalid_batch")
    start, end = timestamp(batch["window"]["start"]), timestamp(batch["window"]["end"])
    started, finished = timestamp(batch["started_at"]), timestamp(batch["finished_at"])
    lookback_hours, collection_mode = _batch_window_settings(batch)
    if end - start != timedelta(hours=lookback_hours) or end != started or not started <= finished <= now:
        raise LocalFTError("invalid_batch_window")
    source_rows = batch.get("rows")
    if not isinstance(source_rows, list) or len(source_rows) > 100:
        raise LocalFTError("invalid_batch_rows")
    seed_start = timestamp(manifest.get("seed_window", manifest["publication_window"])["start"])
    rows = [_row(candidate, start, end, seed_start, finished) for candidate in source_rows]
    report = _report(batch.get("report"))
    report.update(lookback_hours=lookback_hours, collection_mode=collection_mode)
    if report["status"] == "not_needed" and rows:
        raise LocalFTError("invalid_batch_report")
    return batch, rows, report


def merge_batch(root, batch_file, *, now=None):
    """Append rows against current staging data; never replace an older snapshot."""
    root, now = Path(root).resolve(), _clock(now)
    batch_file = _external(batch_file, root)
    validate(root)
    manifest, config = read_json(root / "data/manifest.json"), read_json(root / "crawl_config.json")
    # Entire batch is validated before any database/file mutation.
    batch, rows, report = _validated_batch(batch_file, manifest, now)
    lookback_hours, collection_mode = _batch_window_settings(batch)
    source_path = root / manifest["sources"][SOURCE]["report"]
    previous = read_json(source_path)
    attempted = previous.get("health", {}).get("last_attempt_at")
    stale = attempted is not None and timestamp(batch["started_at"]) <= timestamp(attempted)
    inserted = duplicates = deferred = 0
    with closing(sqlite3.connect(root / "data/news.sqlite3")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        before = db.execute("SELECT count(*) FROM news").fetchone()[0]
        known = {url: ident for url, ident in db.execute("SELECT url, article_id FROM news")}
        known_ids = set(known.values())
        target = config["target_articles"]
        for row in rows:
            if row["url"] in known or row["article_id"] in known_ids:
                duplicates += 1
                continue
            if before + inserted >= target:
                deferred += 1
                continue
            db.execute(f"INSERT INTO news({','.join(FIELDS)}) VALUES ({','.join('?' for _ in FIELDS)})",
                       tuple(row[key] for key in FIELDS))
            known[row["url"]] = row["article_id"]
            known_ids.add(row["article_id"])
            inserted += 1
    report["counts"].update(collected=len(rows), duplicates=duplicates,
                             deferred_cap=deferred, inserted=inserted)
    report["batch_id"] = _batch_id(batch)
    status = "partial" if report["status"] in {"partial", "failed"} and inserted else (
        "failed" if report["status"] in {"partial", "failed"} else "success")
    result = {"status": "stale_batch" if stale and not inserted else status,
              "before": before, "after": before + inserted, "inserted": inserted,
              "duplicates": duplicates, "deferred_cap": deferred,
              "batch_id": _batch_id(batch),
              "lookback_hours": lookback_hours, "collection_mode": collection_mode,
              "collection_status": report["status"], "collection_reason": report.get("reason"),
              "collection_stopped": report["stopped"],
              "execution_location": "local", "health_preserved": stale}
    if not (stale and inserted == 0):
        run = {"started_at": batch["started_at"], "finished_at": batch["finished_at"],
               "merged_at": iso(now), "start": batch["window"]["start"],
               "end": batch["window"]["end"], "scope": "selected",
               "selected_sources": [SOURCE], "sources": {} if stale else {SOURCE: report},
               "local_batch": {"batch_id": result["batch_id"], "lookback_hours": lookback_hours,
                               "collection_mode": collection_mode, "collected": len(rows), "duplicates": duplicates,
                               "deferred_cap": deferred, "inserted": inserted},
               "target_reached": before + inserted >= config["target_articles"], **result}
        refresh(root, run=run, now=now)
        validate(root)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect")
    collect.add_argument("--root", type=Path, default=ROOT)
    collect.add_argument("--cookie-file", type=Path, required=True)
    collect.add_argument("--batch-file", type=Path, required=True)
    collect.add_argument("--max-new", type=int, default=100)
    collect.add_argument("--lookback-hours", type=int, choices=(48, 120), default=48,
                         help="Default daily window is 48 hours; 120 requests an explicit five-day backfill")
    collect.add_argument("--result-file", type=Path)
    merge = commands.add_parser("merge")
    merge.add_argument("--root", type=Path, required=True)
    merge.add_argument("--batch-file", type=Path, required=True)
    merge.add_argument("--result-file", type=Path)
    args = parser.parse_args(argv)
    try:
        result_path = _external(args.result_file, args.root.resolve()) if args.result_file else None
        if result_path and (result_path == args.batch_file.resolve()
                            or (args.command == "collect" and result_path == args.cookie_file.resolve())):
            raise LocalFTError("result_cannot_replace_batch_or_credential")
        if args.command == "collect":
            result = collect_batch(args.root, args.cookie_file, args.batch_file,
                                   max_new=args.max_new, lookback_hours=args.lookback_hours)
        else:
            result = merge_batch(args.root, args.batch_file)
        if result_path:
            write_json(result_path, result)
    except Exception as error:
        reason = str(error) if isinstance(error, LocalFTError) else "local_ft_operation_failed"
        print(json.dumps({"status": "failed", "reason": reason}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
