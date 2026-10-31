"""crosscheck_hourly 的比对逻辑；用合成数据，不联网。"""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from news_price_query.prices import PriceBar
from scripts.crosscheck_hourly import compare, parse_point

HK = ZoneInfo("Asia/Hong_Kong")


class Provider:
    def __init__(self, bars):
        self._bars = bars

    def bars(self, ticker, start, end, interval):
        return [b for b in self._bars
                if b.ticker == ticker and b.interval == interval and start <= b.timestamp <= end]


def bar(stamp, price, ticker="0883.HK"):
    return PriceBar(ticker, "hour", datetime.fromisoformat(stamp), price,
                    "synthetic fixture (not real prices)", "raw", 100)


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.provider = Provider([bar("2026-10-22T09:30:00+08:00", 23.04),
                                  bar("2026-10-22T10:30:00+08:00", 23.50)])
        self.event = datetime(2026, 10, 22, 10, 31, tzinfo=HK)
        self.as_of = datetime(2026, 10, 23, 12, 0, tzinfo=HK)

    def test_matching_reference_is_consistent(self):
        reference = {("0883.HK", "2026-10-22T10:30:00+08:00"): 23.04}
        rows = compare(self.provider, [("0883.HK", self.event)], reference, self.as_of)
        self.assertEqual(rows[0]["system_bar_end"], "2026-10-22T10:30:00+08:00")
        self.assertEqual(rows[0]["system_close"], 23.04)
        self.assertEqual(rows[0]["verdict"], "一致")
        self.assertEqual(rows[0]["diff"], 0.0)

    def test_close_but_not_equal_is_flagged(self):
        reference = {("0883.HK", "2026-10-22T10:30:00+08:00"): 23.05}
        rows = compare(self.provider, [("0883.HK", self.event)], reference, self.as_of)
        self.assertEqual(rows[0]["verdict"], "不一致")
        self.assertAlmostEqual(rows[0]["diff"], -0.01, places=6)

    def test_tolerance_accepts_a_tick(self):
        reference = {("0883.HK", "2026-10-22T10:30:00+08:00"): 23.05}
        rows = compare(self.provider, [("0883.HK", self.event)], reference, self.as_of, tolerance=0.01)
        self.assertEqual(rows[0]["verdict"], "一致")

    def test_missing_reference_does_not_fake_a_pass(self):
        rows = compare(self.provider, [("0883.HK", self.event)], {}, self.as_of)
        self.assertEqual(rows[0]["verdict"], "缺参考值")
        self.assertIsNone(rows[0]["diff"])

    def test_parse_point_requires_offset(self):
        self.assertEqual(parse_point("0883.HK=2026-10-22T10:31:00+08:00")[0], "0883.HK")
        with self.assertRaises(Exception):
            parse_point("0883.HK=2026-10-22T10:31:00")

    def test_parse_point_keeps_equals_sign_in_futures_ticker(self):
        ticker, moment = parse_point("BZ=F=2026-10-28T13:01:00-04:00")
        self.assertEqual(ticker, "BZ=F")
        self.assertEqual(moment.hour, 13)


if __name__ == "__main__":
    unittest.main()
