import unittest

from news_price_query.daily_report import _span
from news_price_query.daily_table import format_daily_table


class StartEndTests(unittest.TestCase):
    def test_before_window_ends_at_baseline_after_window_starts_there(self):
        before = _span("before_5td", "2026-09-25T16:00:00+08:00", 23.4, "2026-09-18T16:00:00+08:00", 23.6)
        self.assertEqual((before["start_price"], before["end_price"]), (23.6, 23.4))
        self.assertTrue(before["start_at"].startswith("2026-09-18"))
        after = _span("after_t1", "2026-09-25T16:00:00+08:00", 23.4, "2026-09-29T16:00:00+08:00", 23.7)
        self.assertEqual((after["start_price"], after["end_price"]), (23.4, 23.7))
        missing = _span("after_t5", "2026-09-25T16:00:00+08:00", 23.4, None, None)
        self.assertIsNone(missing["end_price"])

    def test_table_shows_both_prices_and_missing_text(self):
        report = {
            "ticker": "0883.HK", "published_at_local": "2026-09-28T11:17:42+08:00", "currency": "HKD",
            "windows": {
                "after_t1": {"status": "on_date", "return_pct": 1.2821, "adj_return_pct": 1.2821,
                             **_span("after_t1", "2026-09-25", 23.4, "2026-09-29", 23.7),
                             **{f"adj_{k}": v for k, v in _span("after_t1", "2026-09-25", 23.4, "2026-09-29", 23.7).items()}},
                "after_t3": {"status": "missing", "return_pct": None, "adj_return_pct": None,
                             **_span("after_t3", "2026-09-25", 23.4, None, None)},
            },
            "benchmarks": {"^HSI": {"windows": {"after_t1": {
                "return_pct": 0.055, "excess_return_pct": 1.23, "adj_excess_return_pct": 1.23,
                **_span("after_t1", "2026-09-25", 24510.0898, "2026-09-29", 24523.5703)}}}},
        }
        text = format_daily_table(report)
        self.assertIn("2026-09-25 23.40 → 2026-09-29 23.70", text)
        self.assertIn("24,510.0898 → 2026-09-29 24,523.5703", text)
        self.assertIn("2026-09-25 23.40 → 缺失 缺失", text)
        self.assertNotIn(" 0.00", text)


if __name__ == "__main__":
    unittest.main()
