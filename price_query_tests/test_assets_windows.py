import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from news_price_query.assets import resolve_asset
from news_price_query.windows import targets


HK = ZoneInfo("Asia/Hong_Kong")


class AssetWindowTests(unittest.TestCase):
    def test_byd_names_resolve_to_same_hk_equity(self):
        for name in ("比亚迪", "BYD", "1211.HK", "HK.01211"):
            with self.subTest(name=name):
                asset = resolve_asset(name)
                self.assertEqual(asset.ticker, "1211.HK")
                self.assertEqual(asset.futu_code, "HK.01211")
                self.assertEqual(asset.role, "equity")

    def test_unknown_asset_does_not_guess(self):
        with self.assertRaisesRegex(ValueError, "Unknown asset"):
            resolve_asset("锂电池")

    def test_unlisted_hk_ticker_is_accepted(self):
        asset = resolve_asset("2318.HK")
        self.assertEqual(asset.ticker, "2318.HK")
        self.assertEqual(asset.futu_code, "HK.02318")
        self.assertEqual(resolve_asset("HK.02318"), asset)

    def test_month_end_is_clamped_and_all_windows_exist(self):
        result = targets(datetime(2026, 3, 31, 10, 0, tzinfo=HK))
        self.assertEqual([item.label for item in result], [
            "before_1m", "before_3m", "before_6m", "after_1h", "after_3h",
            "after_12h", "after_24h", "after_3d", "after_1w",
        ])
        self.assertEqual(result[0].target.isoformat(), "2026-02-28T10:00:00+08:00")
        self.assertEqual(result[1].target.isoformat(), "2025-12-31T10:00:00+08:00")
        self.assertEqual(result[-1].target.isoformat(), "2026-04-07T10:00:00+08:00")

    def test_timestamp_must_have_timezone(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            targets(datetime(2026, 9, 25, 10, 0))


if __name__ == "__main__":
    unittest.main()
