#!/usr/bin/env python3
"""Financial Times 时政/宏观新闻爬虫：RSS 取链 + trafilatura 抽正文，写入 SQLite。"""

from __future__ import annotations

import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import feedparser
import trafilatura
from curl_cffi import requests

# ---------------------------------------------------------------------------
# 请求头。会员 Cookie 请粘贴到同目录 cookie.txt，避免写进 Python 字符串。
# cookie.txt 不存在时才使用下面的备用原文（可留空）。
# ---------------------------------------------------------------------------
FALLBACK_RAW_COOKIE = ""
COOKIE_FILE = Path("cookie.txt")
COOKIE_TEMPLATE = """# 把浏览器里复制的 Cookie 全文直接粘贴到本文件（可含多行、无关追踪字段）。
# 程序只会提取：FTSession_s、FTSession、ft-access-decision-policy。
# 下面是占位示例，请整段替换为你的真实 Cookie：

FTSession_s=YOUR_FTSESSION_S_HERE; FTSession=YOUR_FTSESSION_HERE; ft-access-decision-policy=SUBSCRIPTION_POLICY
"""

CORE_COOKIE_KEYS: tuple[str, ...] = (
    "FTSession_s",
    "FTSession",
    "ft-access-decision-policy",
)

HEADERS_BASE: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

DB_PATH = "ft_news.sqlite3"
REQUEST_TIMEOUT = 30
MAX_ARTICLES_PER_RUN = 30
CRAWL_INTERVAL = timedelta(hours=1)
BEIJING_TZ = timezone(timedelta(hours=8))
TIME_FMT = "%Y-%m-%d %H:%M:%S"
CLOCK_FMT = "%H:%M:%S"
MIN_CONTENT_LENGTH = 600
PAYWALL_PHRASES: tuple[str, ...] = (
    "subscribe to unlock",
    "range of subscriptions",
    "discover all the plans",
    "sign in to continue",
)

RSS_FEEDS: list[tuple[str, str]] = [
    # --- 1. 核心时政与宏观 ---
    ("https://www.ft.com/world?format=rss", "world"),                       # 国际时政总版
    ("https://www.ft.com/global-economy?format=rss", "global-economy"),     # 全球宏观经济
    ("https://www.ft.com/europe?format=rss", "europe"),               # 欧洲时政与欧盟
    ("https://www.ft.com/us?format=rss", "us"),                       # 美国政治与经济
    ("https://www.ft.com/asia-pacific?format=rss", "asia-pacific"),   # 亚太与大中华区
    
    # --- 2. 金融市场与资产板块 ---
    ("https://www.ft.com/markets?format=rss", "markets"),                   # 金融市场总版
    ("https://www.ft.com/central-banks?format=rss", "central-banks"),       # 全球央行与利率
    ("https://www.ft.com/equities?format=rss", "equities"),                 # 全球股市
    ("https://www.ft.com/commodities?format=rss", "commodities"),           # 大宗商品与原油
    ("https://www.ft.com/currencies?format=rss", "currencies"),             # 外汇与汇率
    
    # --- 3. 关键产业与商业 ---
    ("https://www.ft.com/technology?format=rss", "technology"),             # 科技与芯片半导体
    ("https://www.ft.com/companies?format=rss", "companies"),               # 全球企业与商业
    ("https://www.ft.com/energy?format=rss", "energy"),              # 传统能源与新能源
]


def ensure_cookie_template() -> None:
    if not COOKIE_FILE.exists():
        COOKIE_FILE.write_text(COOKIE_TEMPLATE, encoding="utf-8")


def load_raw_cookie() -> str:
    if COOKIE_FILE.is_file():
        text = COOKIE_FILE.read_text(encoding="utf-8")
        if text.strip():
            return text
    return FALLBACK_RAW_COOKIE


def extract_effective_ft_cookie(raw_cookie: str) -> str:
    """从杂乱浏览器 Cookie 文本中只抽出 FT 会员核心键值。"""
    if not raw_cookie:
        return ""

    pairs: list[str] = []
    for name in CORE_COOKIE_KEYS:
        pattern = rf"(?:^|[\s;]){re.escape(name)}=([^\s;]+)"
        match = re.search(pattern, raw_cookie)
        if not match:
            continue
        value = match.group(1).strip().strip("\"'")
        if not value:
            continue
        if '"' in value or "{" in value or "}" in value:
            continue
        if value.upper().startswith("YOUR_") or value.endswith("_HERE"):
            continue
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def build_headers() -> dict[str, str]:
    raw = load_raw_cookie()
    cookie = extract_effective_ft_cookie(raw)
    headers = dict(HEADERS_BASE)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def warn_if_cookie_missing(cookie: str) -> None:
    if "FTSession_s=" not in cookie:
        print(
            "⚠️ 提示：未在 cookie.txt 中检测到有效的 FT 会员凭证 (FTSession_s)，"
            "请打开 cookie.txt 粘贴最新 Cookie"
        )


def init_db(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            title TEXT,
            publish_time TEXT,
            crawled_time TEXT,
            source_website TEXT,
            url TEXT PRIMARY KEY,
            content TEXT
        )
        """
    )
    conn.commit()
    return conn


def parse_published(entry: Any) -> str:
    if getattr(entry, "published_parsed", None):
        try:
            utc_dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            return utc_dt.astimezone(BEIJING_TZ).strftime(TIME_FMT)
        except (TypeError, ValueError):
            pass

    raw = (getattr(entry, "published", None) or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(BEIJING_TZ).strftime(TIME_FMT)
    except (TypeError, ValueError):
        return raw


def collect_feed_items() -> list[dict[str, str]]:
    """第 1 步：从 RSS 提取标题、发布时间、原文链接。同一 URL 只保留首次出现。"""
    seen: set[str] = set()
    items: list[dict[str, str]] = []

    for feed_url, _category in RSS_FEEDS:
        print(f"正在读取 RSS: {feed_url}")
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            print(f"  RSS 解析失败: {exc}", file=sys.stderr)
            continue

        if getattr(parsed, "bozo", False) and parsed.get("bozo_exception"):
            print(f"  RSS 警告: {parsed.bozo_exception}", file=sys.stderr)

        entries = parsed.get("entries") or []
        print(f"  得到 {len(entries)} 条条目")

        for entry in entries:
            url = (getattr(entry, "link", None) or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = (getattr(entry, "title", None) or "").strip() or "(无标题)"
            items.append(
                {
                    "title": title,
                    "publish_time": parse_published(entry),
                    "source_website": "Financial Times",
                    "url": url,
                }
            )

    return items


def fetch_full_text(url: str, session: requests.Session) -> str | None:
    """第 2 步：下载页面并用 trafilatura 提取纯净正文，拦截付费墙促销页。"""
    url = url.split("?")[0]
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    text = trafilatura.extract(
        response.text,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )
    if not text:
        return None
    text = text.strip()
    if not text:
        return None

    lowered = " ".join(text.lower().split())
    hit_count = sum(1 for phrase in PAYWALL_PHRASES if phrase in lowered)
    if hit_count >= 2:
        return None
    if len(text) < MIN_CONTENT_LENGTH:
        return None
    return text


def load_existing_urls(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT url FROM articles")}


def crawl() -> int:
    conn: sqlite3.Connection | None = None
    inserted = 0
    try:
        conn = init_db()
        session = requests.Session(impersonate="chrome131")
        headers = build_headers()
        warn_if_cookie_missing(headers.get("Cookie", ""))
        session.headers.update(headers)

        items = collect_feed_items()
        existing_urls = load_existing_urls(conn)
        pending = [item for item in items if item["url"] not in existing_urls]
        to_crawl = pending[:MAX_ARTICLES_PER_RUN]
        already = len(items) - len(pending)
        print(
            f"抓取前核对 SQLite：RSS {len(items)} 篇，"
            f"库中已有 {already} 篇（跳过、不发正文请求），"
            f"待抓取 {len(pending)} 篇"
        )

        total = len(pending)
        if total == 0:
            print("本轮没有需要抓取正文的新文章。")
            return 0

        print(f"\n共 {total} 篇待抓取正文（单线程，篇间随机延时 2.5–4.5 秒）\n")
        failed_paywall_count = 0

        for index, item in enumerate(to_crawl, start=1):
            title_preview = item["title"].replace("\n", " ")
            if len(title_preview) > 80:
                title_preview = title_preview[:77] + "..."
            print(f"正在抓取 [{index}/{total}]: {title_preview}")

            try:
                content = fetch_full_text(item["url"], session)
                if not content:
                    failed_paywall_count += 1
                    print(
                        "  跳过：未能提取到有效正文（付费墙/过短），"
                        "该 URL 不入库，稍后可补爬"
                    )
                else:
                    failed_paywall_count = 0
                    crawled_time = datetime.now(BEIJING_TZ).strftime(TIME_FMT)
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO articles
                            (title, publish_time, crawled_time, source_website, url, content)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["title"],
                            item["publish_time"],
                            crawled_time,
                            item["source_website"],
                            item["url"],
                            content,
                        ),
                    )
                    conn.commit()
                    if cursor.rowcount > 0:
                        inserted += 1
                        print("  已入库")
                    else:
                        print("  已存在，跳过重复 URL")
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failed_paywall_count += 1
                print(f"  失败: {type(exc).__name__}: {exc}（不入库）", file=sys.stderr)
                if failed_paywall_count >= 3:
                    print(
                        "⚠️ 警告：检测到连续多篇内容触发 FT 付费墙拦截，"
                        "你的 Cookie 可能已失效，请重新登录浏览器复制最新 Cookie！"
                    )

            if index < total:
                delay = random.uniform(5, 10)
                time.sleep(delay)

        return inserted
    finally:
        if conn is not None:
            conn.close()


def run_forever() -> None:
    ensure_cookie_template()
    warn_if_cookie_missing(build_headers().get("Cookie", ""))
    print("常驻爬虫已启动：立即执行第一轮，之后每隔 1 小时执行一次。Ctrl+C 退出。")
    try:
        while True:
            inserted = crawl()
            now = datetime.now(BEIJING_TZ)
            next_run = now + CRAWL_INTERVAL
            print(f"当前时间（北京）：{now.strftime(TIME_FMT)}")
            print(f"本次新增入库 {inserted} 条。")
            print(
                "本轮抓取结束，正在等待下一轮..."
                f"（下一次执行时间：{next_run.strftime(CLOCK_FMT)}）"
            )
            time.sleep(CRAWL_INTERVAL.total_seconds())
    except KeyboardInterrupt:
        print("\n收到中断信号（Ctrl+C），已安全关闭数据库连接，程序退出。")


if __name__ == "__main__":
    run_forever()
