"""ftnews_ltc text extraction with visible prose and original publication dates."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
import trafilatura

from .session import StopCollection, validate_url

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


def parse_category_page(html, url):
    """Read only the visible chronological stream and its explicit next link.

    Category pages are discovery hints, never article text or publication proof.
    Follow only a consecutive page on the same category, without extra queries.
    """
    validate_url(url)
    soup = BeautifulSoup(html, "html.parser")
    barrier = soup.select_one('.barrier, .barrier__heading, .subscription-barrier, '
                              '[data-trackable="subscription-barrier"]')
    if barrier and any(p in barrier.get_text(" ", strip=True).lower()
                       for p in ("subscribe", "sign in", "subscription")):
        raise StopCollection("login_or_subscription_required")
    stream = soup.select_one(".js-stream-list")
    if stream is None:
        raise ValueError("category_stream_missing")
    entries = {}
    for link in stream.select(".stream-item a.js-teaser-heading-link[href]"):
        try:
            article_url = canonical_url(urljoin(url, link["href"]))
        except (ValueError, StopCollection):
            continue
        entries.setdefault(article_url, {"url": article_url,
                                        "title": link.get_text(" ", strip=True)})
    next_link = soup.select_one('a[data-trackable="next-page"][href]')
    next_url = None
    if next_link is not None:
        candidate = urljoin(url, next_link["href"])
        try:
            validate_url(candidate)
            current, target = urlsplit(url), urlsplit(candidate)
            query = parse_qs(target.query, keep_blank_values=True)
            current_page = int(parse_qs(current.query).get("page", ["1"])[0])
            valid = (target.path == current.path and not target.fragment
                     and set(query) == {"page"} and len(query["page"]) == 1
                     and query["page"][0] == str(current_page + 1))
        except (ValueError, StopCollection):
            valid = False
        if not valid:
            raise ValueError("invalid_category_pagination")
        next_url = candidate
    return list(entries.values()), next_url


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
        raise ValueError("missing_publication_time")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive_publication_time")
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
        raise ValueError("missing_or_ambiguous_publication_time")
    return distinct.pop()


def _hidden(node):
    style = node.get("style", "")
    return (node.has_attr("hidden") or str(node.get("aria-hidden", "")).lower() == "true"
            or bool(re.search(r"(?:^|;)\s*(?:display\s*:\s*none|(?:content-)?visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)",
                              style, re.I)))


def parse_article(html, url):
    """Use the supplied crawler's trafilatura settings, never hidden payloads.

    Metadata stays separate from text extraction. Feed dates only rank URLs;
    article publication metadata still determines the collection window.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Preserve explicit publication evidence before removing embedded scripts.
    # Parse it only after the access check, so a login page is not a date error.
    metadata_soup = BeautifulSoup(html, "html.parser")
    # Remove hidden ancestors before inspecting their descendants. Reversed
    # traversal keeps decompose() from invalidating a later node in this list.
    for node in reversed(list(soup.find_all(True))):
        if _hidden(node) or node.name in {"script", "style", "noscript", "template"}:
            node.decompose()
    # Reject an explicitly rendered subscription barrier, including one beside
    # a long teaser. Ordinary navigation Subscribe links do not count.
    barrier = soup.select_one('.barrier, .barrier__heading, .subscription-barrier, '
                              '[data-trackable="subscription-barrier"]')
    if barrier and any(p in barrier.get_text(" ", strip=True).lower()
                       for p in ("subscribe", "sign in", "subscription")):
        raise StopCollection("login_or_subscription_required")
    title = soup.find("h1")
    title = title.get_text(" ", strip=True) if title else ""
    for node in reversed(soup.select("head, meta, link, nav, aside, form, button, header, footer, "
                                      ".n-content-tag, .article__related-content")):
        node.decompose()
    # Known body markers keep recommendations out; unfamiliar FT layouts use
    # trafilatura's document detection instead of failing on a CSS selector.
    body = soup.select_one('[itemprop="articleBody"], .article__content-body, '
                           '.article__body, [data-trackable="article-body"]')
    if body is None:
        page_text = " ".join(soup.get_text(" ", strip=True).lower().split())
        hits = sum(phrase in page_text for phrase in PAYWALL_PHRASES)
        if hits >= 2 or "subscribe to unlock" in page_text or "sign in to continue" in page_text:
            raise StopCollection("login_or_subscription_required")
    if not title:
        raise ValueError("article_title_missing")
    published = publication_time(metadata_soup, url)
    # Some extractor fallbacks inspect JSON stored in data attributes. Keep
    # only structural hints and links; prose must be visible DOM text.
    for node in soup.find_all(True):
        node.attrs = {key: value for key, value in node.attrs.items()
                      if key in {"class", "id", "href"}}
    document = f"<html><body><article>{body}</article></body></html>" if body is not None else str(soup)
    text = trafilatura.extract(document, url=url, include_comments=False,
                               include_tables=False, favor_precision=True)
    text = text.strip() if text else ""
    lowered = " ".join(text.lower().split())
    hits = sum(phrase in lowered for phrase in PAYWALL_PHRASES)
    if hits >= 2 or "subscribe to unlock" in lowered or "sign in to continue" in lowered:
        raise StopCollection("login_or_subscription_required")
    if len(text) < 600 or "\ufffd" in text:
        raise ValueError("incomplete_article_body")
    return {"title": title, "content": text, "published": published}
