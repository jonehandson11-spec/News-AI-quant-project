import csv
import json
from pathlib import Path
import tempfile
import unittest

from web.build_site import build


class SiteBuildTests(unittest.TestCase):
    def test_build_indexes_matches_without_publishing_article_bodies(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            news = root / "news.csv"
            with news.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
                writer.writerow(("id-1", "test", "比亚迪发布新车型", "private body text", "2026-09-28T10:00:00+08:00", "2026-09-28T10:05:00+08:00", "https://example.com/1", "zh-CN"))
            output = root / "site"
            manifest = build(news, output)
            index = json.loads((output / "news-index.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["news_articles_total"], 1)
            self.assertEqual(index["matches"][0]["ticker"], "1211.HK")
            self.assertTrue((output / "local-prices.mjs").exists())
            self.assertNotIn("private body text", (output / "news-index.json").read_text(encoding="utf-8"))
            self.assertFalse((output / "price-history.csv").exists())

    def test_authorized_public_prices_are_validated_before_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            news = root / "news.csv"
            with news.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            prices = root / "public_prices.csv"
            prices.write_text("ticker,interval,timestamp,close\n1211.HK,minute,2026-09-28T09:59:00+08:00,100\n", encoding="utf-8")
            output = root / "site"
            manifest = build(news, output, prices)
            self.assertTrue(manifest["public_prices_available"])
            self.assertEqual(manifest["price_bars_total"], 1)
            self.assertEqual((output / "price-history.csv").read_text(encoding="utf-8"), prices.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
