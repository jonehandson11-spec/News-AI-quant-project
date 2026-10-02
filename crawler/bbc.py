"""BBC News RSS discovery with visible prose and original publication dates."""
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .bbc_impl.article import ArticleExtractionError, _structured_articles
from .bbc_impl.fulltext import AccessDenied, ArticleClient, BEIJING, check_url

RSS_URL = "https://feeds.bbci.co.uk/news/world/rss.xml"
RSS_URLS = (
    RSS_URL,
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/uk/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml",
    "https://feeds.bbci.co.uk/news/entertainment_and_arts/rss.xml",
    "https://feeds.bbci.co.uk/news/health/rss.xml",
)
MAX_FEED_BYTES = 4_000_000


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.documents = []
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.active = dict(attrs).get("type") == "application/ld+json"
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            try:
                self.documents.append(json.loads("".join(self.parts)))
            except ValueError:
                pass
            self.active = False


def publication_time(html):
    parser = MetadataParser()
    parser.feed(html)
    articles = [article for document in parser.documents
                for article in _structured_articles(document)]
    if len(articles) != 1 or not isinstance(articles[0].get("datePublished"), str):
        raise ValueError("Missing or ambiguous primary article datePublished")
    result = datetime.fromisoformat(articles[0]["datePublished"].replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Primary article datePublished requires a timezone")
    return result.astimezone(timezone.utc)


def download_feed(url=RSS_URL):
    request = urllib.request.Request(url, headers={
        "User-Agent": "BBCWorldRSSCollector/2.0", "Accept": "application/rss+xml"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            data = response.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403, 429):
            raise AccessDenied(f"RSS HTTP {error.code}; stop this run") from error
        raise
    if len(data) > MAX_FEED_BYTES:
        raise ValueError("BBC feed exceeds size limit")
    return data


def parse_feed(data):
    root = ET.fromstring(data)
    found = {}
    for item in root.findall("./channel/item"):
        link = item.findtext("link", "").strip()
        title = item.findtext("title", "").strip()
        if not link or not title:
            continue
        try:
            parts = urlsplit(link)
            url = urlunsplit(("https", parts.netloc.lower(), parts.path, "", ""))
            check_url(url)
        except ValueError:
            continue
        found.setdefault(url, {"url": url, "title": title})
    if not found:
        raise ValueError("BBC feed contains no supported article links")
    return list(found.values())


def _reason(error):
    """Reports are public: keep diagnostics useful without serializing local paths."""
    if isinstance(error, AccessDenied):
        return "Access denied or rate limited; source stopped (including robots.txt rules)"
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return "Network request failed or timed out"
    if isinstance(error, OSError):
        return "Network or cache I/O failed"
    if isinstance(error, ET.ParseError):
        return "Malformed RSS response"
    if isinstance(error, ArticleExtractionError):
        return "Page does not contain a supported complete visible article"
    return "Invalid feed, article publication metadata, or capture timestamp"


def collect(*, start: datetime, end: datetime, known_urls: set[str], limit: int,
            workdir: Path, report: dict):
    """Yield at most limit new full-text articles; failures stay source-local.

    The RSS timestamp is deliberately not used for publication-window inclusion.
    Status is complete for exhausted feeds or a reached limit, partial when
    usable articles accompany failures, and failed when failures yield no rows.
    """
    counts = {key: 0 for key in ("discovered", "known", "skipped", "outside_window",
                                 "success", "failed", "deferred")}
    report.update(source="BBC News", status="complete", counts=counts, errors=[],
                  scope="BBC News official section RSS feeds", feeds=[], stopped=False,
                  publication_basis="public article JSON-LD datePublished")
    if limit <= 0:
        return
    if (start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None
            or end.utcoffset() is None or start >= end):
        raise ValueError("BBC collection needs an aware, increasing time window")

    def failure(url, error):
        counts["failed"] += 1
        if len(report["errors"]) < 20:
            report["errors"].append({"url": url, "reason": _reason(error)})
        report["status"] = "partial" if counts["success"] else "failed"

    found = {}
    feed_network_failures = 0
    for index, feed_url in enumerate(RSS_URLS):
        if index:
            time.sleep(2)
        feed_report = {"url": feed_url, "status": "complete", "discovered": 0}
        report["feeds"].append(feed_report)
        try:
            feed_rows = parse_feed(download_feed(feed_url))
            feed_network_failures = 0
            feed_report["discovered"] = len(feed_rows)
            for row in feed_rows:
                found.setdefault(row["url"], row)
        except (OSError, ValueError, ET.ParseError) as error:
            feed_report["status"] = "failed"
            failure(feed_url, error)
            network_error = isinstance(error, OSError) and (
                not isinstance(error, urllib.error.HTTPError) or error.code >= 500)
            feed_network_failures = feed_network_failures + 1 if network_error else 0
            if isinstance(error, AccessDenied) or feed_network_failures >= 3:
                report["stopped"] = True
                counts["discovered"] = counts["deferred"] = len(found)
                return
    rows = list(found.values())
    counts["discovered"] = len(rows)
    if not rows:
        return
    client = ArticleClient(Path(workdir), timeout=25, delay=2)
    network_failures = 0
    for index, row in enumerate(rows):
        url = row["url"]
        if counts["success"] >= limit:
            break
        if url in known_urls:
            counts["known"] += 1
            continue
        if any(part in urlsplit(url).path.split("/") for part in ("live", "videos", "video", "av")):
            counts["skipped"] += 1
            continue
        try:
            body, _, captured = client.fetch(url)
            # A successful response ends a run of consecutive network failures,
            # even when its original publication date later proves unusable.
            network_failures = 0
            key = hashlib.sha256(url.encode("utf-8")).hexdigest()
            html = (Path(workdir) / "article_pages" / (key + ".html")).read_text(encoding="utf-8")
            published = publication_time(html)
            if not start <= published <= end:
                counts["outside_window"] += 1
                continue
            captured_at = datetime.strptime(captured, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
            counts["success"] += 1
            report["status"] = "partial" if counts["failed"] else "complete"
            if counts["success"] >= limit:
                # The caller can close the generator immediately after this yield.
                counts["deferred"] = len(rows) - index - 1
            yield {"article_id": key[:32], "source": "BBC News", "title": row["title"],
                   "content": body, "publish_time": published.isoformat(),
                   "crawl_time": captured_at.astimezone(timezone.utc).isoformat(),
                   "url": url, "language": "en"}
        except (OSError, ValueError) as error:
            if isinstance(error, ArticleExtractionError) and "live/video page" in str(error):
                network_failures = 0
                counts["skipped"] += 1
                continue
            network_error = isinstance(error, OSError) and (
                not isinstance(error, urllib.error.HTTPError) or error.code >= 500)
            if network_error:
                network_failures += 1
            else:
                network_failures = 0
            failure(url, error)
            if isinstance(error, AccessDenied) or network_failures >= 3:
                report["stopped"] = True
                counts["deferred"] = len(rows) - index - 1
                break
