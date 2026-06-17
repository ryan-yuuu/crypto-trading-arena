"""Unit tests for arena.account_store.AccountStore.

These build fresh AccountStore instances (via the `make_store` fixture) rather than
the arena.tools singleton, so account state never leaks between tests. Confirmed bugs
are encoded as strict xfails referencing docs/REVIEW_FINDINGS.md.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arena.models import INITIAL_CASH

BTC_ASK = 50010.00
BTC_BID = 49990.00
SOL_ASK = 100.10


# ── Happy-path execution & price selection ───────────────────────


def test_buy_executes_at_best_ask(make_store):
    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "BTC-USD", 1.0, "buy")
    assert result.success, result.message

    acct = store.get_or_create("a")
    assert acct.positions["BTC-USD"] == pytest.approx(1.0)
    assert acct.cash == pytest.approx(INITIAL_CASH - BTC_ASK)  # filled at ask, not last/bid
    assert acct.cost_basis["BTC-USD"] == pytest.approx(BTC_ASK)
    assert acct.trade_count == 1


def test_sell_executes_at_best_bid(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")
    cash_after_buy = store.get_or_create("a").cash

    result = store.execute_trade("a", "BTC-USD", 1.0, "sell")
    assert result.success, result.message
    acct = store.get_or_create("a")
    assert acct.cash == pytest.approx(cash_after_buy + BTC_BID)  # proceeds at bid, not last/ask


def test_action_and_product_id_are_normalized(make_store):
    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "  btc-usd ", 1.0, " BUY ")
    assert result.success, result.message
    assert "BTC-USD" in store.get_or_create("a").positions


# ── Validation / reject paths (no state mutation) ────────────────


def test_invalid_action_rejected(make_store):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", 1.0, "hodl")
    assert not result.success
    assert "Invalid action" in result.message
    assert store.get_or_create("a").trade_count == 0


def test_unknown_product_rejected_lists_available(make_store):
    store = make_store()
    result = store.execute_trade("a", "DOGE-USD", 1.0, "buy")
    assert not result.success
    assert "No live price" in result.message
    assert "BTC-USD" in result.message and "SOL-USD" in result.message  # available list


@pytest.mark.parametrize("qty", [0.0, -1.0, -0.5])
def test_non_positive_quantity_rejected(make_store, qty):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", qty, "buy")
    assert not result.success
    assert "positive" in result.message.lower()


@pytest.mark.parametrize("qty", [0.12, 1.23, 0.05])
def test_excess_precision_quantity_rejected(make_store, qty):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", qty, "buy")
    assert not result.success
    assert "decimal place" in result.message


def test_insufficient_cash_leaves_state_unchanged(make_store):
    store = make_store(taker_bps=0)
    acct = store.get_or_create("a")
    acct.cash = 100.0  # nowhere near 1 BTC @ 50010

    result = store.execute_trade("a", "BTC-USD", 1.0, "buy")
    assert not result.success
    assert "Insufficient cash" in result.message
    assert acct.cash == 100.0
    assert acct.positions == {}
    assert acct.cost_basis == {}
    assert acct.trade_count == 0
    assert store.trade_log == []


def test_sell_more_than_held_rejected(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")
    result = store.execute_trade("a", "BTC-USD", 2.0, "sell")
    assert not result.success
    assert "Insufficient holdings" in result.message
    assert store.get_or_create("a").positions["BTC-USD"] == pytest.approx(1.0)  # untouched


def test_sell_with_no_position_rejected(make_store):
    store = make_store()
    result = store.execute_trade("a", "BTC-USD", 1.0, "sell")
    assert not result.success
    assert "Insufficient holdings" in result.message


# ── Position lifecycle ───────────────────────────────────────────


def test_full_sell_clears_position_and_all_metadata(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")
    store.execute_trade("a", "BTC-USD", 1.0, "sell")

    acct = store.get_or_create("a")
    assert "BTC-USD" not in acct.positions
    assert "BTC-USD" not in acct.cost_basis
    assert "BTC-USD" not in acct.avg_entry_ts


def test_partial_sell_keeps_per_unit_cost_and_all_keys(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")
    acct = store.get_or_create("a")
    avg_before = acct.avg_cost_per_unit("BTC-USD")

    store.execute_trade("a", "BTC-USD", 0.4, "sell")

    assert acct.positions["BTC-USD"] == pytest.approx(0.6)
    assert acct.avg_cost_per_unit("BTC-USD") == pytest.approx(avg_before)
    # All three maps remain populated and in sync after a partial close.
    assert acct.cost_basis["BTC-USD"] == pytest.approx(avg_before * 0.6)
    assert "BTC-USD" in acct.avg_entry_ts


def test_account_maps_stay_in_sync_across_a_trade_sequence(make_store):
    """positions / cost_basis / avg_entry_ts must share one key set after every fill (R9)."""
    store = make_store(taker_bps=10)
    acct = store.get_or_create("a")
    sequence = [
        ("BTC-USD", 1.0, "buy"),
        ("SOL-USD", 5.0, "buy"),
        ("BTC-USD", 0.5, "buy"),
        ("BTC-USD", 0.5, "sell"),
        ("SOL-USD", 5.0, "sell"),  # fully closes SOL
        ("BTC-USD", 1.0, "sell"),  # fully closes BTC
    ]
    for product, qty, action in sequence:
        assert store.execute_trade("a", product, qty, action).success
        keys = set(acct.positions)
        assert set(acct.cost_basis) == keys
        assert set(acct.avg_entry_ts) == keys
        assert all(v >= 0 for v in acct.cost_basis.values())
    assert acct.positions == {}  # everything closed


def test_weighted_average_entry_timestamp(make_store, monkeypatch):
    """Two equal-size buys at different times blend to the midpoint timestamp."""
    import arena.account_store as acct_mod

    clock = {"t": 1000.0}

    class _FixedClock:
        @classmethod
        def now(cls):
            return datetime.fromtimestamp(clock["t"], tz=timezone.utc)

    monkeypatch.setattr(acct_mod, "datetime", _FixedClock)

    store = make_store(taker_bps=0)
    assert store.execute_trade("a", "SOL-USD", 1.0, "buy").success
    clock["t"] = 2000.0
    assert store.execute_trade("a", "SOL-USD", 1.0, "buy").success

    # Equal sizes at t=1000 and t=2000 → weighted-average entry of 1500.
    assert store.get_or_create("a").avg_entry_ts["SOL-USD"] == pytest.approx(1500.0)


# ── Trade log & recorder forwarding ──────────────────────────────


def test_trade_log_appends_one_entry_per_fill(make_store):
    store = make_store(taker_bps=10)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")
    store.execute_trade("a", "BTC-USD", 0.5, "sell")

    log = store.trade_log
    assert len(log) == 2
    assert log[0].action == "buy" and log[0].product_id == "BTC-USD"
    assert log[0].price == pytest.approx(BTC_ASK)
    assert log[1].action == "sell"
    assert log[1].price == pytest.approx(BTC_BID)
    assert log[0].fee > 0  # 10 bps charged


def test_latency_forwarded_to_log_and_recorder(make_store):
    store = make_store(taker_bps=0)
    captured = {}

    class _Recorder:
        def record_trade(self, **kw):
            captured.update(kw)

    store.attach_recorder(_Recorder())
    store.execute_trade("a", "BTC-USD", 1.0, "buy", latency=1.23)

    assert store.trade_log[0].latency == pytest.approx(1.23)
    assert captured["latency"] == pytest.approx(1.23)
    assert captured["agent_id"] == "a"
    assert captured["action"] == "buy"
    assert captured["cash_after"] == pytest.approx(INITIAL_CASH - BTC_ASK)


def test_rejected_trade_is_not_recorded(make_store):
    store = make_store(taker_bps=0)
    calls = []

    class _Recorder:
        def record_trade(self, **kw):
            calls.append(kw)

    store.attach_recorder(_Recorder())
    result = store.execute_trade("a", "BTC-USD", 1.0, "sell")  # nothing held → rejected
    assert not result.success
    assert calls == []
    assert store.trade_log == []


def test_get_or_create_is_idempotent_and_per_agent(make_store):
    store = make_store()
    a1 = store.get_or_create("a")
    a2 = store.get_or_create("a")
    b = store.get_or_create("b")
    assert a1 is a2
    assert a1 is not b


# ── Confirmed bugs (strict xfail — assert CORRECT behavior) ───────


@pytest.mark.xfail(
    reason="B2: qty in (0,1e-9] rounds to 0.0 and crashes on a fresh position",
    raises=ZeroDivisionError,
    strict=True,
)
def test_subrounding_quantity_rejected(make_store):
    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "BTC-USD", 1e-10, "buy")
    assert not result.success  # should be rejected, not crash or phantom-fill


@pytest.mark.xfail(
    reason="B5: NaN quantity bypasses all guards and poisons the account",
    strict=True,
)
def test_nan_quantity_rejected(make_store):
    import math

    store = make_store(taker_bps=0)
    result = store.execute_trade("a", "BTC-USD", float("nan"), "buy")
    assert not result.success
    acct = store.get_or_create("a")
    assert math.isfinite(acct.cash)
    assert acct.positions == {}


@pytest.mark.xfail(
    reason="B6: float drift in accumulated position blocks a legitimate full sell",
    strict=True,
)
def test_full_position_sell_after_fractional_buys(make_store):
    store = make_store(taker_bps=0)
    for qty in (0.1, 0.1, 0.7):
        assert store.execute_trade("a", "BTC-USD", qty, "buy").success
    result = store.execute_trade("a", "BTC-USD", 0.9, "sell")
    assert result.success, result.message
