import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from news_price_query.assets import resolve_asset
from news_price_query.cli import main
from news_price_query.news import find_direct_match, read_news


class NewsCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prices = self.root / "prices.csv"
        with self.prices.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("ticker", "interval", "timestamp", "close"))
            writer.writerows([
                ("1211.HK", "day", "2026-03-25", 70),
                ("1211.HK", "day", "2026-06-25", 80),
                ("1211.HK", "day", "2026-08-25", 90),
                ("1211.HK", "minute", "2026-09-25T09:59:00+08:00", 100),
                ("1211.HK", "minute", "2026-09-25T11:00:00+08:00", 102),
            ])

    def test_team_csv_embedded_newline_stays_one_article_and_has_offset(self):
        path = self.root / "news.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            writer.writerow(("abc", "���˲ƾ�", "���ǵϷ����³�", "��һ��\n�ڶ���", "2026-09-25T10:00:00+08:00", "2026-09-25T10:05:00+08:00", "https://example.test/a", "zh-CN"))
        items = read_news(path)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].content, "��һ��\n�ڶ���")
        self.assertEqual(items[0].published_at.isoformat(), "2026-09-25T10:00:00+08:00")
        match = find_direct_match(items[0], resolve_asset("BYD"))
        self.assertEqual(match.kind, "title_mention")
        self.assertEqual(match.evidence, "���ǵ�")

    def test_bbc_six_column_time_is_hong_kong_time(self):
        path = self.root / "bbc.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("title", "publish_time", "crawled_time", "source_website", "url", "content"))
            writer.writerow(("BYD factory opens", "2026-09-25 10:00:00", "2026-09-25 11:00:00", "BBC News", "https://example.test/b", "BYD opened a factory."))
        item = read_news(path)[0]
        self.assertEqual(item.published_at.isoformat(), "2026-09-25T10:00:00+08:00")
        self.assertEqual(find_direct_match(item, resolve_asset("BYD")).evidence, "BYD")

    def test_sector_keyword_is_not_automatic_byd_match(self):
        path = self.root / "sector.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            writer.writerow(("abc", "���˲ƾ�", "﮵�ؼ۸��µ�", "����Դ������Ӱ��", "2026-09-25T10:00:00+08:00", "2026-09-25T10:05:00+08:00", "https://example.test/c", "zh-CN"))
        self.assertIsNone(find_direct_match(read_news(path)[0], resolve_asset("BYD")))

    def test_query_cli_rejects_naive_time_without_timezone(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["query", "--asset", "BYD", "--time", "2026-09-25T10:00:00", "--provider", "csv", "--prices", str(self.prices)])
        self.assertEqual(code, 2)
        self.assertIn("--timezone", err.getvalue())

    def test_query_cli_emits_price_and_provenance(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["query", "--asset", "BYD", "--time", "2026-09-25T10:00:00+08:00", "--as-of", "2026-09-25T12:00:00+08:00", "--provider", "csv", "--prices", str(self.prices)])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["ticker"], "1211.HK")
        self.assertEqual(result["baseline"]["price"], 100.0)
        self.assertEqual(result["windows"]["after_1h"]["price"], 102.0)
        self.assertEqual(result["windows"]["after_3h"]["status"], "pending")

    def test_batch_keeps_article_id_and_skips_unmatched(self):
        path = self.root / "news.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            writer.writerow(("one", "���˲ƾ�", "���ǵ��³�", "����", "2026-09-25T10:00:00+08:00", "2026-09-25T11:00:00+08:00", "https://example.test/one", "zh-CN"))
            writer.writerow(("two", "���˲ƾ�", "﮵�ؼ۸�", "����", "2026-09-25T10:00:00+08:00", "2026-09-25T11:00:00+08:00", "https://example.test/two", "zh-CN"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["batch", "--asset", "BYD", "--news", str(path), "--as-of", "2026-09-25T12:00:00+08:00", "--provider", "csv", "--prices", str(self.prices)])
        self.assertEqual(code, 0)
        results = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([item["article_id"] for item in results], ["one"])
        self.assertEqual(results[0]["match"]["kind"], "title_mention")

    def test_batch_csv_writes_one_row_per_news_window(self):
        news = self.root / "news.csv"
        output = self.root / "results.csv"
        with news.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            writer.writerow(("one", "���˲ƾ�", "���ǵ��³�", "����", "2026-09-25T10:00:00+08:00", "2026-09-25T11:00:00+08:00", "https://example.test/one", "zh-CN"))
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["batch", "--asset", "BYD", "--news", str(news), "--as-of", "2026-09-25T12:00:00+08:00", "--provider", "csv", "--prices", str(self.prices), "--format", "csv", "--output", str(output)])
        self.assertEqual(code, 0)
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 9)
        one_hour = next(row for row in rows if row["window"] == "after_1h")
        self.assertEqual(one_hour["article_id"], "one")
        self.assertEqual(one_hour["price"], "102.0")
        self.assertEqual(one_hour["return_pct"], "2.0")

    def test_scan_lists_direct_mentions_without_price_data(self):
        news = self.root / "scan_news.csv"
        with news.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            writer.writerow(("one", "���˲ƾ�", "���ǵ��³�", "����", "2026-09-25T10:00:00+08:00", "2026-09-25T11:00:00+08:00", "https://example.test/one", "zh-CN"))
            writer.writerow(("two", "���˲ƾ�", "﮵�ؼ۸�", "����", "2026-09-25T10:00:00+08:00", "2026-09-25T11:00:00+08:00", "https://example.test/two", "zh-CN"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["scan", "--asset", "BYD", "--news", str(news)])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["articles_total"], 2)
        self.assertEqual(result["matches_total"], 1)
        self.assertEqual(result["matches"][0]["article_id"], "one")
        self.assertEqual(result["matches"][0]["match_type"], "title_mention")

    def test_scan_creates_output_parent_directory(self):
        news = self.root / "scan_news.csv"
        with news.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("article_id", "source", "title", "content", "publish_time", "crawl_time", "url", "language"))
            writer.writerow(("one", "���˲ƾ�", "���ǵ��³�", "����", "2026-09-25T10:00:00+08:00", "2026-09-25T11:00:00+08:00", "https://example.test/one", "zh-CN"))
        output = self.root / "new-folder" / "scan.json"
        code = main(["scan", "--asset", "BYD", "--news", str(news), "--output", str(output)])
        self.assertEqual(code, 0)
        self.assertTrue(output.exists())

    def test_optional_benchmark_reports_aligned_excess_return(self):
        with self.prices.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows([
                ("2800.HK", "minute", "2026-09-25T09:59:00+08:00", 50),
                ("2800.HK", "minute", "2026-09-25T11:00:00+08:00", 50.5),
            ])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["query", "--asset", "BYD", "--time", "2026-09-25T10:00:00+08:00", "--as-of", "2026-09-25T12:00:00+08:00", "--provider", "csv", "--prices", str(self.prices), "--benchmark", "2800.HK"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["benchmark_ticker"], "2800.HK")
        self.assertEqual(result["windows"]["after_1h"]["benchmark_return_pct"], 1.0)
        self.assertEqual(result["windows"]["after_1h"]["excess_return_pct"], 1.0)

    def test_benchmark_with_stale_baseline_has_no_excess_return(self):
        with self.prices.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows([
                ("2800.HK", "minute", "2026-09-25T09:55:00+08:00", 50),
                ("2800.HK", "minute", "2026-09-25T11:00:00+08:00", 50.5),
            ])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["query", "--asset", "BYD", "--time", "2026-09-25T10:00:00+08:00", "--as-of", "2026-09-25T12:00:00+08:00", "--provider", "csv", "--prices", str(self.prices), "--benchmark", "2800.HK"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertIsNone(result["windows"]["after_1h"]["excess_return_pct"])


if __name__ == "__main__":
    unittest.main()
