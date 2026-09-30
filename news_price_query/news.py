"""Import news records and identify explicit asset mentions."""

import csv
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .assets import Asset


HK = ZoneInfo("Asia/Hong_Kong")
TEAM_FIELDS = {"article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"}
BBC_FIELDS = {"title", "publish_time", "crawled_time", "source_website", "url", "content"}


@dataclass(frozen=True)
class NewsItem:
    article_id: str
    source: str
    title: str
    content: str
    published_at: datetime
    url: str
    language: str


@dataclass(frozen=True)
class Match:
    kind: str
    evidence: str
    verified_by_human: bool = False


def _published(value: str, *, bbc: bool) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not bbc:
            raise ValueError("Team news publish_time needs a timezone offset")
        parsed = parsed.replace(tzinfo=HK)
    return parsed.astimezone(HK)


def read_news(path: str | Path) -> list[NewsItem]:
    """Read the team's current eight-column CSV or the BBC six-column export."""
    items: list[NewsItem] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or ())
        team = TEAM_FIELDS.issubset(fields)
        bbc = BBC_FIELDS.issubset(fields)
        if not team and not bbc:
            raise ValueError("Unknown news CSV schema; expected team eight-column or BBC six-column export")
        for row_number, row in enumerate(reader, 2):
            try:
                url = row["url"].strip()
                article_id = row["article_id"].strip() if team else sha256(url.encode("utf-8")).hexdigest()[:32]
                items.append(NewsItem(
                    article_id=article_id,
                    source=(row["source"] if team else row["source_website"]).strip(),
                    title=row["title"].strip(),
                    content=row["content"] or "",
                    published_at=_published(row["publish_time"], bbc=bbc and not team),
                    url=url,
                    language=(row["language"] if team else "en").strip(),
                ))
            except (KeyError, AttributeError, ValueError) as exc:
                raise ValueError(f"Invalid news row {row_number}: {exc}") from exc
    return items


def _contains(text: str, alias: str) -> bool:
    if alias.isascii():
        pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
        return re.search(pattern, text, re.IGNORECASE) is not None
    return alias in text


def find_direct_match(news: NewsItem, asset: Asset) -> Match | None:
    """Find a named company; sector vocabulary alone is never a match."""
    aliases = sorted(asset.aliases, key=len, reverse=True)
    for field, kind in ((news.title, "title_mention"), (news.content, "body_mention")):
        for alias in aliases:
            if _contains(field, alias):
                return Match(kind=kind, evidence=alias)
    return None
