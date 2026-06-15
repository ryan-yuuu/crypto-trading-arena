"""Core data models for the trading arena."""

from __future__ import annotations

import typing
from dataclasses import dataclass, field
from datetime import datetime

# ── Configuration ────────────────────────────────────────────────

INITIAL_CASH = 100_000.0

# ── Data model ───────────────────────────────────────────────────


@dataclass
class TradeResult:
    success: bool
    message: str


@dataclass
class AgentAccount:
    cash: float = INITIAL_CASH
    positions: dict[str, float] = field(default_factory=dict)
    cost_basis: dict[str, float] = field(default_factory=dict)
    # Weighted-average entry timestamp (Unix epoch) per position
    avg_entry_ts: dict[str, float] = field(default_factory=dict)
    trade_count: int = 0
    # Cumulative taker fees paid across all fills (buys + sells).
    total_fees_paid: float = 0.0
    # Realized P&L from closed quantity, net of both entry and exit fees.
    # Buy fees enter via the capitalized cost basis; sell fees are deducted
    # from proceeds — so this figure already reflects the full round-trip cost.
    realized_pnl: float = 0.0

    def portfolio_value(self, price_book: PriceBook) -> float:
        """Total value: cash + mark-to-market of all positions using live prices."""
        positions_value = 0.0
        for pid, qty in self.positions.items():
            entry = price_book.get(pid)
            if entry is not None:
                positions_value += qty * float(entry["price"])
        return self.cash + positions_value

    def avg_cost_per_unit(self, product_id: str) -> float:
        """Average cost per unit for a position."""
        qty = self.positions.get(product_id, 0)
        if qty == 0:
            return 0.0
        return self.cost_basis.get(product_id, 0.0) / qty


# ── Trade log ────────────────────────────────────────────────────


class TradeLogEntry(typing.NamedTuple):
    """One row of the in-memory trade log feeding the dashboard's recent-trades panel."""

    timestamp: str
    agent_id: str
    action: str
    product_id: str
    quantity: float
    price: float
    fee: float
    latency: float | None


# ── Trade recorder protocol ──────────────────────────────────────


class TradeRecorder(typing.Protocol):
    def record_trade(
        self,
        *,
        agent_id: str,
        action: str,
        product_id: str,
        quantity: float,
        price: float,
        fee: float,
        cash_after: float,
        latency: float | None,
    ) -> None: ...


# ── Candle / Timeframe ───────────────────────────────────────────


@dataclass
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Timeframe:
    """Defines a candle granularity and the time window it covers."""

    granularity: int  # seconds (API param: 60, 300, 900)
    start_minutes_ago: int  # beginning of window (farther from now)
    end_minutes_ago: int  # end of window (closer to now)
    label: str  # human-readable label for the agent prompt


TIMEFRAMES = [
    Timeframe(900, 180, 90, "15-min candles (3h ago -> 90min ago)"),
    Timeframe(300, 90, 20, "5-min candles (90min ago -> 20min ago)"),
    Timeframe(60, 20, 0, "1-min candles (last 20 minutes)"),
]


# TYPE_CHECKING-only import to avoid circular dependency at runtime.
# AgentAccount.portfolio_value uses PriceBook via the `from __future__
# import annotations` string-annotation trick, so no runtime import needed.
if typing.TYPE_CHECKING:
    from arena.price_book import PriceBook
