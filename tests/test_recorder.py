"""Unit tests for arena.recorder.DataRecorder (CSV trade + snapshot recording)."""

from __future__ import annotations

import asyncio
import csv
import glob

import pytest

from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.price_book import PriceBook
from arena.recorder import DataRecorder, SnapshotRow, TradeRow


def _rows(data_dir, prefix):
    path = glob.glob(str(data_dir / f"{prefix}_*.csv"))[0]
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


# ── File setup ───────────────────────────────────────────────────


def test_creates_files_with_headers(tmp_path):
    DataRecorder(data_dir=str(tmp_path))
    trades = glob.glob(str(tmp_path / "trades_*.csv"))
    snaps = glob.glob(str(tmp_path / "snapshots_*.csv"))
    assert len(trades) == 1 and len(snaps) == 1
    with open(trades[0]) as fh:
        assert fh.readline().strip().split(",") == list(TradeRow.model_fields)
    with open(snaps[0]) as fh:
        assert fh.readline().strip().split(",") == list(SnapshotRow.model_fields)


# ── record_trade ─────────────────────────────────────────────────


def test_record_trade_writes_row_with_derived_total_value(tmp_path):
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.record_trade(
        agent_id="a", action="buy", product_id="BTC-USD", quantity=1.5,
        price=50_000.0, fee=45.0, cash_after=24_955.0, latency=2.0,
    )
    row = _rows(tmp_path, "trades")[0]
    assert row["agent_id"] == "a"
    assert float(row["total_value"]) == pytest.approx(75_000.0)  # price * quantity
    assert float(row["fee"]) == pytest.approx(45.0)
    assert float(row["cash_after"]) == pytest.approx(24_955.0)
    assert float(row["latency"]) == pytest.approx(2.0)


def test_record_trade_latency_none_serialized_blank(tmp_path):
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.record_trade(
        agent_id="a", action="sell", product_id="BTC-USD", quantity=1.0,
        price=50_000.0, fee=0.0, cash_after=1.0, latency=None,
    )
    assert _rows(tmp_path, "trades")[0]["latency"] == ""


# ── take_snapshot ────────────────────────────────────────────────


def test_snapshot_no_accounts_is_noop(tmp_path):
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.take_snapshot(AccountStore(PriceBook(), FeeModel(0)))
    assert _rows(tmp_path, "snapshots") == []


def test_snapshot_cash_only_account_one_blank_position_row(tmp_path, make_store):
    store = make_store(taker_bps=0)
    store.get_or_create("a")  # cash only, no positions
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.take_snapshot(store)

    rows = _rows(tmp_path, "snapshots")
    assert len(rows) == 1
    assert rows[0]["product_id"] == ""
    assert float(rows[0]["quantity"]) == 0.0
    assert float(rows[0]["portfolio_value"]) == pytest.approx(100_000.0)


def test_snapshot_with_priced_position(tmp_path, make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")  # cost basis 50010 @ ask
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.take_snapshot(store)

    row = next(r for r in _rows(tmp_path, "snapshots") if r["product_id"] == "BTC-USD")
    assert float(row["market_price"]) == pytest.approx(50_000.0)  # last price
    assert float(row["market_value"]) == pytest.approx(50_000.0)
    # unrealized = market_value - cost_basis = 50000 - 50010 = -10
    assert float(row["unrealized_pnl"]) == pytest.approx(-10.0)


@pytest.mark.xfail(
    reason="B7: unpriced position records unrealized_pnl=-cost_basis while "
    "portfolio_value skips it — the row is internally inconsistent",
    strict=True,
)
def test_snapshot_unpriced_position_pnl_is_consistent(tmp_path):
    store = AccountStore(PriceBook(), FeeModel(0))  # empty book → no quote for BTC
    acct = store.get_or_create("a")
    acct.positions["BTC-USD"] = 1.0
    acct.cost_basis["BTC-USD"] = 60_000.0
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.take_snapshot(store)

    row = next(r for r in _rows(tmp_path, "snapshots") if r["product_id"] == "BTC-USD")
    # If the position is excluded from portfolio_value, its unrealized_pnl must not be
    # booked as a -60000 loss in the same row.
    pv = float(row["portfolio_value"])
    unrealized = float(row["unrealized_pnl"])
    assert pv + unrealized == pytest.approx(pv), "unrealized_pnl contradicts portfolio_value"


# ── snapshot loop lifecycle ──────────────────────────────────────


async def test_close_writes_final_snapshot_on_cancel(tmp_path, make_store):
    """A long interval guarantees the periodic tick never fires, so the only row
    must come from the final-snapshot-on-cancel path in close()."""
    store = make_store(taker_bps=0)
    store.get_or_create("a")
    rec = DataRecorder(data_dir=str(tmp_path))
    rec.start_snapshot_loop(store, interval=100.0)
    await asyncio.sleep(0)  # let the loop task start and block on sleep
    await rec.close()

    assert len(_rows(tmp_path, "snapshots")) == 1
