"""Trading fee model.

All simulated fills cross the spread (buys at best_ask, sells at best_bid),
so only the taker rate is applied. Quoted in basis points (bps): 1 bps = 0.01%.
Default 60 bps matches Coinbase Advanced Trade's base-tier taker rate; override
per-deployment via config.json (`trading.fees.taker_bps`), which is the single
source of truth read by both the trading tools and the price-feed connector.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeeModel:
    taker_bps: int = 60

    def __post_init__(self) -> None:
        if self.taker_bps < 0:
            raise ValueError(f"taker_bps must be non-negative, got {self.taker_bps}")

    @property
    def taker_rate(self) -> float:
        return self.taker_bps / 10_000.0

    def buy_cost(self, notional: float) -> tuple[float, float]:
        """For a buy of size `notional` (price * qty), return (cash_out, fee).

        The fee is capitalized into cost basis: cash_out = notional + fee.
        """
        fee = notional * self.taker_rate
        return notional + fee, fee

    def sell_proceeds(self, notional: float) -> tuple[float, float]:
        """For a sell of size `notional` (price * qty), return (cash_in, fee).

        The fee is deducted from proceeds: cash_in = notional - fee.
        """
        fee = notional * self.taker_rate
        return notional - fee, fee
