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
            "ticker": "0883.HK", "published_at_local": "2026-09-28T11:17:42+08:00", "currency": "HKD", "price_unit": "HKD 港元",
            "windows": {
                "after_t1": {"status": "on_date", "return_pct": 1.2821, "adj_return_pct": 1.2821,
                             **_span("after_t1", "2026-09-25", 23.4, "2026-09-29", 23.7),
                             **{f"adj_{k}": v for k, v in _span("after_t1", "2026-09-25", 23.4, "2026-09-29", 23.7).items()}},
                "after_t3": {"status": "missing", "return_pct": None, "adj_return_pct": None,
                             **_span("after_t3", "2026-09-25", 23.4, None, None)},
            },
            "benchmarks": {"^HSI": {"price_unit": "点", "windows": {"after_t1": {
                "return_pct": 0.055, "excess_return_pct": 1.23, "adj_excess_return_pct": 1.23,
                **_span("after_t1", "2026-09-25", 24510.0898, "2026-09-29", 24523.5703)}}}},
        }
        text = format_daily_table(report)
        self.assertIn("价格单位：HKD 港元", text)
        self.assertIn("2026-09-25 23.40 HKD 港元 → 2026-09-29 23.70 HKD 港元", text)
        self.assertIn("24,510.0898 点 → 2026-09-29 24,523.5703 点", text)
        self.assertIn("2026-09-25 23.40 HKD 港元 → 缺失 缺失", text)
        self.assertNotIn(" 0.00", text)


    def test_price_unit_labels(self):
        from news_price_query.assets import resolve_asset
        from news_price_query.daily_report import price_unit
        expected = {"0883.HK": "HKD 港元", "XOM": "USD 美元", "CNQ.TO": "CAD 加元", "2222.SR": "SAR 里亚尔",
                    "BZ=F": "美元/桶", "CL=F": "美元/桶", "RB=F": "美元/加仑", "HO=F": "美元/加仑", "^HSI": "点"}
        for ticker, label in expected.items():
            self.assertEqual(price_unit(resolve_asset(ticker)), label, ticker)


if __name__ == "__main__":
    unittest.main()
