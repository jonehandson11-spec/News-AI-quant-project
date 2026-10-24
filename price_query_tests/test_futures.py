"""Futures examples use synthetic prices, never market observations."""

import tempfile
import sqlite3
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.assets import Asset, market_of_ticker, resolve_asset
from news_price_query.daily_report import daily_report
from news_price_query.futures import front_contract, last_trade_date, roll_dates, root_for_ticker
from news_price_query.market_calendars import calendar_for
from news_price_query.news import NewsItem
from news_price_query.price_store import FuturesRollRow, LocalDbPriceProvider, PriceStore
from news_price_query.price_update import UniverseEntry, update_prices
from news_price_query.windows import TradingWindowConfig
from web.service import QueryService


NY = ZoneInfo("America/New_York")
HK = ZoneInfo("Asia/Hong_Kong")


class FuturesRulesTests(unittest.TestCase):
    def test_expiries_and_front_contract(self):
        expected = {("CL", 2026, 11): date(2026, 10, 20),
                    ("CL", 2026, 12): date(2026, 11, 20),
                    ("CL", 2020, 5): date(2020, 4, 21),
                    ("HO", 2026, 11): date(2026, 10, 30),
                    ("RB", 2026, 11): date(2026, 10, 30),
                    ("HO", 2026, 12): date(2026, 11, 30),
                    ("BZ", 2026, 12): date(2026, 10, 30),
                    ("BZ", 2026, 10): date(2026, 8, 28),
                    ("BZ", 2016, 1): date(2015, 12, 16)}
        for args, wanted in expected.items():
            with self.subTest(args=args):
                self.assertEqual(last_trade_date(*args), wanted)
        self.assertEqual(front_contract("CL", date(2026, 10, 20)), "CLX26")
        self.assertEqual(front_contract("CL", date(2026, 10, 21)), "CLZ26")
        self.assertEqual(front_contract("BZ", date(2026, 10, 21)), "BZZ26")
        self.assertEqual(front_contract("BZ", date(2000, 1, 3)), "BZG00")
        self.assertEqual(root_for_ticker("CL=F"), "CL")
        self.assertIsNone(root_for_ticker("XX=F"))

    def test_roll_date_is_first_nymex_session_after_expiry(self):
        rolls = roll_dates("CL", date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual([(r.roll_date, r.from_contract, r.to_contract, r.last_trade_date)
                          for r in rolls],
                         [(date(2026, 10, 21), "CLX26", "CLZ26", date(2026, 10, 20))])

    def test_calendars_and_exchange_close(self):
        nymex = calendar_for("NYMEX")
        self.assertFalse(nymex.is_trading_day(date(2026, 1, 19)))
        self.assertFalse(nymex.is_trading_day(date(2026, 4, 3)))
        self.assertTrue(nymex.is_trading_day(date(2025, 1, 9)))
        self.assertIn("CMEGlobex_CL", nymex.day(date(2025, 1, 9)).source_url)
        self.assertEqual(nymex.day(date(2026, 10, 21)).close.isoformat(), "14:30:00")
        self.assertEqual(nymex.t0(datetime(2026, 10, 21, 15, tzinfo=NY)), date(2026, 10, 22))
        self.assertEqual(nymex.t0(datetime(2026, 10, 22, 3, tzinfo=HK)), date(2026, 10, 22))
        self.assertEqual(nymex.t0(datetime(2026, 10, 21, 14, tzinfo=NY)), date(2026, 10, 21))
        self.assertFalse(calendar_for("XLON").is_trading_day(date(2026, 8, 31)))

    def test_saudi_overrides(self):
        saudi = calendar_for("XSAU")
        for day in (date(2024, 4, 14), date(2022, 7, 13)):
            self.assertTrue(saudi.is_trading_day(day))
        for day in (date(2021, 5, 11), date(2022, 11, 23), date(2025, 6, 10)):
            self.assertFalse(saudi.is_trading_day(day))

    def test_registry(self):
        self.assertEqual(market_of_ticker("CL=F"), ("NYMEX", "USD"))
        self.assertEqual(resolve_asset("CL=F").unit, "USD/bbl")
        self.assertTrue(resolve_asset("CL=F").queryable)
        self.assertFalse(resolve_asset("ICE-LSGO").queryable)
        self.assertFalse(Asset("TEST", "", "equity", (), data_status="ok").queryable)
        with self.assertRaisesRegex(NotImplementedError, "no free data source"):
            market_of_ticker("ICE-LSGO")
        with self.assertRaises(NotImplementedError):
            market_of_ticker("XX=F")


class FuturesIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = PriceStore(Path(tmp.name) / "prices.sqlite3")
        self.now = datetime(2026, 10, 22, 13, tzinfo=NY)

    def _update(self):
        # Synthetic prices straddle the CLX26 -> CLZ26 roll; today's row must be excluded.
        prices = {"2026-10-16": 69.0, "2026-10-19": 70.0, "2026-10-20": 71.0,
                  "2026-10-21": 73.0, "2026-10-22": 74.0}
        def download(ticker, start, end):
            return [{"date": day, "close": price, "adj_close": price, "volume": 100.0}
                    for day, price in prices.items()] if ticker == "CL=F" else []
        return update_prices(self.store, [UniverseEntry("CL=F", aliases=("Synthetic Oil",)),
                                          UniverseEntry("ICE-LSGO")],
                             download, now=self.now, start="2026-10-16")

    def test_update_stores_roll_and_excludes_current_ny_date(self):
        result = self._update()
        summaries = {item.ticker: item for item in result["tickers"]}
        self.assertEqual([row.date for row in self.store.rows("CL=F")],
                         ["2026-10-16", "2026-10-19", "2026-10-20", "2026-10-21"])
        self.assertGreaterEqual(summaries["CL=F"].rolls, 1)
        self.assertEqual({item.ticker: item.role for item in self.store.instruments()}["CL=F"], "futures")
        self.assertEqual(summaries["CL=F"].dropped_unfinalized, ["2026-10-22"])
        self.assertIn("no free data", summaries["ICE-LSGO"].skipped)
        self.assertEqual([(r.roll_date, r.from_contract, r.to_contract)
                          for r in self.store.rolls("CL=F")],
                         [("2026-10-21", "CLX26", "CLZ26")])
        self.assertEqual(self.store.rolls("CL=F", end="2026-10-20"), [])

    def test_report_and_service(self):
        self._update()
        provider = LocalDbPriceProvider(store=self.store)
        config = TradingWindowConfig(before_days=(1,), after_days=(1,))
        # Default rule: CLX26 has only 1 trading day (10-20) left after the 10-19 baseline (< 5),
        # so the next contract CLZ26 is used and the result says so.
        switched = daily_report(provider, resolve_asset("CL=F"), datetime(2026, 10, 20, 14, tzinfo=NY),
                                as_of=self.now, config=config)
        self.assertEqual(switched["contract"]["contract"], "CLZ26")
        self.assertTrue(switched["contract"]["switched"])
        self.assertEqual(switched["contract"]["front_days_to_expiry"], 1)
        self.assertIn("临近到期，改用 CLZ26", switched["contract"]["note"])
        # min_days_to_expiry=0 keeps the front contract (used below to test 'expired').
        report = daily_report(provider, resolve_asset("CL=F"),
                              datetime(2026, 10, 20, 14, tzinfo=NY), as_of=self.now,
                              config=config, min_days_to_expiry=0)
        self.assertEqual(report["benchmarks"], {})
        self.assertEqual(report["unit"], "USD/bbl")
        self.assertEqual(report["asset_type"], "futures")
        # Fixed contract = CLX26 (front on the 10-19 baseline); it expires 10-20, so t+1 (10-21) is expired.
        self.assertEqual(report["contract"]["contract"], "CLX26")
        self.assertEqual(report["contract"]["last_trade_date"], "2026-10-20")
        after = report["windows"]["after_t1"]
        self.assertEqual(after["status"], "expired")
        self.assertIsNone(after["return_pct"])
        self.assertTrue(after["continuous"]["crosses_roll"])
        self.assertEqual(after["continuous"]["rolls"][0]["to_contract"], "CLZ26")
        self.assertAlmostEqual(after["continuous"]["return_pct"], (73.0 / 70.0 - 1) * 100)
        before = report["windows"]["before_1td"]
        self.assertAlmostEqual(before["return_pct"], (70.0 - 69.0) / 69.0 * 100)
        self.assertIn("derived:CL=F", before["source"])
        self.assertFalse(before["continuous"]["crosses_roll"])
        service = QueryService(lambda: provider, news_source=lambda: [], now=lambda: self.now,
                               mode="daily", instruments=self.store.instruments)
        catalog = {row["ticker"]: row for row in service.catalog()}
        self.assertTrue(catalog["CL=F"]["queryable"])
        self.assertEqual(catalog["CL=F"]["unit"], "USD/bbl")
        self.assertFalse(catalog["ICE-LSGO"]["queryable"])
        with self.assertRaisesRegex(ValueError, "无免费数据"):
            service.query("ICE-LSGO", "2026-10-20T14:00:00-04:00")

    def test_fixed_contract_uses_single_contract_across_roll(self):
        now = datetime(2026, 10, 24, 12, tzinfo=NY)
        continuous = {"2026-10-16": 69.0, "2026-10-19": 70.0, "2026-10-20": 71.0,
                      "2026-10-21": 73.0, "2026-10-22": 74.0, "2026-10-23": 75.0}
        clz = {"2026-10-16": 66.0, "2026-10-19": 67.0, "2026-10-20": 68.0,
               "2026-10-21": 73.0, "2026-10-22": 74.0, "2026-10-23": 75.0}
        calls = []

        def download(ticker, start, end):
            calls.append(ticker)
            table = {"CL=F": continuous, "CLZ26.NYM": clz}.get(ticker, {})
            return [{"date": d, "close": c, "adj_close": c, "volume": 10.0}
                    for d, c in table.items() if start <= d < end]

        update_prices(self.store, [UniverseEntry("CL=F")], download, now=now, start="2026-10-16")
        self.assertIn("CLZ26.NYM", calls)
        self.assertIn("CLF27.NYM", calls)  # next contract is fetched too
        clx = self.store.rows("CLX26.NYM")
        self.assertEqual([r.date for r in clx], ["2026-10-16", "2026-10-19", "2026-10-20"])
        self.assertTrue(all(r.source.startswith("derived:CL=F") for r in clx))
        direct = self.store.rows("CLZ26.NYM", "2026-10-16", "2026-10-16")[0]
        self.assertEqual((direct.close, direct.source), (66.0, "yfinance"))
        provider = LocalDbPriceProvider(store=self.store)
        # 2026-10-21 15:00 New York is after the 14:30 close: t0 = 10-22, baseline 10-21 (front = CLZ26).
        report = daily_report(provider, resolve_asset("CL=F"), datetime(2026, 10, 21, 15, tzinfo=NY),
                              as_of=now, config=TradingWindowConfig(before_days=(1,), after_days=(1,)))
        self.assertEqual(report["contract"]["contract"], "CLZ26")
        self.assertEqual(report["baseline"]["price"], 73.0)
        before = report["windows"]["before_1td"]
        self.assertAlmostEqual(before["return_pct"], (73.0 - 68.0) / 68.0 * 100)
        self.assertTrue(before["continuous"]["crosses_roll"])
        self.assertAlmostEqual(before["continuous"]["return_pct"], (73.0 - 71.0) / 71.0 * 100)
        self.assertAlmostEqual(report["windows"]["after_t1"]["return_pct"], (75.0 / 73.0 - 1) * 100)

    def test_days_to_expiry_threshold_is_exact(self):
        from news_price_query.daily_report import _days_to_expiry
        nymex = calendar_for("NYMEX")
        # CLX26 last trade date 2026-10-20.
        self.assertEqual(_days_to_expiry(nymex, date(2026, 10, 20), date(2026, 10, 20)), 0)
        self.assertEqual(_days_to_expiry(nymex, date(2026, 10, 13), date(2026, 10, 20)), 5)
        self.assertEqual(_days_to_expiry(nymex, date(2026, 10, 14), date(2026, 10, 20)), 4)
        prices = {d: 70.0 + i for i, d in enumerate(
            ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"])}
        def download(ticker, start, end):
            return [{"date": d, "close": c, "adj_close": c, "volume": 1.0}
                    for d, c in prices.items() if start <= d < end] if ticker == "CL=F" else []
        update_prices(self.store, [UniverseEntry("CL=F")], download,
                      now=datetime(2026, 10, 17, 12, tzinfo=NY), start="2026-10-09")
        provider = LocalDbPriceProvider(store=self.store)
        config = TradingWindowConfig(before_days=(1,), after_days=(1,))
        as_of = datetime(2026, 10, 17, 12, tzinfo=NY)
        # Baseline 10-13: 5 trading days left (14,15,16,19,20) -> keep CLX26.
        keep = daily_report(provider, resolve_asset("CL=F"), datetime(2026, 10, 14, 9, tzinfo=NY),
                            as_of=as_of, config=config)
        self.assertEqual((keep["contract"]["contract"], keep["contract"]["switched"]), ("CLX26", False))
        # Baseline 10-14: 4 left -> switch to CLZ26 (no CLZ26 prices in this fixture -> missing, not zero).
        switch = daily_report(provider, resolve_asset("CL=F"), datetime(2026, 10, 15, 9, tzinfo=NY),
                              as_of=as_of, config=config)
        self.assertEqual((switch["contract"]["contract"], switch["contract"]["switched"]), ("CLZ26", True))
        self.assertIsNone(switch["baseline"]["price"])
        self.assertIsNotNone(switch["contract"]["error"])
        self.assertIsNone(switch["windows"]["after_t1"]["return_pct"])
        # Configurable: threshold 4 keeps CLX26 on the 10-14 baseline.
        custom = daily_report(provider, resolve_asset("CL=F"), datetime(2026, 10, 15, 9, tzinfo=NY),
                              as_of=as_of, config=config, min_days_to_expiry=4)
        self.assertEqual(custom["contract"]["contract"], "CLX26")

    def test_negative_cl_settlement_is_stored_and_has_no_percentage_return(self):
        prices = {"2020-04-16": 0.0, "2020-04-17": 18.27,
                  "2020-04-20": -37.63, "2020-04-21": 10.01}

        def download(ticker, start, end):
            if ticker != "CL=F":
                return []
            return [{"date": day, "close": close, "adj_close": close, "volume": 100.0}
                    for day, close in prices.items() if start <= day < end]

        update_prices(self.store, [UniverseEntry("CL=F")], download,
                      now=self.now, start="2020-04-16")
        self.assertEqual(self.store.rows("CL=F", "2020-04-16", "2020-04-16")[0].close, 0.0)
        self.assertEqual(self.store.rows("CL=F", "2020-04-20", "2020-04-20")[0].close, -37.63)
        provider = LocalDbPriceProvider(store=self.store)
        config = TradingWindowConfig(before_days=(1,), after_days=(0,))
        for published, expected_baseline, expected_window in (
            (datetime(2020, 4, 17, 15, tzinfo=NY), 18.27, -37.63),
            (datetime(2020, 4, 20, 15, tzinfo=NY), -37.63, 10.01),
        ):
            with self.subTest(published=published):
                report = daily_report(provider, resolve_asset("CL=F"), published,
                                      as_of=self.now, config=config, min_days_to_expiry=0)
                window = report["windows"]["after_t0"]
                self.assertEqual(report["baseline"]["price"], expected_baseline)
                self.assertEqual(window["price"], expected_window)
                self.assertIsNone(window["return_pct"])
                self.assertIsNone(window["adj_return_pct"])
                self.assertEqual(window["flag"], "nonpositive_price")
                before = report["windows"]["before_1td"]
                self.assertIsNone(before["return_pct"])
                self.assertEqual(before["flag"], "nonpositive_price")

    def test_refetch_removes_stale_futures_rolls(self):
        self._update()
        self.store.upsert_rolls([FuturesRollRow("CL=F", "2026-10-16", "OLD", "NEW",
                                               "2026-10-15", "fixture", "fixture")])
        self.assertEqual(len(self.store.rolls("CL=F")), 2)
        self._update_for_refetch()
        self.assertEqual([roll.roll_date for roll in self.store.rolls("CL=F")], ["2026-10-21"])

    def _update_for_refetch(self):
        prices = {"2026-10-16": 69.0, "2026-10-19": 70.0, "2026-10-20": 71.0,
                  "2026-10-21": 73.0}
        def download(ticker, start, end):
            return [{"date": day, "close": price, "adj_close": price, "volume": 100.0}
                    for day, price in prices.items() if start <= day < end] if ticker == "CL=F" else []
        update_prices(self.store, [UniverseEntry("CL=F")], download, now=self.now,
                      start="2026-10-16", refetch=("CL=F",))

    def test_service_news_matches_futures_alias(self):
        self._update()
        article = NewsItem("oil-1", "fixture", "Synthetic Oil price rises", "",
                           datetime(2026, 10, 20, 9, tzinfo=HK), "https://example.test/oil", "en")
        service = QueryService(lambda: None, news_source=lambda: [article],
                               mode="daily", instruments=self.store.instruments)
        self.assertEqual([item["article_id"] for item in service.news("CL=F")], ["oil-1"])

    def test_existing_database_gains_roll_table_without_losing_data(self):
        path = self.store.path
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("INSERT INTO meta VALUES ('legacy_marker', 'preserved')")
            conn.execute("DROP TABLE futures_rolls")
        PriceStore(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='legacy_marker'").fetchone()[0],
                             "preserved")
            self.assertIsNotNone(conn.execute("SELECT name FROM sqlite_master "
                                              "WHERE type='table' AND name='futures_rolls'").fetchone())


if __name__ == "__main__":
    unittest.main()
