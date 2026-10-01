"""Small, explicit HK asset registry for the first event-study cohort."""

from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class Asset:
    ticker: str
    futu_code: str
    role: str
    aliases: tuple[str, ...]


ASSETS = (
    Asset("1211.HK", "HK.01211", "equity", ("比亚迪", "比亚迪股份", "BYD")),
    Asset("0700.HK", "HK.00700", "equity", ("腾讯", "腾讯控股", "Tencent")),
    Asset("9988.HK", "HK.09988", "equity", ("阿里巴巴", "阿里巴巴集团", "Alibaba")),
    Asset("0981.HK", "HK.00981", "equity", ("中芯国际", "SMIC")),
    Asset("0005.HK", "HK.00005", "equity", ("汇丰", "汇丰控股", "HSBC")),
    Asset("2800.HK", "HK.02800", "benchmark", ("盈富基金", "Tracker Fund of Hong Kong")),
    Asset("3033.HK", "HK.03033", "benchmark", ("南方恒生科技ETF", "恒生科技ETF")),
)


def _key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


_LOOKUP = {
    _key(alias): asset
    for asset in ASSETS
    for alias in (asset.ticker, asset.futu_code, *asset.aliases)
}


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
