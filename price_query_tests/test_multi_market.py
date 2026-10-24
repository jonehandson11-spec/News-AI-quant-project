import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from news_price_query.assets import RESEARCH_ASSETS, market_of_ticker, resolve_asset
from news_price_query.daily_report import daily_report
from news_price_query.fx import convert_return_pct
from news_price_query.market_calendars import calendar_for
from news_price_query.news import NewsItem, find_direct_match
from news_price_query.price_store import LocalDbPriceProvider, PriceStore
from news_price_query.price_update import UniverseEntry, read_universe, update_prices
from web.service import QueryService

HK = ZoneInfo("Asia/Hong_Kong")
NY = ZoneInfo("America/New_York")
DATA = Path(__file__).resolve().parents[1] / "news_price_query" / "data"


def hk(*parts):
    return datetime(*parts, tzinfo=HK)


def news(title, body=""):
    return NewsItem("n1", "test", title, body, hk(2026, 10, 1, 9), "https://example.com", "zh")


class MarketCalendarTests(unittest.TestCase):
    def test_us_premarket_and_after_close_in_hk_time(self):
        nyse = calendar_for("XNYS")
        # 2026-10-21 20:00 HKT = 08:00 New York (EDT): before the open, t0 is the same US day.
        self.assertEqual(nyse.session_of(hk(2026, 10, 21, 20)), "pre_open")
        self.assertEqual(nyse.t0(hk(2026, 10, 21, 20)), date(2026, 10, 21))
        # 2026-10-22 05:30 HKT = 2026-10-21 17:30 New York: after the close -> next US trading day.
        self.assertEqual(nyse.session_of(hk(2026, 10, 22, 5, 30)), "after_close")
        self.assertEqual(nyse.t0(hk(2026, 10, 22, 5, 30)), date(2026, 10, 22))
        self.assertEqual(nyse.session_of(hk(2026, 10, 22, 3, 59)), "regular_session")

    def test_us_daylight_saving_change(self):
        nyse = calendar_for("XNYS")
        # After 2026-11-01 New York is UTC-5, so the 16:00 close is 05:00 HKT, not 04:00.
        self.assertEqual(nyse.session_of(hk(2026, 11, 3, 4, 30)), "regular_session")
        self.assertEqual(nyse.t0(hk(2026, 11, 3, 4, 30)), date(2026, 11, 2))
        self.assertEqual(nyse.t0(hk(2026, 11, 3, 5, 30)), date(2026, 11, 3))
        self.assertEqual(nyse.day(date(2026, 11, 2)).close_at().astimezone(HK), hk(2026, 11, 3, 5))

    def test_us_holiday_and_half_day(self):
        nyse = calendar_for("XNYS")
        self.assertFalse(nyse.is_trading_day(date(2026, 11, 26)))  # Thanksgiving
        friday = nyse.day(date(2026, 11, 27))
        self.assertTrue(friday.half_day)
        self.assertEqual(friday.close.isoformat(), "13:00:00")
        self.assertFalse(nyse.is_trading_day(date(2001, 9, 11)))

    def test_toronto_and_saudi_calendars(self):
        tsx = calendar_for("XTSE")
        self.assertFalse(tsx.is_trading_day(date(2026, 10, 12)))  # Canadian Thanksgiving
        self.assertTrue(tsx.is_trading_day(date(2026, 10, 13)))
        tadawul = calendar_for("XSAU")
        self.assertTrue(tadawul.is_trading_day(date(2026, 10, 25)))   # Sunday
        self.assertFalse(tadawul.is_trading_day(date(2026, 10, 23)))  # Friday
        self.assertEqual(tadawul.day(date(2026, 10, 22)).close_at().astimezone(HK), hk(2026, 10, 22, 20))

    def test_unknown_market_is_not_guessed(self):
        with self.assertRaises(NotImplementedError):
            calendar_for("XLON")


class ResearchRegistryTests(unittest.TestCase):
    def test_nineteen_targets_with_stages(self):
        research = [a for a in RESEARCH_ASSETS if a.role != "benchmark"]
        self.assertEqual(len(research), 19)
        self.assertEqual(len({a.stage for a in research}), 10)
        futures = [a.ticker for a in research if a.asset_type == "futures"]
        self.assertEqual(futures, ["BZ=F", "CL=F", "RB=F", "HO=F", "ICE-LSGO"])
        self.assertTrue(all(not a.queryable for a in research if a.asset_type == "futures"))
        pool = [e.ticker for e in read_universe(DATA / "universe_oil_research.csv")]
        self.assertEqual(sorted(pool), sorted(a.ticker for a in research))

    def test_resolution_and_markets(self):
        self.assertEqual(resolve_asset("沙特阿美").ticker, "2222.SR")
        self.assertEqual(resolve_asset("CNQ").ticker, "CNQ.TO")
        self.assertEqual(resolve_asset("中海油").ticker, "0883.HK")
        self.assertEqual(resolve_asset("Couche-Tard").ticker, "ATD.TO")
        self.assertEqual(market_of_ticker("XOM"), ("XNYS", "USD"))
        self.assertEqual(market_of_ticker("ATD.TO"), ("XTSE", "CAD"))
        self.assertEqual(market_of_ticker("2222.SR"), ("XSAU", "SAR"))
        self.assertEqual(market_of_ticker("0883.HK"), ("XHKG", "HKD"))
        for ticker in ("CL=F", "ICE-LSGO", "SHEL.L"):
            with self.assertRaises(NotImplementedError):
                market_of_ticker(ticker)

    def test_cnooc_alias_does_not_match_oilfield_services(self):
        cnooc = resolve_asset("0883.HK")
        self.assertIsNone(find_direct_match(news("中海油田服务发布三季度业绩"), cnooc))
        self.assertIsNone(find_direct_match(news("中海油服中标海外钻井合同"), cnooc))
        self.assertIsNotNone(find_direct_match(news("中海油田服务与中海油签署合同"), cnooc))
        self.assertEqual(find_direct_match(news("中海油公布产量"), cnooc).evidence, "中海油")

    def test_sinopec_alias_does_not_match_kantons(self):
        sinopec = resolve_asset("0386.HK")
        self.assertIsNone(find_direct_match(news("中石化冠德公布业绩"), sinopec))
        self.assertIsNotNone(find_direct_match(news("中石化上调油价"), sinopec))

    def test_fx_interface(self):
        self.assertAlmostEqual(convert_return_pct(10.0, -5.0), 4.5)


# Synthetic fixture prices (not market data).
US_DAYS = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-19", "2026-10-20",
           "2026-10-21", "2026-10-22", "2026-10-23"]


def series(base, step):
    return [(d, base + i * step, base + i * step, 100) for i, d in enumerate(US_DAYS)]


def fake_downloader(tables, calls):
    def download(ticker, start, end):
        calls.append((ticker, start, end))
        return [{"date": d, "close": c, "adj_close": a, "volume": v}
                for d, c, a, v in tables.get(ticker, []) if start <= d < end]
    return download


class MultiMarketUpdateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "prices.sqlite3"
        self.store = PriceStore(self.db)
        tables = {"XOM": series(100.0, 1.0), "SPY": series(500.0, 5.0), "XLE": series(80.0, 0.0),
                  "ATD.TO": series(70.0, 0.7), "XIU.TO": series(40.0, 0.0), "^GSPTSE": series(30000.0, 0.0)}
        tables["XOM"].append(("2026-10-24", 999.0, 999.0, 100))  # Saturday filler: dropped
        self.calls = []
        # 2026-10-24 01:00 HKT = 2026-10-23 13:00 New York: the 10-23 US session is still open.
        self.now = hk(2026, 10, 24, 1)
        self.result = update_prices(self.store, [UniverseEntry("XOM"), UniverseEntry("ATD.TO"),
                                                 UniverseEntry("BZ=F")],
                                    fake_downloader(tables, self.calls), now=self.now, start="2026-10-01")
        self.summary = {t.ticker: t for t in self.result["tickers"]}

    def test_benchmarks_per_market_and_futures_skipped(self):
        self.assertTrue({"SPY", "XLE", "XIU.TO", "^GSPTSE", "2800.HK", "^HSI"} <= set(self.summary))
        self.assertIsNotNone(self.summary["BZ=F"].skipped)
        self.assertEqual(self.summary["XOM"].market, "XNYS")
        self.assertEqual(self.summary["ATD.TO"].market, "XTSE")
        instruments = {i.ticker: i for i in self.store.instruments()}
        self.assertEqual((instruments["XOM"].currency, instruments["ATD.TO"].currency), ("USD", "CAD"))

    def test_today_is_the_exchange_local_date(self):
        xom = self.summary["XOM"]
        self.assertEqual(xom.last_date, "2026-10-22")
        self.assertEqual([c[2] for c in self.calls if c[0] == "XOM"], ["2026-10-23"])
        self.assertEqual(xom.missing_trading_days, [])

    def test_us_daily_report_uses_us_calendar_and_benchmarks(self):
        provider = LocalDbPriceProvider(self.db)
        # 2026-10-20 05:30 HKT = 2026-10-19 17:30 New York, after the close: t0 = 10-20, baseline 10-19.
        report = daily_report(provider, resolve_asset("XOM"), hk(2026, 10, 20, 5, 30),
                              as_of=hk(2026, 10, 24, 1),
                              benchmarks=("SPY", "XLE", "2800.HK"))
        self.assertEqual(report["t0"], "2026-10-20")
        self.assertEqual(report["baseline"]["observed_at"][:10], "2026-10-19")
        self.assertEqual(report["baseline"]["price"], 103.0)
        self.assertEqual(report["currency"], "USD")
        self.assertEqual(report["market"], "XNYS")
        self.assertTrue(report["published_at_local"].startswith("2026-10-19T17:30:00-04:00"))
        self.assertEqual(set(report["benchmarks"]), {"SPY", "XLE"})  # cross-market 2800.HK skipped
        t1 = report["windows"]["after_t1"]
        # after_t1 = first trading day after t0 (10-21, close 105); XLE is flat in the fixture.
        self.assertEqual(t1["observed_at"][:10], "2026-10-21")
        self.assertAlmostEqual(t1["return_pct"], (105 / 103 - 1) * 100)
        self.assertAlmostEqual(report["benchmarks"]["XLE"]["windows"]["after_t1"]["excess_return_pct"],
                               (105 / 103 - 1) * 100, places=4)

    def test_service_catalog_has_stages_and_disabled_futures(self):
        provider = LocalDbPriceProvider(self.db)
        service = QueryService(lambda: provider, news_source=lambda: [], now=lambda: hk(2026, 10, 24, 1),
                               mode="daily", instruments=provider.store.instruments,
                               price_source="本地价格库", data_version=provider.data_version)
        catalog = {row["ticker"]: row for row in service.catalog()}
        self.assertEqual(catalog["XOM"]["stage"], "综合油气")
        self.assertEqual(catalog["ATD.TO"]["currency"], "CAD")
        self.assertFalse(catalog["BZ=F"]["queryable"])
        self.assertNotIn("SPY", catalog)
        with self.assertRaisesRegex(ValueError, "下一块"):
            service.query("BZ=F", "2026-10-20T05:30:00+08:00")
        payload = service.query("XOM", "2026-10-20T05:30:00+08:00")
        self.assertEqual(set(payload["benchmarks"]), {"SPY", "XLE"})


if __name__ == "__main__":
    unittest.main()
