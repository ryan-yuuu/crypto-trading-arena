"""Shared types and cross-process contracts for exchange connectors."""

import logging

from pydantic import BaseModel

PRICE_TOPIC = "market_data.prices"

# Agent-routing topics — the shared fan-out contract. Every agent subscribes to
# AGENT_INPUT_TOPIC under its own consumer group (so one publish broadcasts to all
# agents); agent turn events are observed on AGENT_OUTPUT_TOPIC. These names are an
# on-the-wire contract shared by independently-deployed processes and must stay
# byte-stable across the whole fleet.
AGENT_INPUT_TOPIC = "agent_router.input"
AGENT_OUTPUT_TOPIC = "agent_router.output"

# calfkit routes agent replies to the invoker's inbox and logs a per-reply "no pending
# handle" notice under this (calfkit-owned) logger when a fire-and-forget / observer client —
# one that holds no run handles — binds a shared inbox like AGENT_OUTPUT_TOPIC. See ADR-0002.
SHARED_INBOX_REPLY_LOGGER = "calfkit.client.hub"


def quiet_shared_inbox_reply_log(level: int = logging.ERROR) -> None:
    """Suppress the shared-inbox "no pending handle" reply notices by raising the calfkit hub
    logger to ``level``. Connectors use ERROR (genuine fault replies stay visible); the
    full-screen viewer uses CRITICAL (it renders faults in-panel, and any stderr line would
    corrupt its display)."""
    logging.getLogger(SHARED_INBOX_REPLY_LOGGER).setLevel(level)


class TickerMessage(BaseModel):
    """Ticker message published to Kafka (common schema for all exchanges)."""

    product_id: str
    price: str
    best_bid: str
    best_bid_size: str
    best_ask: str
    best_ask_size: str
    side: str
    last_size: str
    open_24h: str
    high_24h: str
    low_24h: str
    volume_24h: str
    volume_30d: str
    trade_id: int
    sequence: int
    time: str
