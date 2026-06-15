"""Trading fee model.

All simulated fills cross the spread (buys at best_ask, sells at best_bid),
so only the taker rate is applied. Quoted in basis points (bps): 1 bps = 0.01%.

The default of 60 bps matches Coinbase Advanced Trade's base-tier taker rate.
The rate is configured via config.json (`trading.fees.taker_bps`), read
independently by the tools-node entrypoint (which charges the fee) and the
price-feed connector (which advertises it to agents).

`taker_bps` is an integer: every documented venue preset (Coinbase 60, Binance
VIP 0 = 10, Kraken Pro = 40) is a whole number of basis points. Fractional-bps
rates (e.g. Binance's 7.5 bps BNB discount) are intentionally out of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

# Single canonical default, referenced by FeeModel and config.FeeConfig so the
# "60 bps" default lives in exactly one place.
DEFAULT_TAKER_BPS = 60
# Upper guard against fat-fingered config (1000 bps = 10%); a higher rate is
# almost certainly a misconfiguration rather than a real venue fee.
MAX_TAKER_BPS = 1000


class Fill(NamedTuple):
    """Outcome of applying fees to one fill.

    `cash` is the cash leaving the account on a buy (cash_out) or entering it on
    a sell (cash_in); `fee` is the taker fee charged on the fill.
    """

    cash: float
    fee: float


@dataclass(frozen=True)
class FeeModel:
    taker_bps: int = DEFAULT_TAKER_BPS

    def __post_init__(self) -> None:
        if not 0 <= self.taker_bps <= MAX_TAKER_BPS:
            raise ValueError(
                f"taker_bps must be between 0 and {MAX_TAKER_BPS}, got {self.taker_bps}"
            )

    @property
    def taker_rate(self) -> float:
        return self.taker_bps / 10_000.0

    def buy_cost(self, notional: float) -> Fill:
        """For a buy of size `notional` (price * qty), return the cash out and fee.

        The fee is capitalized into cost basis: cash_out = notional + fee.
        """
        fee = notional * self.taker_rate
        return Fill(notional + fee, fee)

    def sell_proceeds(self, notional: float) -> Fill:
        """For a sell of size `notional` (price * qty), return the cash in and fee.

        The fee is deducted from proceeds: cash_in = notional - fee.
        """
        fee = notional * self.taker_rate
        return Fill(notional - fee, fee)

    def disclosure_prompt(self) -> str:
        """Agent-facing description of the fee, injected into the ticker prompt."""
        if self.taker_bps == 0:
            return "Trading is fee-free in this deployment."
        return (
            f"A taker fee of {self.taker_bps} bps "
            f"({self.taker_bps / 100:.2f}%) is charged on every fill "
            "(both buys and sells). Factor this into your sizing and P&L targets — "
            f"a round-trip costs {2 * self.taker_bps} bps."
        )
