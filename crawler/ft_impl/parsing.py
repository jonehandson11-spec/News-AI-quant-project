"""Only visible article prose and unambiguous original publication dates."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from .session import StopCollection, validate_url
from .diagnostics import ArticleParseError

ARTICLE_TYPES = {"NewsArticle", "Article", "ReportageNewsArticle", "AnalysisNewsArticle", "OpinionNewsArticle"}
PAYWALL_PHRASES = (
    "subscribe to unlock", "range of subscriptions", "discover all the plans",
    "sign in to continue", "what our readers say", "brief with no waffle",
)


def canonical_url(url):
    validate_url(url)
    parsed = urlsplit(url)
    if not re.fullmatch(r"/content/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/?", parsed.path):
        raise ValueError("unsupported_article_url")
    return urlunsplit(("https", "www.ft.com", parsed.path.rstrip("/"), "", ""))


def parse_feed(text):
    root = ET.fromstring(text)
    entries = []
    for item in root.findall("./channel/item"):
        try:
            url = canonical_url((item.findtext("link") or "").strip())
        except (ValueError, StopCollection):
            continue
        # RSS dates rank candidates only; HTML original publication metadata
        # still determines whether an article belongs in the requested window.
        try:
            hint = parsedate_to_datetime(item.findtext("pubDate") or "")
            if hint.tzinfo is None:
                hint = None
        except (TypeError, ValueError, OverflowError):
            hint = None
        entries.append({"url": url, "title": (item.findtext("title") or "").strip(),
                        "rss_published": hint})
    if not entries and root.find("channel") is None:
        raise ValueError("unsupported_feed")
    return entries


def _articles(value):
    if isinstance(value, list):
        for item in value:
            yield from _articles(item)
    elif isinstance(value, dict):
        kind = value.get("@type", [])
        kinds = [kind] if isinstance(kind, str) else kind
        if isinstance(kinds, list) and ARTICLE_TYPES.intersection(kinds):
            yield value
        if "@graph" in value:
            yield from _articles(value["@graph"])


def _aware(value):
    if not isinstance(value, str):
        raise ArticleParseError("missing_publication_time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ArticleParseError("invalid_publication_time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ArticleParseError("naive_publication_time")
    return parsed.astimezone(timezone.utc)


def publication_time(soup, url):
    candidates = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        for article in _articles(data):
            linked = article.get("url") or article.get("mainEntityOfPage")
            if isinstance(linked, dict):
                linked = linked.get("@id") or linked.get("url")
            if linked:
                try:
                    if canonical_url(linked) != url:
                        continue
                except (ValueError, StopCollection, TypeError):
                    continue
            if "datePublished" in article:
                candidates.append(_aware(article["datePublished"]))
    if not candidates:
        # Only explicit published markers qualify; generic/o-date or updated
        # timestamps and RSS pubDate are not evidence of original publication.
        for node in soup.select('time[itemprop="datePublished"][datetime], '
                                'time[data-trackable="published"][datetime], '
                                '.article__timestamp--published time[datetime]'):
            candidates.append(_aware(node.get("datetime")))
    distinct = set(candidates)
    if len(distinct) != 1:
        raise ArticleParseError("missing_or_ambiguous_publication_time")
    return distinct.pop()


def parse_article(html, url):
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one('[itemprop="articleBody"], .article__content-body, '
                           '.article__body, [data-trackable="article-body"]')
    # Check explicit barriers even when no article container was delivered.
    # These codes describe the response, not confirmed account entitlement.
    barriers = soup.select('.barrier, .barrier__heading, .subscription-barrier, '
                           '[data-trackable="subscription-barrier"]')
    for barrier in barriers:
        barrier_text = " ".join(barrier.get_text(" ", strip=True).lower().split())
        if "subscribe" in barrier_text or "subscription" in barrier_text:
            raise StopCollection("subscription_barrier_detected")
        if "sign in" in barrier_text:
            raise StopCollection("login_prompt_detected")
    if body is None:
        page_text = " ".join(soup.get_text(" ", strip=True).lower().split())
        if any(p in page_text for p in PAYWALL_PHRASES):
            raise StopCollection("paywall_page_text_detected")
        raise ArticleParseError("visible_article_body_missing")
    for node in body.select("script, style, noscript, nav, aside, form, button, "
                            ".n-content-tag, .article__related-content, [aria-hidden='true']"):
        node.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in body.select("p")]
    text = "\n\n".join(p for p in paragraphs if p).strip()
    lowered = " ".join(text.lower().split())
    hits = sum(phrase in lowered for phrase in PAYWALL_PHRASES)
    if hits >= 2 or "subscribe to unlock" in lowered or "sign in to continue" in lowered:
        raise StopCollection("paywall_body_text_detected")
    if len(text) < 600 or len(paragraphs) < 2 or "\ufffd" in text:
        raise ArticleParseError("incomplete_article_body")
    title = soup.find("h1")
    title = title.get_text(" ", strip=True) if title else ""
    if not title:
        raise ArticleParseError("article_title_missing")
    return {"title": title, "content": text, "published": publication_time(soup, url)}
