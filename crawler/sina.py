"""Collect recent Sina Finance full text with bounded, polite public requests."""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
from requests.adapters import HTTPAdapter

from .sina_impl.parser import canonicalize_url, is_allowed_url, is_article_url, normalize_time, parse_article

FEED_URL = "https://feed.mix.sina.com.cn/api/roll/get"
USER_AGENT = "SharedNewsCollector/1.0 (+https://github.com/jonehandson11-spec/News-AI-quant-project)"
MAX_PAGES = 100
REQUEST_DELAY_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 20


class _StopSource(Exception):
    pass


def _error(report: dict, url: str, reason: str) -> None:
    report["counts"]["errors"] += 1
    if len(report["errors"]) < 20:
        report["errors"].append({"url": url, "reason": reason})


class _Client:
    def __init__(self, report: dict):
        self.report = report
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"})
        self.session.mount("https://", HTTPAdapter(max_retries=0))
        self.last_request = None
        self.consecutive_failures = 0
        self.robots: dict[str, RobotFileParser] = {}
        self.delay = REQUEST_DELAY_SECONDS

    def close(self) -> None:
        self.session.close()

    def _raw_get(self, url: str):
        if self.last_request is not None:
            time.sleep(max(0.0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.report["counts"]["requests"] += 1
        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False)
        except requests.RequestException as exc:
            self.consecutive_failures += 1
            self.report["counts"]["network_errors"] += 1
            _error(self.report, url, "network request failed: " + type(exc).__name__)
            if self.consecutive_failures >= 3:
                raise _StopSource("three consecutive network failures") from None
            return None
        if response.status_code in (401, 403, 429):
            _error(self.report, url, "HTTP " + str(response.status_code) + "; source stopped")
            raise _StopSource("access denied or rate limited")
        if response.status_code >= 500:
            self.consecutive_failures += 1
            self.report["counts"]["network_errors"] += 1
            _error(self.report, url, "HTTP " + str(response.status_code))
            if self.consecutive_failures >= 3:
                raise _StopSource("three consecutive network failures")
            return None
        self.consecutive_failures = 0
        return response

    def _robots_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = parts.scheme + "://" + parts.netloc
        if origin not in self.robots:
            robots_url = origin + "/robots.txt"
            response = self._raw_get(robots_url)
            if response is None:
                raise _StopSource("robots.txt could not be checked")
            rules = RobotFileParser(robots_url)
            if response.status_code in (404, 410):
                rules.parse([])
            elif response.status_code == 200:
                rules.parse(response.text.splitlines())
            else:
                _error(self.report, robots_url, "robots.txt unavailable: HTTP " + str(response.status_code))
                raise _StopSource("robots.txt could not be checked")
            self.robots[origin] = rules
            crawl_delay = rules.crawl_delay(USER_AGENT)
            if crawl_delay:
                self.delay = max(self.delay, float(crawl_delay))
            rate = rules.request_rate(USER_AGENT)
            if rate and rate.requests > 0:
                self.delay = max(self.delay, rate.seconds / rate.requests)
        if not self.robots[origin].can_fetch(USER_AGENT, url):
            self.report["counts"]["robots_denied"] += 1
            _error(self.report, url, "robots.txt disallows this URL")
            return False
        return True

    def get(self, url: str):
        for _ in range(4):
            if not is_allowed_url(url):
                _error(self.report, url, "unsupported URL or redirect destination")
                return None
            if not self._robots_allowed(url):
                return None
            response = self._raw_get(url)
            if response is None:
                return None
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                if not location:
                    break
                url = urljoin(url, location)
                continue
            if response.status_code != 200:
                _error(self.report, url, "HTTP " + str(response.status_code))
                return None
            if not response.encoding or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding or "utf-8"
            return response
        _error(self.report, url, "redirect limit reached or redirect missing location")
        return None


def collect(*, start: datetime, end: datetime, known_urls: set[str], limit: int, workdir: Path, report: dict):
    """Yield at most ``limit`` new articles, inclusive of both time boundaries.

    The feed's ctime is used only for discovery and pagination. Stored dates
    always come from the article HTML. No persistent files are required.
    """
    report.clear()
    report.update({"status": "complete", "counts": {key: 0 for key in (
        "pages", "discovered", "skipped_known", "skipped_window", "invalid", "fetched",
        "yielded", "requests", "network_errors", "robots_denied", "errors",
    )}, "errors": []})
    if limit <= 0:
        report["reason"] = "limit reached"
        return
    if start.tzinfo is None or end.tzinfo is None or start > end:
        raise ValueError("start and end must be aware datetimes in chronological order")
    known = {canonicalize_url(url) for url in known_urls}
    seen: set[str] = set()
    client = _Client(report)
    old_pages = 0
    try:
        for page in range(1, MAX_PAGES + 1):
            feed_url = FEED_URL + "?" + urlencode({"pageid": 153, "lid": 2516, "num": 50, "page": page})
            response = client.get(feed_url)
            if response is None:
                if report["counts"]["robots_denied"]:
                    raise _StopSource("feed denied by robots.txt")
                old_pages = 0
                continue
            try:
                payload = response.json()
                result = payload.get("result")
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ValueError("unexpected feed structure")
                items = result["data"]
            except (ValueError, AttributeError):
                _error(report, feed_url, "invalid feed JSON or structure")
                old_pages = 0
                continue
            report["counts"]["pages"] += 1
            if not items:
                report["reason"] = "feed exhausted"
                break
            all_old = True
            for item in items:
                if not isinstance(item, dict):
                    all_old = False
                    report["counts"]["invalid"] += 1
                    _error(report, feed_url, "invalid feed item")
                    continue
                try:
                    feed_time = normalize_time(item.get("ctime"))
                except (ValueError, TypeError, OverflowError, OSError):
                    feed_time = None
                if feed_time is None or feed_time >= start:
                    all_old = False
                try:
                    url = canonicalize_url(str(item.get("url", "")))
                except ValueError:
                    url = ""
                if not is_article_url(url) or url in seen:
                    continue
                seen.add(url)
                report["counts"]["discovered"] += 1
                if url in known:
                    report["counts"]["skipped_known"] += 1
                    continue
                if feed_time is not None and not start <= feed_time <= end:
                    report["counts"]["skipped_window"] += 1
                    continue
                article_response = client.get(url)
                if article_response is None:
                    continue
                report["counts"]["fetched"] += 1
                try:
                    article = parse_article(article_response.text, url, str(item.get("title", "")))
                    page_time = normalize_time(article["publish_time"])
                except (ValueError, TypeError, OverflowError, OSError) as exc:
                    report["counts"]["invalid"] += 1
                    _error(report, url, "article rejected: " + str(exc))
                    continue
                if not start <= page_time <= end:
                    report["counts"]["skipped_window"] += 1
                    continue
                report["counts"]["yielded"] += 1
                yield article
                if report["counts"]["yielded"] >= limit:
                    report["reason"] = "limit reached"
                    break
            if report["counts"]["yielded"] >= limit:
                break
            old_pages = old_pages + 1 if all_old else 0
            if old_pages >= 2:
                report["reason"] = "two consecutive pages older than window"
                break
        else:
            _error(report, FEED_URL, "maximum page count reached")
            report["reason"] = "maximum page count reached"
    except _StopSource as exc:
        report["reason"] = str(exc)
        report["status"] = "partial" if report["counts"]["yielded"] else "failed"
    finally:
        client.close()
        if report["status"] == "complete" and report["counts"]["errors"]:
            report["status"] = "partial" if report["counts"]["pages"] else "failed"
