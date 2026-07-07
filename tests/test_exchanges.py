"""Unit tests for the exchange connectors (no network, no LLM).

Covers candle/ticker parsing, the agent-prompt assembly in `_publish_latest` (via a
fake Client), connector construction guards, and the candle-book wiring asymmetry
between the two `run()` entrypoints (finding B1).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from arena.fees import FeeModel
from arena.models import TIMEFRAMES
from arena.price_book import CandleBook
from exchanges import AGENT_OUTPUT_TOPIC, TickerMessage
from exchanges import coinbase as cb
from exchanges import binance as bn
from exchanges.binance import BINANCE_INTERVAL_MAP, BinanceKafkaConnector, parse_binance_candle
from exchanges.coinbase import CoinbaseKafkaConnector, parse_coinbase_candle


def _ticker(product_id="BTC-USD", price="50000.00") -> TickerMessage:
    return TickerMessage(
        product_id=product_id, price=price, best_bid="49990.00", best_bid_size="1",
        best_ask="50010.00", best_ask_size="2", side="buy", last_size="0.1",
        open_24h="49000", high_24h="51000", low_24h="48000", volume_24h="15000",
        volume_30d="900000", trade_id=7, sequence=11, time="2024-01-01T00:00:00Z",
    )


class _FakeGateway:
    """A fake calfkit AgentGateway: records .send() calls into a shared sink."""

    def __init__(self, topic, sink):
        self._topic = topic
        self._sink = sink

    async def send(self, user_prompt, *, deps=None, **kwargs):
        self._sink.append({"user_prompt": user_prompt, "topic": self._topic, "deps": deps})


class _FakeClient:
    """Captures agent(topic=...).send(...) invocations instead of touching a broker."""

    def __init__(self):
        self.invocations = []

    def agent(self, name=None, *, topic=None, output_type=str):
        return _FakeGateway(topic, self.invocations)


# ── Candle / ticker parsing ──────────────────────────────────────


def test_parse_coinbase_candle_column_order():
    # Coinbase: [time, low, high, open, close, volume]
    c = parse_coinbase_candle([1_700_000_000, 10.0, 30.0, 20.0, 25.0, 100.0])
    assert (c.open, c.high, c.low, c.close, c.volume) == (20.0, 30.0, 10.0, 25.0, 100.0)
    assert c.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)


def test_parse_binance_candle_column_order_and_ms_timestamp():
    # Binance: [openTime_ms, open, high, low, close, volume, ...]
    c = parse_binance_candle([1_700_000_000_000, 20.0, 30.0, 10.0, 25.0, 100.0, 999])
    assert (c.open, c.high, c.low, c.close, c.volume) == (20.0, 30.0, 10.0, 25.0, 100.0)
    assert c.time == datetime.fromtimestamp(1_700_000_000, tz=timezone.utc)  # ms → s


def test_parse_binance_ticker_maps_websocket_fields():
    conn = BinanceKafkaConnector(_FakeClient(), "t", ["BTCUSDT"], FeeModel(0))
    msg = conn._parse_binance_ticker(
        {"s": "BTCUSDT", "c": "50000", "b": "49990", "B": "1.0", "a": "50010",
         "A": "2.0", "Q": "0.5", "o": "49000", "h": "51000", "l": "48000",
         "v": "15000", "n": 42, "E": 1_700_000_000_000}
    )
    assert msg is not None
    assert msg.product_id == "BTCUSDT"
    assert msg.best_bid == "49990" and msg.best_ask == "50010"
    assert msg.price == "50000"


def test_parse_binance_ticker_missing_field_returns_none():
    conn = BinanceKafkaConnector(_FakeClient(), "t", ["BTCUSDT"], FeeModel(0))
    assert conn._parse_binance_ticker({"s": "BTCUSDT"}) is None  # missing required keys


def test_binance_interval_map_covers_every_timeframe_granularity():
    """Every TIMEFRAME granularity must map to a Binance interval, else poll_rest
    silently falls back to '1m' and fetches the wrong candle resolution."""
    for tf in TIMEFRAMES:
        assert tf.granularity in BINANCE_INTERVAL_MAP, tf.label


# ── Construction guards ──────────────────────────────────────────


def test_coinbase_connector_requires_products():
    with pytest.raises(ValueError):
        CoinbaseKafkaConnector(_FakeClient(), "t", [], FeeModel(0))


def test_binance_connector_requires_symbols():
    with pytest.raises(ValueError):
        BinanceKafkaConnector(_FakeClient(), "t", [], FeeModel(0))


def test_fee_disclosure_is_precomputed():
    fee = FeeModel(taker_bps=60)
    conn = CoinbaseKafkaConnector(_FakeClient(), "t", ["BTC-USD"], fee)
    assert conn._fee_disclosure == fee.disclosure_prompt()


# ── _publish_latest prompt assembly ──────────────────────────────


async def test_publish_latest_includes_fee_and_essential_ticker_fields():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "agent_router.input", ["BTC-USD"], FeeModel(60))
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()

    assert len(client.invocations) == 1
    inv = client.invocations[0]
    prompt = inv["user_prompt"]
    assert inv["topic"] == "agent_router.input"
    assert "invoked_at" in inv["deps"]
    assert "60 bps" in prompt  # fee disclosure injected
    # Essential fields kept; verbose fields excluded from the batch JSON.
    assert '"product_id"' in prompt and '"best_bid"' in prompt and '"price"' in prompt
    assert '"volume_24h"' not in prompt
    assert '"best_bid_size"' not in prompt
    assert '"time"' not in prompt


async def test_publish_latest_noop_when_no_tickers():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "t", ["BTC-USD"], FeeModel(0))
    await conn._publish_latest()
    assert client.invocations == []


async def test_publish_latest_appends_candle_history_when_available():
    client = _FakeClient()
    candles = CandleBook()
    candles.update_from_api("BTC-USD", 60, [[1_700_000_000, 1, 2, 1, 2, 5]])
    conn = CoinbaseKafkaConnector(
        client, "t", ["BTC-USD"], FeeModel(0), candle_book=candles
    )
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()
    assert "Price History (OHLCV" in client.invocations[0]["user_prompt"]


async def test_publish_latest_omits_candle_history_when_absent():
    client = _FakeClient()
    conn = CoinbaseKafkaConnector(client, "t", ["BTC-USD"], FeeModel(0))  # no candle book
    conn._latest = {"BTC-USD": _ticker()}

    await conn._publish_latest()
    assert "Price History" not in client.invocations[0]["user_prompt"]


# ── run() candle-book wiring (finding B1) ────────────────────────


def _capturing(captured):
    """A stand-in connector class that records its construction kwargs."""

    class _Conn:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def stop(self):  # referenced by run()'s signal-handler registration
            pass

        async def start(self):
            return None

    return _Conn


def _stub_run_env(monkeypatch, module):
    """Stub broker connect, env, and signal-handler registration for a run() call.

    Returns (connector_kwargs, connect_kwargs) captured during the run() call."""
    captured = {}
    connect_kwargs = {}

    class _Client:
        @staticmethod
        def connect(servers, **kwargs):
            connect_kwargs.update(kwargs)
            return object()

    monkeypatch.setenv("OPENAI_API_KEY", "dummy")  # config.json references it
    monkeypatch.setattr(module, "Client", _Client)
    # run() registers SIGINT/SIGTERM handlers on the running loop; neutralize them.
    monkeypatch.setattr(
        asyncio.get_running_loop(), "add_signal_handler", lambda *a, **k: None
    )
    return captured, connect_kwargs


async def test_binance_run_wires_a_candle_book(monkeypatch):
    captured, connect_kwargs = _stub_run_env(monkeypatch, bn)
    monkeypatch.setattr(bn, "BinanceKafkaConnector", _capturing(captured))
    args = SimpleNamespace(
        config="config.json", symbols=["BTCUSDT"], min_interval=60.0, bootstrap_servers="x"
    )
    await bn.run(args)
    # The connector must bind the shared inbox so the viewer can observe agent events.
    assert connect_kwargs.get("inbox_topic") == AGENT_OUTPUT_TOPIC
    assert captured.get("candle_book") is not None


@pytest.mark.xfail(
    reason="B1: Coinbase run() never passes a candle_book, so Coinbase agents get no OHLCV",
    strict=True,
)
async def test_coinbase_run_wires_a_candle_book(monkeypatch):
    captured, connect_kwargs = _stub_run_env(monkeypatch, cb)
    monkeypatch.setattr(cb, "CoinbaseKafkaConnector", _capturing(captured))
    args = SimpleNamespace(
        config="config.json", products=["BTC-USD"], min_interval=60.0, bootstrap_servers="x"
    )
    await cb.run(args)
    assert connect_kwargs.get("inbox_topic") == AGENT_OUTPUT_TOPIC
    assert captured.get("candle_book") is not None
