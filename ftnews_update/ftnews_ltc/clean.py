#!/usr/bin/env python3
"""清理 ft_news.db 中过短或付费墙促销残文。"""

from __future__ import annotations

import sqlite3
import sys

DB_PATH = "ft_news.sqlite3"
MIN_CONTENT_LENGTH = 600
PAYWALL_PHRASES: tuple[str, ...] = (
    "Subscribe to unlock",
    "Standard Digital",
    "range of subscriptions",
    "Discover all the plans",
    "was €",
    "Save now on essential",
    "what our readers say",       # 👈 新增：秒杀读者评价推广
    "brief with no waffle",       # 👈 新增：秒杀 Lex 专栏推广词
)


def hit_count(content: str) -> int:
    lowered = " ".join((content or "").lower().split())
    return sum(1 for phrase in PAYWALL_PHRASES if phrase.lower() in lowered)


def should_delete(content: str) -> bool:
    text = content or ""
    if len(text) < MIN_CONTENT_LENGTH:
        return True
    return hit_count(text) >= 2


def clean() -> None:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("SELECT url, content FROM articles").fetchall()
        urls_to_delete = [url for url, content in rows if should_delete(content)]

        if urls_to_delete:
            conn.executemany(
                "DELETE FROM articles WHERE url = ?",
                [(url,) for url in urls_to_delete],
            )
            conn.commit()

        print(f"✅ 清理完成！已成功剔除付费墙垃圾文章 {len(urls_to_delete)} 篇")
    except sqlite3.Error as exc:
        print(f"清理失败: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    clean()
