"""Parse only the publication evidence and full text on a Sina article page."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

CHINA_TZ = ZoneInfo("Asia/Shanghai")
ARTICLE_PATH_RE = re.compile(r"(?:doc-|/\d{4}-\d{2}-\d{2}/|/roll/\d{8}/)", re.I)
PROMOTIONAL_RE = re.compile(
    r"^(?:炒股就看|股市瞬息万变|海量资讯、精准解读|责任编辑[：:]|点击进入专题|"
    r"打开新浪财经APP|下载新浪财经APP|（截图来自新浪财经APP|扫码.*(?:开户|下载))"
)


def clean_text(text: str) -> str:
    text = unescape(text).replace("\u3000", " ").replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in {"http", "https"} else parts.scheme
    return urlunsplit((scheme, parts.netloc.lower(), re.sub(r"/+", "/", parts.path), "", ""))


def is_allowed_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return (
            parts.scheme == "https"
            and parts.port in (None, 443)
            and parts.username is None
            and parts.password is None
            and any(host == suffix or host.endswith("." + suffix) for suffix in ("sina.com.cn", "sina.cn"))
        )
    except ValueError:
        return False


def is_article_url(url: str) -> bool:
    return is_allowed_url(url) and bool(ARTICLE_PATH_RE.search(urlsplit(url).path))


def normalize_time(value: object) -> datetime:
    if value is None or value == "":
        raise ValueError("publication time missing")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        timestamp = int(float(value))
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        return datetime.fromtimestamp(timestamp, timezone.utc).astimezone(CHINA_TZ).replace(microsecond=0)
    text = clean_text(str(value)).replace("年", "-").replace("月", "-").replace("日", " ")
    text = re.sub(r"\s+", " ", text).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        if parsed is None:
            raise ValueError("publication time invalid") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TZ)
    return parsed.astimezone(CHINA_TZ).replace(microsecond=0)


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            value = clean_text(str(tag["content"]))
            if value:
                return value
    return None


def parse_article(html: str, url: str, fallback_title: str | None = None) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select("script, style, noscript, iframe, .article-editor, .show_statement, .keywords, .appendQr_wrap, .article-bottom, .tip, .sinaad-toolkit-box"):
        tag.decompose()
    title = _meta(soup, "og:title", "twitter:title")
    if not title:
        heading = soup.select_one("h1")
        title = clean_text(heading.get_text(" ", strip=True)) if heading else fallback_title
    title = clean_text(title or fallback_title or "")

    published = _meta(soup, "bytedance:published_time", "weibo: article:create_at", "article:published_time", "pub_date", "publishdate", "date")
    if not published:
        time_tag = soup.select_one("time[datetime], .date, .time-source, .article-time, .publish-time")
        if time_tag:
            published = time_tag.get("datetime") or time_tag.get_text(" ", strip=True)
            # A visible date may be followed by the publisher's name.
            if not time_tag.get("datetime"):
                match = re.search(r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?", str(published))
                published = match.group(0) if match else published
    publish_time = normalize_time(published)

    content = ""
    for selector in ("#artibody", "#article", ".article-content", ".article_body", "article"):
        node = soup.select_one(selector)
        if not node:
            continue
        paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in node.select("p")]
        if not paragraphs and selector in ("#artibody", "#article"):
            # The 7x24 flash template stores actual text directly in this div.
            paragraphs = [clean_text(node.get_text(" ", strip=True))]
        candidate = "\n".join(p for p in paragraphs if p and not PROMOTIONAL_RE.search(p))
        if candidate:
            content = clean_text(candidate)
            break
    if not title:
        raise ValueError("article title missing")
    if len(content) < 40:
        raise ValueError("article full text missing or shorter than 40 characters")

    url = canonicalize_url(url)
    language_text = title + "\n" + content
    chinese = len(re.findall(r"[\u3400-\u9fff]", language_text))
    latin = len(re.findall(r"[A-Za-z]", language_text))
    language = "zh-CN" if chinese >= max(5, latin // 3) else ("en" if latin >= 5 else "unknown")
    return {
        "article_id": hashlib.sha256(url.encode("utf-8")).hexdigest()[:32],
        "source": "新浪财经",
        "title": title,
        "content": content,
        "publish_time": publish_time.isoformat(),
        "crawl_time": datetime.now(CHINA_TZ).replace(microsecond=0).isoformat(),
        "url": url,
        "language": language,
    }
