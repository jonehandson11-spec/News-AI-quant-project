"""Historical price bars from a local CSV or optional Futu OpenD."""

import csv
from dataclasses import dataclass
from datetime import datetime, time
from math import isfinite
from pathlib import Path
from zoneinfo import ZoneInfo


HK = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class PriceBar:
    ticker: str
    interval: str
    timestamp: datetime
    close: float
    source: str
    basis: str = "raw"


def _parse_timestamp(value: str, interval: str) -> datetime:
    if interval == "day" and len(value) == 10:
        return datetime.combine(datetime.fromisoformat(value).date(), time(16, 0), HK)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Price timestamp needs timezone: {value}")
    return parsed.astimezone(HK)


class CsvPriceProvider:
    """Read `ticker,interval,timestamp,close[,basis]` CSV bars."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.source = f"csv:{self.path.name}"
        self._bars: list[PriceBar] = []
        seen: set[tuple[str, str, datetime]] = set()
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            required = {"ticker", "interval", "timestamp", "close"}
            if not required.issubset(reader.fieldnames or ()):
                raise ValueError(f"Price CSV needs columns: {', '.join(sorted(required))}")
            for row in reader:
                interval = row["interval"].strip().lower()
                if interval not in {"minute", "day"}:
                    raise ValueError(f"Unsupported price interval: {interval}")
                close = float(row["close"])
                if not isfinite(close) or close <= 0:
                    raise ValueError("Close price must be positive and finite")
                bar = PriceBar(
                    ticker=row["ticker"].strip().upper(),
                    interval=interval,
                    timestamp=_parse_timestamp(row["timestamp"].strip(), interval),
                    close=close,
                    source=self.source,
                    basis=(row.get("basis") or "raw").strip(),
                )
                key = (bar.ticker, bar.interval, bar.timestamp)
                if key in seen:
                    raise ValueError(f"Duplicate price bar: {bar.ticker} {bar.interval} {bar.timestamp.isoformat()}")
                seen.add(key)
                self._bars.append(bar)

    def bars(self, ticker: str, start: datetime, end: datetime, interval: str) -> list[PriceBar]:
        selected = [
            bar for bar in self._bars
            if bar.ticker == ticker.upper()
            and bar.interval == interval
            and start <= bar.timestamp <= end
        ]
        return sorted(selected, key=lambda item: item.timestamp)


class FutuPriceProvider:
    """Optional Futu OpenD adapter; requires `futu-api` and working quote access."""

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, context=None):
        try:
            import futu
        except ImportError as exc:
            raise RuntimeError("Futu provider requires the futu-api package and OpenD") from exc
        self._futu = futu
        self._owned = context is None
        self._context = context or futu.OpenQuoteContext(host=host, port=port)

    def close(self):
        if self._owned:
            self._context.close()

    def bars(self, ticker: str, start: datetime, end: datetime, interval: str) -> list[PriceBar]:
        from .assets import resolve_asset

        asset = resolve_asset(ticker)
        ktype = self._futu.KLType.K_1M if interval == "minute" else self._futu.KLType.K_DAY
        start_date = start.astimezone(HK).date().isoformat()
        end_date = end.astimezone(HK).date().isoformat()
        page_key = None
        output: list[PriceBar] = []
        while True:
            ret, data, page_key = self._context.request_history_kline(
                asset.futu_code, start=start_date, end=end_date,
                ktype=ktype, autype=self._futu.AuType.NONE,
                max_count=1000, page_req_key=page_key,
            )
            if ret != self._futu.RET_OK:
                raise RuntimeError(f"Futu history request failed for {ticker}: {data}")
            for _, row in data.iterrows():
                timestamp = datetime.fromisoformat(str(row["time_key"]))
                if interval == "day":
                    timestamp = timestamp.replace(hour=16, minute=0, second=0)
                timestamp = timestamp.replace(tzinfo=HK)
                close = float(row["close"])
                if not isfinite(close) or close <= 0:
                    raise ValueError(f"Futu returned invalid close for {ticker}: {close}")
                if start <= timestamp <= end:
                    output.append(PriceBar(
                        ticker=asset.ticker, interval=interval, timestamp=timestamp,
                        close=close, source="futu", basis="raw",
                    ))
            if page_key is None:
                break
        return sorted(output, key=lambda item: item.timestamp)
