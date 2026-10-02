"""Credentialed FT collection: no credential files, RSS summaries or paywalls stored."""
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

from .ft_impl.parsing import parse_article, parse_category_page, parse_feed
from .ft_impl.session import FTSession, StopCollection

SOURCE = "Financial Times"
CATEGORY_PAGES = tuple("https://www.ft.com/" + category for category in (
    "world", "global-economy", "europe", "us", "asia-pacific", "markets",
    "central-banks", "equities", "commodities", "currencies", "technology",
    "companies", "energy"))
RSS_FEEDS = tuple(url + "?format=rss" for url in CATEGORY_PAGES)
CATEGORY_PAGE_LIMIT = 5
DISCOVERY_PAGE_LIMIT = 40


def collect(*, start: datetime, end: datetime, known_urls: set[str], limit: int,
            workdir: Path, report: dict):
    """Yield at most limit authenticated, full-text articles in an aware window.

    The local runner injects FT_COOKIE from a private file outside the repository.
    The CookieJar is renewed in memory during redirects; expired credentials
    require the owner to sign in normally and update that file. workdir is unused;
    authenticated HTML and cookies are deliberately never written to disk.
    """
    counts = {key: 0 for key in ("discovered", "known", "skipped", "outside_window",
                                 "success", "failed", "deferred")}
    report.update(source=SOURCE, status="complete", counts=counts, errors=[],
                  skipped_reasons={},
                  stopped=False, publication_basis="article original publication time",
                  credential_storage="environment; memory only")
    if limit <= 0:
        return
    if (start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None
            or end.utcoffset() is None or start >= end):
        raise ValueError("FT requires an aware increasing publication window")
    extended_discovery = end - start > timedelta(hours=48)
    discovery = {
        "mode": "rss_and_category_pages" if extended_discovery else "rss",
        "pages_fetched": 0, "page_limit": DISCOVERY_PAGE_LIMIT,
        "category_page_limit": CATEGORY_PAGE_LIMIT, "categories_completed": 0,
        "categories_total": len(CATEGORY_PAGES) if extended_discovery else 0,
        "coverage_limited": True,
        "coverage_note": "Bounded discovery from selected FT categories; not an exhaustive FT archive.",
        "category_outcomes": [],
    }
    report["discovery"] = discovery

    def failure(url, reason):
        counts["failed"] += 1
        if len(report["errors"]) < 20:
            report["errors"].append({"url": url, "reason": reason})
        report["status"] = "partial" if counts["success"] else "failed"

    def stop(error):
        report.update(stopped=True, reason=error.reason)
        if error.reason == "rate_limited":
            if error.retry_after_seconds is not None:
                report["retry_after_seconds"] = error.retry_after_seconds
            if error.endpoint_kind is not None:
                report["endpoint_kind"] = error.endpoint_kind

    network_failures = 0
    with FTSession() as session:
        if not session.has_login_cookie():
            report.update(reason="auth_required", stopped=True)
            failure("https://www.ft.com", "auth_required")
            return
        found = {}
        for feed in RSS_FEEDS:
            try:
                text = session.fetch(feed)
                network_failures = 0
                for item in parse_feed(text):
                    found.setdefault(item["url"], item)
            except StopCollection as error:
                failure(feed, error.reason)
                stop(error)
                return
            except requests.RequestException:
                network_failures += 1
                failure(feed, "feed_request_failed")
                if network_failures >= 3:
                    report.update(stopped=True, reason="consecutive_network_failures")
                    return
            except (ValueError, ET.ParseError):
                failure(feed, "invalid_feed")
        if extended_discovery:
            # Round-robin prevents high-volume categories consuming the budget
            # before any of the other configured categories has been inspected.
            pending = deque()
            for category in CATEGORY_PAGES:
                outcome = {"url": category, "pages": 0, "reason": "total_page_limit"}
                discovery["category_outcomes"].append(outcome)
                pending.append((category + "?page=1", outcome, set()))

            def mark_discovery_stopped():
                for outcome in discovery["category_outcomes"]:
                    if outcome["reason"] == "total_page_limit":
                        outcome["reason"] = "collection_stopped"

            while pending and discovery["pages_fetched"] < DISCOVERY_PAGE_LIMIT:
                page_url, outcome, seen = pending.popleft()
                try:
                    if not session.has_login_cookie():
                        raise StopCollection("auth_expired")
                    # Charge attempts, including failed requests, to the budget.
                    discovery["pages_fetched"] += 1
                    outcome["pages"] += 1
                    text = session.fetch(page_url)
                    network_failures = 0
                    items, next_url = parse_category_page(text, page_url)
                    page_urls = {item["url"] for item in items}
                    for item in items:
                        found.setdefault(item["url"], item)
                    if not next_url:
                        outcome["reason"] = "no_next_page"
                        discovery["categories_completed"] += 1
                    elif not page_urls.difference(seen):
                        outcome["reason"] = "no_progress"
                    elif outcome["pages"] >= CATEGORY_PAGE_LIMIT:
                        outcome["reason"] = "page_limit"
                    else:
                        seen.update(page_urls)
                        pending.append((next_url, outcome, seen))
                except StopCollection as error:
                    failure(page_url, error.reason)
                    stop(error)
                    mark_discovery_stopped()
                    counts["discovered"] = len(found)
                    return
                except requests.RequestException:
                    network_failures += 1
                    outcome["reason"] = "request_failed"
                    failure(page_url, "category_request_failed")
                    if network_failures >= 3:
                        report.update(stopped=True, reason="consecutive_network_failures")
                        mark_discovery_stopped()
                        counts["discovered"] = len(found)
                        return
                except ValueError:
                    outcome["reason"] = "invalid_listing"
                    failure(page_url, "invalid_category_listing")
        rows = sorted(found.values(), key=lambda row: row.get("rss_published")
                      or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        counts["discovered"] = len(rows)
        for index, row in enumerate(rows):
            url = row["url"]
            if url in known_urls:
                counts["known"] += 1
                continue
            try:
                if not session.has_login_cookie():
                    raise StopCollection("auth_expired")
                html = session.fetch(url)
                network_failures = 0
                article = parse_article(html, url)
                if not start <= article["published"] <= end:
                    counts["outside_window"] += 1
                    continue
                captured = datetime.now(timezone.utc)
                if captured < article["published"]:
                    raise ValueError("future_publication_time")
                counts["success"] += 1
                report["status"] = "partial" if counts["failed"] else "complete"
                # The batch writer may close the generator immediately on yield.
                # Record unvisited candidates before handing it the last row.
                if counts["success"] >= limit:
                    counts["deferred"] = len(rows) - index - 1
                yield {"article_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
                       "source": SOURCE, "title": article["title"], "content": article["content"],
                       "publish_time": article["published"].isoformat(),
                       "crawl_time": captured.isoformat(), "url": url, "language": "en"}
                if counts["success"] >= limit:
                    counts["deferred"] = len(rows) - index - 1
                    return
            except StopCollection as error:
                if error.reason == "unsafe_destination":
                    # A feed can link to a UUID that redirects outside the
                    # permitted FT article host. The session already refused
                    # that destination without sending credentials to it. Skip
                    # this candidate, not every other independent article.
                    counts["skipped"] += 1
                    skips = report["skipped_reasons"]
                    skips[error.reason] = skips.get(error.reason, 0) + 1
                    continue
                failure(url, error.reason)
                stop(error)
                counts["deferred"] = len(rows) - index - 1
                return
            except requests.RequestException:
                network_failures += 1
                failure(url, "article_request_failed")
                if network_failures >= 3:
                    report.update(stopped=True, reason="consecutive_network_failures")
                    counts["deferred"] = len(rows) - index - 1
                    return
            except ValueError:
                failure(url, "invalid_or_incomplete_article")
