import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, stdev
from zoneinfo import ZoneInfo

from news_price_query.assets import Asset, resolve_asset
from news_price_query.market_calendars import calendar_for
from news_price_query.market_state import BRENT_WTI_PROFILE_ID, PROFILE_ID, csv_headers, csv_values, market_state
from news_price_query.price_store import DailyRow, PriceStore
from web.service import QueryService
from web.server import _market_state_flag
from zoneinfo import ZoneInfo


NY = ZoneInfo("America/New_York")


class MarketStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = PriceStore(Path(self.tmp.name) / "prices.sqlite3")
        self.calendar = calendar_for("XNYS")
        self.dates = self.calendar._trading[-320:]
        self.as_of = datetime.combine(self.dates[-1], datetime.min.time(), NY) + timedelta(days=1)
        self.published = datetime.combine(self.dates[-1], datetime.min.time(), NY).replace(hour=17)
        self.run_id = self.store.start_run("test", None, 1, self.as_of)

    def add_prices(self, ticker, prices, *, volumes=None, dates=None, market="XNYS", adjusted=None):
        dates = dates or self.dates[-len(prices):]
        volumes = volumes if volumes is not None else [100.0] * len(prices)
        adjusted = adjusted if adjusted is not None else prices
        rows = [DailyRow(ticker, market, "USD", day.isoformat(), price, adj,
                         volume, "test", self.as_of.isoformat(), self.run_id)
                for day, price, adj, volume in zip(dates, prices, adjusted, volumes)]
        self.store.upsert_rows(rows)

    def test_brent_uses_raw_wti_daily_closes_with_a_fixed_cutoff(self):
        calendar = calendar_for("NYMEX")
        cutoff = date(2026, 10, 29)
        dates = [calendar.shift_trading_days(cutoff, i) for i in range(-273, 2)]
        brent, wti = [80.0], [72.0]
        for i in range(273):
            change = (0.025, -0.012, 0.004, 0.001)[i % 4]
            wti.append(wti[-1] * (1 + change))
            brent.append(brent[-1] * (1 + 1.5 * change))
        self.add_prices("BZ=F", [*brent, 1000.0], dates=dates, market="NYMEX")
        self.add_prices("CL=F", [*wti, 2000.0], dates=dates, market="NYMEX", adjusted=[72.0] * len(dates))
        event = datetime(2026, 10, 29, 23, 59, tzinfo=NY)
        asset = resolve_asset("BZ=F")
        result = market_state(self.store, asset, event, as_of=event + timedelta(days=1))
        self.assertEqual(result["profile_id"], BRENT_WTI_PROFILE_ID)
        self.assertEqual(result["cutoff_date"], "2026-10-29")
        self.assertEqual(result["benchmark"], "CL=F")
        beta = result["fields"]["benchmark_beta"]
        momentum = result["fields"]["benchmark_momentum_z"]
        returns = [wti[i] / wti[i - 21] - 1 for i in range(21, 274)]
        self.assertEqual(beta["n_pairs"], 63)
        self.assertAlmostEqual(beta["value"], 1.5, places=12)
        self.assertAlmostEqual(momentum["value"], (returns[-1] - mean(returns[:-1])) / stdev(returns[:-1]))
        self.assertEqual(momentum["window_end"], "2026-10-29")

        # A nonpositive close inside both windows must not create a return over the gap.
        self.add_prices("CL=F", [-15.0], dates=[dates[-10]], market="NYMEX")
        fields = market_state(self.store, asset, event, as_of=event)["fields"]
        for key in ("benchmark_beta", "benchmark_momentum_z"):
            self.assertEqual(fields[key]["missing_reason"], "INSUFFICIENT_HISTORY")
            self.assertIsNone(fields[key]["value"])

        self.add_prices("CL=F", [0.0], dates=[cutoff], market="NYMEX")
        fields = market_state(self.store, asset, event, as_of=event)["fields"]
        self.assertEqual(fields["benchmark_beta"]["missing_reason"], "NOT_OBSERVED")
        self.assertEqual(fields["benchmark_momentum_z"]["missing_reason"], "NOT_OBSERVED")

    def test_known_sequence_metrics_and_exact_beta(self):
        # The final 64 benchmark returns alternate, and asset returns are exactly 2x.
        bench = [100.0]
        asset = [50.0]
        for i in range(319):
            r = (0.01, -0.005, 0.002)[i % 3]
            bench.append(bench[-1] * (1 + r))
            asset.append(asset[-1] * (1 + 2 * r))
        self.add_prices("SPY", bench)
        self.add_prices("XOM", asset, volumes=[float(i + 1) for i in range(320)])
        result = market_state(self.store, Asset("XOM", "", "equity", ("XOM",), market="XNYS"),
                              self.published, as_of=self.as_of)
        fields = result["fields"]
        self.assertAlmostEqual(fields["benchmark_beta"]["value"], 2.0, places=12)
        self.assertEqual(fields["benchmark_beta"]["n_pairs"], 63)
        prices = asset
        mom = [prices[i] / prices[i - 21] - 1 for i in range(21, len(prices))]
        self.assertAlmostEqual(fields["momentum_z"]["current_return"], mom[-1])
        self.assertAlmostEqual(fields["momentum_z"]["reference_mean"], mean(mom[-253:-1]))
        self.assertAlmostEqual(fields["realized_volatility"]["value"], stdev([prices[i] / prices[i-1] - 1 for i in range(-21, 0)]))
        current_vol = mean(range(300, 321))
        ref_vol = mean(range(48, 300))
        self.assertAlmostEqual(fields["relative_trading_activity"]["value"], current_vol / ref_vol)
        self.assertEqual(result["benchmark"], "SPY")
        json.dumps(result, allow_nan=False)

    def test_index_volume_not_applicable_and_short_history(self):
        self.add_prices("^HSI", [100.0 + i for i in range(40)])
        index = market_state(self.store, Asset("^HSI", "", "benchmark", (), market="XHKG"),
                             self.published, as_of=self.as_of)
        self.assertEqual(index["fields"]["relative_trading_activity"]["missing_reason"], "NOT_APPLICABLE")
        self.assertEqual(index["fields"]["momentum_z"]["missing_reason"], "INSUFFICIENT_HISTORY")

    def test_unsupported_market_and_futures_have_no_benchmark(self):
        for asset in (Asset("ABC.SR", "", "equity", (), market="XSAU"),
                      resolve_asset("CL=F"),
                      Asset("CLZ26.NYM", "", "futures", (), market="NYMEX", asset_type="futures")):
            result = market_state(self.store, asset, self.published, as_of=self.as_of)
            self.assertIsNone(result["benchmark"])
            self.assertEqual(result["fields"]["benchmark_beta"]["missing_reason"], "NOT_APPLICABLE")

    def test_future_extreme_row_does_not_change_result(self):
        self.add_prices("XOM", [100.0 + i * 0.2 for i in range(320)])
        asset = Asset("XOM", "", "equity", (), market="XNYS")
        before = market_state(self.store, asset, self.published, as_of=self.as_of)
        future = self.dates[-1] + timedelta(days=1)
        row = DailyRow("XOM", "XNYS", "USD", future.isoformat(), 1e99, 1e99,
                       1e99, "test", self.as_of.isoformat(), self.run_id)
        self.store.upsert_rows([row])
        after = market_state(self.store, asset, self.published, as_of=self.as_of)
        self.assertEqual(before, after)

    def test_missing_cutoff_price_marks_price_metrics_not_observed(self):
        self.add_prices("XOM", [100.0 + i for i in range(39)], dates=self.dates[-40:-1])
        asset = Asset("XOM", "", "equity", (), market="XNYS")
        result = market_state(self.store, asset, self.published, as_of=self.as_of)
        for key in ("momentum_z", "realized_volatility", "relative_trading_activity",
                    "benchmark_beta", "benchmark_momentum_z"):
            self.assertIsNone(result["fields"][key]["value"], key)
            self.assertEqual(result["fields"][key]["missing_reason"], "NOT_OBSERVED", key)

    def test_hong_kong_cutoff_uses_last_completed_close(self):
        from zoneinfo import ZoneInfo
        published = datetime(2026, 9, 28, 11, 17, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        as_of = datetime(2026, 9, 29, 12, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
        result = market_state(self.store, resolve_asset("0883.HK"), published, as_of=as_of)
        self.assertEqual(result["cutoff_date"], "2026-09-25")

    def test_empty_store_serializes_and_coverage_is_zero(self):
        result = market_state(self.store, resolve_asset("0700.HK"), self.published,
                              as_of=self.as_of, calendar=self.calendar)
        self.assertEqual(result["profile_id"], PROFILE_ID)
        self.assertEqual(result["fields"]["data_coverage"]["value"], 0.0)
        self.assertIsNotNone(result["fields"]["data_coverage"]["first_expected_date"])
        self.assertEqual(len(result["fields"]["data_coverage"]["missing_dates"]), 20)
        self.assertEqual(result["fields"]["momentum_z"]["missing_reason"], "NOT_OBSERVED")
        json.dumps(result, allow_nan=False)

    def test_service_flag_adds_market_state_only_when_enabled(self):
        store = self.store
        event_hk = self.published.astimezone(ZoneInfo("Asia/Hong_Kong"))
        class Provider:
            def __init__(self): self.store = store
            def bars(self, ticker, start, end, interval):
                from news_price_query.prices import PriceBar
                if interval != "minute": return []
                return [PriceBar(ticker, interval, event_hk - timedelta(minutes=1), 100.0, "test")]
        service = QueryService(Provider, now=lambda: self.as_of, mode="minute")
        base = service.query("0700.HK", self.published.isoformat())
        enabled = service.query("0700.HK", self.published.isoformat(), market_state=True)
        self.assertNotIn("market_state", base)
        self.assertEqual(enabled["market_state"]["profile_id"], PROFILE_ID)

    def test_service_without_store_returns_market_state_error(self):
        event_hk = self.published.astimezone(ZoneInfo("Asia/Hong_Kong"))
        class Provider:
            def bars(self, ticker, start, end, interval):
                from news_price_query.prices import PriceBar
                if interval != "minute": return []
                return [PriceBar(ticker, interval, event_hk - timedelta(minutes=1), 100.0, "test")]
        service = QueryService(Provider, now=lambda: self.as_of, mode="minute")
        result = service.query("0700.HK", self.published.isoformat(), market_state=True)
        self.assertEqual(result["market_state"], {"profile_id": PROFILE_ID, "error": "需要本地价格库"})

    def test_server_rejects_non_boolean_market_state(self):
        for value in ("true", 1, None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "market_state 必须是布尔值"):
                _market_state_flag({"market_state": value})
        self.assertIs(_market_state_flag({}), False)

    def test_csv_values_leave_missing_blank_and_keep_zero_coverage(self):
        headers = csv_headers()
        self.assertEqual(headers[0], "momentum_z")
        self.assertEqual(headers[1], "momentum_z_missing_reason")
        self.assertEqual(headers[-3:], ("profile_id", "cutoff_date", "benchmark"))
        values = csv_values({
            "profile_id": PROFILE_ID,
            "cutoff_date": "2026-09-25",
            "benchmark": "2800.HK",
            "fields": {
                "momentum_z": {"value": -0.6595588260084632, "missing_reason": None},
                "realized_volatility": {"value": 0.01535311845855941},
                "relative_trading_activity": {"value": 0.5807046764688161},
                "liquidity_stress_z": {"value": None, "missing_reason": "ESTIMATOR_NOT_READY"},
                "benchmark_beta": {"value": 0.6884755979714041},
                "benchmark_momentum_z": {"value": -0.8964502213602309},
                "data_coverage": {"value": 1.0},
            },
        })
        self.assertAlmostEqual(values["momentum_z"], -0.6596, places=4)
        self.assertAlmostEqual(values["realized_volatility"], 0.015353, places=6)
        self.assertAlmostEqual(values["relative_trading_activity"], 0.5807, places=4)
        self.assertIsNone(values["liquidity_stress_z"])
        self.assertEqual(values["liquidity_stress_z_missing_reason"], "ESTIMATOR_NOT_READY")
        self.assertAlmostEqual(values["benchmark_beta"], 0.6885, places=4)
        self.assertAlmostEqual(values["benchmark_momentum_z"], -0.8965, places=4)
        self.assertEqual(values["data_coverage"], 1.0)
        self.assertEqual(values["data_coverage_missing_reason"], "")
        empty = csv_values({"profile_id": PROFILE_ID, "error": "需要本地价格库"})
        self.assertIsNone(empty["momentum_z"])
        self.assertEqual(empty["momentum_z_missing_reason"], "")
        self.assertEqual(empty["profile_id"], PROFILE_ID)
        zero = csv_values({"fields": {"data_coverage": {"value": 0.0, "missing_reason": None}}})
        self.assertEqual(zero["data_coverage"], 0.0)


if __name__ == "__main__":
    unittest.main()
