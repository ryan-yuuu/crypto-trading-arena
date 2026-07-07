"""Broker integration tests against a real Kafka-API broker (Redpanda).

These exercise the production pub/sub wiring that the in-memory tests can't:
serialization over the wire, real topic routing, and consumer-group fan-out.

They are opt-in: marked ``@pytest.mark.broker`` and skipped unless ``--run-broker``
is passed. When enabled, a Redpanda container is started via testcontainers, so a
running Docker daemon is the only prerequisite. No LLM is involved.

Each test tags its payload with a unique sentinel and reads with
``auto_offset_reset="earliest"``, so a session-shared topic cannot drop the message
(timing) or leak another test's message (cross-talk).
"""

from __future__ import annotations

import asyncio

import pytest

from calfkit import Client
from calfkit.models.envelope import Envelope
from arena.fees import FeeModel
from arena.price_book import PriceBook
from exchanges import AGENT_INPUT_TOPIC, PRICE_TOPIC, TickerMessage
from exchanges.coinbase import CoinbaseKafkaConnector

pytestmark = pytest.mark.broker

AGENT_TOPIC = AGENT_INPUT_TOPIC
WAIT_TIMEOUT = 45.0


def _ticker(product_id: str, price: str = "50000.00") -> TickerMessage:
    return TickerMessage(
        product_id=product_id, price=price, best_bid="49990.00", best_bid_size="1",
        best_ask="50010.00", best_ask_size="2", side="buy", last_size="0.1",
        open_24h="49000", high_24h="51000", low_24h="48000", volume_24h="15000",
        volume_30d="900000", trade_id=7, sequence=11, time="2024-01-01T00:00:00Z",
    )


@pytest.fixture(scope="session")
def redpanda_bootstrap():
    """Start a Redpanda broker for the test session; yield its bootstrap address."""
    from testcontainers.kafka import RedpandaContainer

    container = RedpandaContainer()
    container.start()
    try:
        yield container.get_bootstrap_server()
    finally:
        container.stop()


@pytest.fixture
async def client(redpanda_bootstrap):
    """A connected calfkit Client, closed after the test."""
    c = Client.connect(redpanda_bootstrap)
    try:
        yield c
    finally:
        await c.aclose()


# ── Tests ────────────────────────────────────────────────────────


async def test_price_topic_round_trip_hydrates_price_book(client):
    """The exact tools_and_dashboard handler: a TickerMessage on PRICE_TOPIC updates
    a PriceBook after a real serialize → Kafka → deserialize round trip."""
    pb = PriceBook()
    got = asyncio.Event()

    @client.broker.subscriber(
        PRICE_TOPIC, group_id="it-hydrate", auto_offset_reset="earliest"
    )
    async def handle(ticker: TickerMessage) -> None:
        if ticker.product_id == "SENTINEL-HYDRATE":
            pb.update(ticker.model_dump())
            got.set()

    await client.broker.start()
    await client.broker.publish(_ticker("SENTINEL-HYDRATE", price="64000.00"), PRICE_TOPIC)
    await asyncio.wait_for(got.wait(), timeout=WAIT_TIMEOUT)

    entry = pb.get("SENTINEL-HYDRATE")
    assert entry is not None
    assert entry["best_ask"] == "50010.00"
    assert entry["price"] == "64000.00"


async def test_consumer_group_fanout(client):
    """Two distinct consumer groups each receive every message — the mechanism the
    arena relies on so every agent sees every market tick."""
    group_a = asyncio.Event()
    group_b = asyncio.Event()

    @client.broker.subscriber(PRICE_TOPIC, group_id="it-fanout-a", auto_offset_reset="earliest")
    async def handle_a(ticker: TickerMessage) -> None:
        if ticker.product_id == "SENTINEL-FANOUT":
            group_a.set()

    @client.broker.subscriber(PRICE_TOPIC, group_id="it-fanout-b", auto_offset_reset="earliest")
    async def handle_b(ticker: TickerMessage) -> None:
        if ticker.product_id == "SENTINEL-FANOUT":
            group_b.set()

    await client.broker.start()
    await client.broker.publish(_ticker("SENTINEL-FANOUT"), PRICE_TOPIC)

    await asyncio.wait_for(asyncio.gather(group_a.wait(), group_b.wait()), timeout=WAIT_TIMEOUT)


async def test_connector_publishes_invoke_to_agent_topic(client):
    """A real CoinbaseKafkaConnector's _publish_latest lands a well-formed invoke
    envelope — carrying the ticker prompt — on agent_router.input over real Kafka."""
    received: list[Envelope] = []
    got = asyncio.Event()

    @client.broker.subscriber(AGENT_TOPIC, group_id="it-invoke", auto_offset_reset="earliest")
    async def handle(envelope: Envelope) -> None:
        if "SENTINEL-INVOKE" in envelope.model_dump_json():
            received.append(envelope)
            got.set()

    await client.broker.start()

    connector = CoinbaseKafkaConnector(
        client, AGENT_TOPIC, ["SENTINEL-INVOKE"], FeeModel(taker_bps=60)
    )
    connector._latest = {"SENTINEL-INVOKE": _ticker("SENTINEL-INVOKE")}
    await connector._publish_latest()

    await asyncio.wait_for(got.wait(), timeout=WAIT_TIMEOUT)
    # The fee disclosure and ticker both survived the round trip.
    payload = received[0].model_dump_json()
    assert "SENTINEL-INVOKE" in payload
    assert "60 bps" in payload
