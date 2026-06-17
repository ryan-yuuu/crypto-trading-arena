"""Unit tests for arena.models — AgentAccount math and timeframe invariants."""

from __future__ import annotations

import pytest

from arena.models import (
    INITIAL_CASH,
    TIMEFRAMES,
    AgentAccount,
    TradeLogEntry,
)
from arena.price_book import PriceBook


# ── AgentAccount.portfolio_value ─────────────────────────────────


def test_portfolio_value_is_cash_when_no_positions(price_book):
    account = AgentAccount(cash=12_345.67)
    assert account.portfolio_value(price_book) == pytest.approx(12_345.67)


def test_portfolio_value_marks_positions_at_last_price_not_bid_or_ask(price_book):
    """Mark-to-market uses the 'price' (last trade) field, not best_bid/best_ask.

    This pins a deliberate modelling choice: unrealized value uses the last price
    even though fills cross the spread. BTC price=50000 (bid 49990 / ask 50010).
    """
    account = AgentAccount(cash=1_000.0, positions={"BTC-USD": 2.0})
    # 1000 cash + 2 * 50000 last price = 101000 (not 2*49990 or 2*50010).
    assert account.portfolio_value(price_book) == pytest.approx(101_000.0)


def test_portfolio_value_skips_positions_without_a_live_price(price_book):
    """A held product with no quote contributes nothing rather than crashing."""
    account = AgentAccount(cash=500.0, positions={"BTC-USD": 1.0, "DOGE-USD": 9999.0})
    # DOGE has no price entry → ignored. 500 + 1*50000.
    assert account.portfolio_value(price_book) == pytest.approx(50_500.0)


def test_portfolio_value_default_starting_balance():
    assert AgentAccount().cash == INITIAL_CASH
    assert AgentAccount().portfolio_value(PriceBook()) == pytest.approx(INITIAL_CASH)


# ── AgentAccount.avg_cost_per_unit ───────────────────────────────


def test_avg_cost_per_unit_zero_for_absent_position():
    assert AgentAccount().avg_cost_per_unit("BTC-USD") == 0.0


def test_avg_cost_per_unit_zero_when_quantity_is_zero():
    """A residual zero-qty entry must not divide by zero."""
    account = AgentAccount(positions={"BTC-USD": 0.0}, cost_basis={"BTC-USD": 100.0})
    assert account.avg_cost_per_unit("BTC-USD") == 0.0


def test_avg_cost_per_unit_divides_basis_by_quantity():
    account = AgentAccount(positions={"BTC-USD": 4.0}, cost_basis={"BTC-USD": 200.0})
    assert account.avg_cost_per_unit("BTC-USD") == pytest.approx(50.0)


# ── TradeLogEntry ────────────────────────────────────────────────


def test_trade_log_entry_field_order_matches_consumers():
    """Dashboard/recorder read these fields by name; lock the schema."""
    entry = TradeLogEntry("12:00:00", "agent", "buy", "BTC-USD", 1.5, 50000.0, 30.0, 2.5)
    assert entry.timestamp == "12:00:00"
    assert entry.agent_id == "agent"
    assert entry.action == "buy"
    assert entry.product_id == "BTC-USD"
    assert entry.quantity == 1.5
    assert entry.price == 50000.0
    assert entry.fee == 30.0
    assert entry.latency == 2.5


def test_trade_log_entry_latency_is_optional():
    entry = TradeLogEntry("12:00:00", "agent", "sell", "BTC-USD", 1.0, 50000.0, 30.0, None)
    assert entry.latency is None


# ── TIMEFRAMES invariants ────────────────────────────────────────


def test_timeframes_windows_are_descending_and_contiguous():
    """Each window runs from farther-ago to nearer-ago, and they tile 180min → now.

    poll_rest derives REST start/end from these, so a broken window would request
    inverted or gapped candle ranges.
    """
    assert len(TIMEFRAMES) == 3
    for tf in TIMEFRAMES:
        assert tf.start_minutes_ago > tf.end_minutes_ago, tf.label

    # Coarsest first, finest last; granularity strictly decreasing.
    granularities = [tf.granularity for tf in TIMEFRAMES]
    assert granularities == sorted(granularities, reverse=True)
    assert granularities == [900, 300, 60]

    # Contiguous coverage: each timeframe's end is the next one's start.
    for coarser, finer in zip(TIMEFRAMES, TIMEFRAMES[1:]):
        assert coarser.end_minutes_ago == finer.start_minutes_ago
    assert TIMEFRAMES[-1].end_minutes_ago == 0  # finest window ends at "now"
