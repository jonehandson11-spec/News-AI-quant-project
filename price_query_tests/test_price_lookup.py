"""Direct quotes use synthetic SQLite prices and never require an article."""

from datetime import datetime
import http.client
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from zoneinfo import ZoneInfo

from news_price_query.price_store import DailyRow, HourlyRow, LocalDbPriceProvider, PriceStore
from web.server import create_server
from web.service import QueryService


HK = ZoneInfo("Asia/Hong_Kong")


class PriceLookupTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = PriceStore(Path(temp.name) / "prices.sqlite3")
        self.now = datetime(2026, 9, 28, 11, 15, tzinfo=HK)
        self.run = self.store.start_run("synthetic", None, 1, self.now)
        self.store.upsert_rows([
            self.daily("0700.HK", "2026-09-25", 100),
            self.daily("0700.HK", "2026-09-28", 110),  # Same day close is still in the future.
        ])
        self.store.upsert_hourly_rows([
            self.hourly("09:30", 101),
            self.hourly("10:30", 109),  # Second bar isn't complete at 11:15.
        ])
        self.service = QueryService(lambda: LocalDbPriceProvider(store=self.store),
                                    news_source=self.reject_news, now=lambda: self.now,
                                    instruments=self.store.instruments)

    @staticmethod
    def reject_news():
        raise AssertionError("A price lookup must not read the news feed")

    def daily(self, ticker, day, price):
        return DailyRow(ticker, "XHKG", "HKD", day, price, price, 100,
                        "synthetic", self.now.isoformat(), self.run)

    def hourly(self, time, price):
        local = datetime.fromisoformat(f"2026-09-28T{time}:00+08:00")
        utc = local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        return HourlyRow("0700.HK", "XHKG", "HKD", "1h", utc, str(HK), local.isoformat(),
                         price, price, price, price, price, 100, "synthetic",
                         self.now.isoformat(), self.run)

    def test_daily_lookup_uses_close_only_rows_and_declares_previous_session(self):
        price = self.service.price("700", "1d")
        self.assertEqual(price["symbol"], "0700.HK")
        self.assertEqual(price["currency"], "HKD")
        self.assertEqual(price["price"], 100)
        self.assertEqual(price["source"], "synthetic")
        self.assertEqual(price["observed_at"], "2026-09-25T16:00:00+08:00")
        self.assertEqual(price["price_status"], "prior_session")
        self.assertNotIn("market_state", price)

    def test_hourly_lookup_uses_bar_end_and_never_returns_incomplete_bar(self):
        price = self.service.price("腾讯", "1h", "2026-09-28", market_state=True)
        self.assertEqual(price["price"], 101)
        self.assertEqual(price["observed_at"], "2026-09-28T10:30:00+08:00")
        self.assertEqual(price["price_status"], "observed")
        factors = price["market_state"]
        self.assertEqual(factors["cutoff_date"], "2026-09-25")
        self.assertEqual(factors["benchmark"], "2800.HK")
        self.assertEqual(len(factors["fields"]), 7)
        self.assertEqual(factors["fields"]["liquidity_stress_z"]["missing_reason"], "ESTIMATOR_NOT_READY")
        self.assertGreater(factors["fields"]["data_coverage"]["value"], 0)
        self.assertEqual(factors["quality"]["data_coverage"],
                         {"value": None, "missing_reason": "REFERENCE_MISSING"})
        self.assertEqual(factors["quality"]["evidence_reliability"],
                         {"value": None, "missing_reason": "ESTIMATOR_NOT_READY"})

        self.now = datetime(2026, 9, 28, 16, 30, tzinfo=HK)
        repeated = self.service.price("腾讯", "1h", "2026-09-28", market_state=True,
                                      as_of=price["cutoff_at"])
        self.assertEqual(repeated["market_state"]["cutoff_date"], "2026-09-25")
        self.assertEqual(repeated["observed_at"], price["observed_at"])

    def test_quality_adapter_preserves_scope_and_does_not_average_factor_evidence(self):
        calls = []

        def quality_source(**context):
            calls.append(context)
            return {
                "evidence_reliability": {"by_variable": {
                    "momentum_z": {"value": 0.6, "method_version": "synthetic-v1"},
                    "realized_volatility": {"value": float("nan"), "method_version": "synthetic-v1"},
                }},
                "data_coverage": {"value": 0, "scope_id": "synthetic-scope", "method_version": "synthetic-v1"},
            }

        self.service.gvs_quality_source = quality_source
        self.service.price("WTI", "1d")
        self.assertEqual(calls, [])
        result = self.service.price("WTI", "1d", market_state=True)
        quality = result["market_state"]["quality"]
        self.assertEqual(result["asset_id"], "commodity:wti")
        self.assertEqual(calls[0]["asset"].asset_id, result["asset_id"])
        self.assertEqual(calls[0]["as_of"].isoformat(), result["cutoff_at"])
        self.assertEqual(calls[0]["profile_id"], result["market_state"]["profile_id"])
        self.assertIsNone(quality["evidence_reliability"]["value"])
        self.assertEqual(quality["evidence_reliability"]["by_variable"]["momentum_z"]["value"], 0.6)
        self.assertIsNone(quality["evidence_reliability"]["by_variable"]["realized_volatility"]["value"])
        self.assertEqual(quality["data_coverage"]["value"], 0)

        self.service.gvs_quality_source = lambda **_: {"data_coverage": {
            "value": 1, "method_version": "synthetic-v1",
        }}
        coverage = self.service.price("WTI", "1d", market_state=True)["market_state"]["quality"]["data_coverage"]
        self.assertEqual((coverage["value"], coverage["missing_reason"]), (None, "REFERENCE_MISSING"))

    def test_oil_catalog_and_missing_price(self):
        assets = {row["ticker"]: row for row in self.service.catalog()}
        self.assertEqual(sum(row["oil_research"] for row in assets.values()), 19)
        self.assertTrue(assets["0883.HK"]["oil_research"])
        self.assertFalse(assets["ICE-LSGO"]["queryable"])
        price = self.service.price("WTI", "1d", "2026-09-25")
        self.assertEqual(price["symbol"], "CL=F")
        self.assertEqual(price["unit"], "USD/bbl")
        self.assertIsNone(price["price"])
        self.assertEqual(price["price_status"], "missing")

    def test_daily_lookup_survives_hourly_fetch_failure(self):
        def ensure(store, ticker, *, now):
            self.assertEqual(ticker, "0857.HK")
            store.upsert_rows([self.daily(ticker, "2026-09-25", 8)])
            raise ValueError("No hourly coverage")

        self.service.yahoo_ondemand = True
        self.service.ondemand_ensure = ensure
        result = self.service.price("857", "1d", "2026-09-25")
        self.assertEqual(result["price"], 8)
        self.assertEqual(result["price_status"], "observed")

    def test_http_direct_price_and_invalid_query(self):
        with create_server(self.service, port=0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request("GET", "/api/price?symbol=0700.HK&interval=1h&date=2026-09-28")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["price"], 101)
                conn.request("GET", "/api/price?symbol=0700.HK&interval=1h&date=2026-09-29")
                response = conn.getresponse()
                self.assertEqual(response.status, 400)
                response.read()
            finally:
                conn.close()
                server.shutdown()
                thread.join(timeout=5)
