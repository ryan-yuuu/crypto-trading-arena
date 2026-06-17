"""Unit tests for arena.price_book — PriceBook and CandleBook."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arena.price_book import CandleBook, PriceBook, _default_parse_row


# ── PriceBook ────────────────────────────────────────────────────


def test_update_fills_optional_field_defaults():
    pb = PriceBook()
    pb.update(
        {"product_id": "BTC-USD", "price": "50000", "best_bid": "49990", "best_ask": "50010"}
    )
    entry = pb.get("BTC-USD")
    assert entry["price"] == "50000"
    assert entry["best_bid_size"] == "0"  # optional → default
    assert entry["best_ask_size"] == "0"
    assert entry["side"] == ""
    assert entry["volume_24h"] == "0"
    assert entry["time"] == ""


def test_get_unknown_product_returns_none():
    assert PriceBook().get("NOPE-USD") is None


def test_snapshot_is_an_independent_copy(price_book):
    snap = price_book.snapshot()
    snap.pop("BTC-USD")  # mutate the snapshot
    assert price_book.get("BTC-USD") is not None  # book unaffected


def test_update_overwrites_with_latest_quote(price_book):
    price_book.update(
        {"product_id": "BTC-USD", "price": "60000", "best_bid": "59990", "best_ask": "60010"}
    )
    assert price_book.get("BTC-USD")["price"] == "60000"


@pytest.mark.parametrize("missing", ["product_id", "price", "best_bid", "best_ask"])
def test_update_requires_core_fields(missing):
    data = {"product_id": "BTC-USD", "price": "1", "best_bid": "1", "best_ask": "1"}
    data.pop(missing)
    with pytest.raises(KeyError):
        PriceBook().update(data)


# ── CandleBook: parsing & ordering ───────────────────────────────


def test_default_parse_row_maps_coinbase_column_order():
    # Coinbase row: [time, low, high, open, close, volume]
    candle = _default_parse_row([1_700_000_000, 10.0, 30.0, 20.0, 25.0, 100.0])
    assert candle.open == 20.0
    assert candle.high == 30.0
    assert candle.low == 10.0
    assert candle.close == 25.0
    assert candle.volume == 100.0
    assert candle.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_update_from_api_sorts_candles_by_time():
    book = CandleBook()
    base = 1_700_000_000
    # Rows arrive newest-first (as Coinbase returns them); expect oldest-first output.
    rows = [
        [base + 120, 1, 2, 1, 2, 1],
        [base + 0, 1, 2, 1, 2, 1],
        [base + 60, 1, 2, 1, 2, 1],
    ]
    book.update_from_api("BTC-USD", 60, rows)
    out = book.format_prompt(["BTC-USD"])
    t0 = datetime.fromtimestamp(base, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t1 = datetime.fromtimestamp(base + 60, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    t2 = datetime.fromtimestamp(base + 120, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out.index(t0) < out.index(t1) < out.index(t2)


def test_update_from_api_replaces_previous_candles():
    book = CandleBook()
    book.update_from_api("BTC-USD", 60, [[1, 1, 1, 1, 1, 1], [2, 1, 1, 1, 1, 1]])
    book.update_from_api("BTC-USD", 60, [[3, 9, 9, 9, 9, 9]])  # replace, not append
    out = book.format_prompt(["BTC-USD"])
    assert out.count("BTC-USD,") == 1  # only the single replacement row


def test_custom_parse_row_is_used():
    sentinel = []

    def parser(row):
        sentinel.append(row)
        return _default_parse_row(row)

    book = CandleBook(parse_row=parser)
    book.update_from_api("BTC-USD", 60, [[1_700_000_000, 1, 2, 1, 2, 1]])
    assert sentinel  # custom parser was invoked


# ── CandleBook: prompt structure & has_data ──────────────────────


def test_format_prompt_includes_every_timeframe_and_header():
    book = CandleBook()
    book.update_from_api("BTC-USD", 60, [[1_700_000_000, 1, 2, 1, 2, 1]])
    out = book.format_prompt(["BTC-USD"])
    # All three timeframe section labels present...
    assert "15-min candles" in out
    assert "5-min candles" in out
    assert "1-min candles" in out
    # ...and the CSV header appears once per section.
    assert out.count("product,time,open,high,low,close,volume") == 3
    # 1-min section has the data row; coarser sections have none.
    assert "BTC-USD," in out


def test_has_data_and_empty_prompt():
    book = CandleBook()
    assert book.has_data() is False
    out = book.format_prompt(["BTC-USD"])
    assert "15-min candles" in out  # labels still render
    assert "BTC-USD," not in out  # but no data rows
    book.update_from_api("BTC-USD", 60, [[1, 1, 1, 1, 1, 1]])
    assert book.has_data() is True
