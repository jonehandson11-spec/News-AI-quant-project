"""Credentialed FT collection: no credential files, RSS summaries or paywalls stored."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

from .ft_impl.parsing import parse_article, parse_feed
from .ft_impl.session import FTSession, StopCollection

SOURCE = "Financial Times"
RSS_FEEDS = tuple("https://www.ft.com/" + category + "?format=rss" for category in (
    "world", "global-economy", "europe", "us", "asia-pacific", "markets",
    "central-banks", "equities", "commodities", "currencies", "technology",
    "companies", "energy"))


def collect(*, start: datetime, end: datetime, known_urls: set[str], limit: int,
            workdir: Path, report: dict):
    """Yield at most limit authenticated, full-text articles in an aware window.

    FT_COOKIE must be injected from a private server secret. The CookieJar is
    renewed in memory during redirects, but expired credentials require the
    account owner to sign in normally and update that secret. workdir is unused;
    authenticated HTML and cookies are deliberately never written to disk.
    """
    counts = {key: 0 for key in ("discovered", "known", "skipped", "outside_window",
                                 "success", "failed", "deferred")}
    report.update(source=SOURCE, status="complete", counts=counts, errors=[],
                  stopped=False, publication_basis="article original publication time",
                  credential_storage="environment; memory only")
    if limit <= 0:
        return
    if (start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None
            or end.utcoffset() is None or start >= end):
        raise ValueError("FT requires an aware increasing publication window")

    def failure(url, reason):
        counts["failed"] += 1
        if len(report["errors"]) < 20:
            report["errors"].append({"url": url, "reason": reason})
        report["status"] = "partial" if counts["success"] else "failed"

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
                report.update(stopped=True, reason=error.reason)
                return
            except requests.RequestException:
                network_failures += 1
                failure(feed, "feed_request_failed")
                if network_failures >= 3:
                    report.update(stopped=True, reason="consecutive_network_failures")
                    return
            except (ValueError, ET.ParseError):
                failure(feed, "invalid_feed")
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
                yield {"article_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
                       "source": SOURCE, "title": article["title"], "content": article["content"],
                       "publish_time": article["published"].isoformat(),
                       "crawl_time": captured.isoformat(), "url": url, "language": "en"}
                if counts["success"] >= limit:
                    counts["deferred"] = len(rows) - index - 1
                    return
            except StopCollection as error:
                failure(url, error.reason)
                report.update(stopped=True, reason=error.reason)
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
