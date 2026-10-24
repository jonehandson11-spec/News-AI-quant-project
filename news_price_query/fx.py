"""Currency decomposition interface (not wired into results yet).

All returns are reported in the asset's local currency. To express a local
return in another currency later, combine it with the FX return over the same
dates: (1 + r_local) * (1 + r_fx) - 1, where r_fx is the change in the price of
one unit of local currency measured in the target currency.
"""

from typing import Protocol
from datetime import date


class FxProvider(Protocol):
    def rate(self, base: str, quote: str, day: date) -> float | None:
        """Units of `quote` per one `base` on `day` (a close/fixing), or None."""


def convert_return_pct(local_return_pct: float, fx_return_pct: float) -> float:
    return ((1 + local_return_pct / 100) * (1 + fx_return_pct / 100) - 1) * 100
