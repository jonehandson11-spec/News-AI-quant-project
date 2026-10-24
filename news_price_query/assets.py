"""Explicit asset registry: the first HK cohort, benchmark indexes, and the
oil research assets in data/research_assets.csv (stage, market, currency...)."""

import csv
from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata


RESEARCH_CSV = Path(__file__).resolve().parent / "data" / "research_assets.csv"


@dataclass(frozen=True)
class Asset:
    ticker: str
    futu_code: str
    role: str
    aliases: tuple[str, ...]
    market: str = "XHKG"
    currency: str = "HKD"
    asset_type: str = "equity"
    stage: str | None = None
    stage_en: str | None = None
    name_zh: str | None = None
    name_en: str | None = None
    data_ticker: str | None = None
    exchange: str | None = None
    exclude_terms: tuple[str, ...] = ()
    alt_tickers: tuple[str, ...] = ()
    data_status: str | None = None
    notes: str | None = None

    @property
    def queryable(self) -> bool:
        return self.asset_type in ("equity", "index") and self.data_status != "no_free_source"


ASSETS = (
    Asset("1211.HK", "HK.01211", "equity", ("比亚迪", "比亚迪股份", "BYD")),
    Asset("0700.HK", "HK.00700", "equity", ("腾讯", "腾讯控股", "Tencent")),
    Asset("9988.HK", "HK.09988", "equity", ("阿里巴巴", "阿里巴巴集团", "Alibaba")),
    Asset("0981.HK", "HK.00981", "equity", ("中芯国际", "SMIC")),
    Asset("0005.HK", "HK.00005", "equity", ("汇丰", "汇丰控股", "HSBC")),
    Asset("2800.HK", "HK.02800", "benchmark", ("盈富基金", "Tracker Fund of Hong Kong")),
    Asset("3033.HK", "HK.03033", "benchmark", ("南方恒生科技ETF", "恒生科技ETF")),
)


# Price indexes used only as benchmarks (not listed securities).
INDEXES = (
    Asset("^HSI", "HK.800000", "benchmark", ("恒生指数", "恒指", "Hang Seng Index", "HSI")),
)


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(";") if part.strip())


def _futu_code(ticker: str) -> str:
    match = re.fullmatch(r"(\d{1,5})\.HK", ticker)
    return f"HK.{int(match.group(1)):05d}" if match else ""


def load_research_assets(path: Path = RESEARCH_CSV) -> tuple[Asset, ...]:
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    output = []
    for row in rows:
        role = "benchmark" if row["role"] == "benchmark" else (
            "futures" if row["asset_type"] == "futures" else "equity")
        names = tuple(dict.fromkeys(n for n in (row["name_zh"], row["name_en"]) if n))
        output.append(Asset(
            ticker=row["ticker"], futu_code=_futu_code(row["ticker"]), role=role,
            aliases=tuple(dict.fromkeys((*names, *_split(row["aliases"])))),
            market=row["market"], currency=row["currency"], asset_type=row["asset_type"],
            stage=row["stage_zh"] or None, stage_en=row["stage_en"] or None,
            name_zh=row["name_zh"] or None, name_en=row["name_en"] or None,
            data_ticker=row["data_ticker"] or None, exchange=row["exchange"] or None,
            exclude_terms=_split(row["exclude_terms"]), alt_tickers=_split(row["alt_tickers"]),
            data_status=row["data_status"] or None, notes=row["notes"] or None,
        ))
    return tuple(output)


RESEARCH_ASSETS = load_research_assets()


def _key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


_LOOKUP: dict = {}
for _asset in (*RESEARCH_ASSETS, *INDEXES, *ASSETS):  # later entries win: the original 7 keep their names
    for _alias in (_asset.ticker, _asset.futu_code, *_asset.aliases, *_asset.alt_tickers):
        if _alias:
            _LOOKUP[_key(_alias)] = _asset
# A ticker always resolves to its own research entry, even if another asset uses it as an alias.
for _asset in RESEARCH_ASSETS:
    _LOOKUP[_key(_asset.ticker)] = _asset


def market_of_ticker(ticker: str) -> tuple[str, str]:
    """(market MIC, currency) from the registry, else from the ticker suffix."""
    upper = ticker.upper()
    asset = _LOOKUP.get(_key(ticker))
    # Alternative listings (e.g. SHEL.L for SHEL) resolve to the primary asset in
    # name lookups but trade elsewhere, so only an exact ticker match counts here.
    if asset is not None and upper in {asset.ticker.upper(), (asset.data_ticker or "").upper()}:
        if asset.asset_type == "futures":
            raise NotImplementedError(f"{ticker}: futures are registered but not priced yet (next block)")
        return asset.market, asset.currency
    if upper.endswith(".HK") or upper == "^HSI":
        return "XHKG", "HKD"
    if upper.endswith(".TO"):
        return "XTSE", "CAD"
    if upper.endswith(".SR"):
        return "XSAU", "SAR"
    if upper.endswith("=F"):
        raise NotImplementedError(f"{ticker}: futures are registered but not priced yet")
    if re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z]{1,2})?", upper):  # Yahoo US style, e.g. XOM, BRK-B
        return "XNYS", "USD"
    raise NotImplementedError(f"{ticker}: unknown market")


def resolve_asset(name: str) -> Asset:
    """Resolve an explicit name or code; never infer a stock from a sector."""
    key = _key(name)
    asset = _LOOKUP.get(key)
    if asset is not None:
        return asset
    code = re.fullmatch(r"(?:([0-9]{1,5})\.hk|hk\.([0-9]{1,5}))", key)
    if code is not None:
        digits = code.group(1) or code.group(2)
        number = int(digits)
        if number > 0:
            ticker = f"{str(number).zfill(4)}.HK"
            futu_code = f"HK.{str(number).zfill(5)}"
            return Asset(ticker, futu_code, "equity", (ticker, futu_code))
    raise ValueError(f"Unknown asset: {name}. Use a registered name or HK ticker.")
