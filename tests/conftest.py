"""Shared pytest configuration and fixtures for the arena test suite.

Two opt-in test categories keep the default run fast and dependency-free:

* ``@pytest.mark.llm``    — performs a live LLM inference call. Auto-skips when
  ``OPENAI_API_KEY`` is unset; CI also excludes them with ``-m "not llm"``.
* ``@pytest.mark.broker`` — needs a live Kafka-API broker. Skipped unless
  ``--run-broker`` is passed, following pytest's recommended pattern for tests
  that require an external resource. The broker fixture spins up Redpanda via
  testcontainers, so a Docker daemon is the only prerequisite.
"""

import logging

import pytest

from arena.account_store import AccountStore
from arena.fees import FeeModel
from arena.price_book import PriceBook

# ── Canonical test market data ───────────────────────────────────
# best_ask > price > best_bid so buy/sell price selection is observable, and
# the spread is wide enough that fee-vs-spread effects are distinguishable.

BTC_TICKER = {
    "product_id": "BTC-USD",
    "price": "50000.00",
    "best_bid": "49990.00",
    "best_bid_size": "1.5",
    "best_ask": "50010.00",
    "best_ask_size": "2.0",
    "side": "buy",
    "last_size": "0.1",
    "volume_24h": "15000.0",
    "time": "2024-01-01T00:00:00Z",
}

SOL_TICKER = {
    "product_id": "SOL-USD",
    "price": "100.00",
    "best_bid": "99.90",
    "best_bid_size": "100",
    "best_ask": "100.10",
    "best_ask_size": "150",
    "side": "buy",
    "last_size": "5.0",
    "volume_24h": "500000.0",
    "time": "2024-01-01T00:00:00Z",
}


# ── Collection hooks: markers + external-dependency opt-in ────────


def pytest_configure(config):
    """Quiet noisy third-party loggers; arena.* DEBUG logs stay visible."""
    for name in ("openai", "httpcore", "httpx", "asyncio", "aiokafka", "faststream"):
        logging.getLogger(name).setLevel(logging.WARNING)


def pytest_addoption(parser):
    parser.addoption(
        "--run-broker",
        action="store_true",
        default=False,
        help="run tests marked @pytest.mark.broker (spins up a live Redpanda broker via Docker)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip broker tests unless explicitly opted in with --run-broker."""
    if config.getoption("--run-broker"):
        return
    skip_broker = pytest.mark.skip(
        reason="needs --run-broker (starts a live Redpanda broker via testcontainers)"
    )
    for item in items:
        if "broker" in item.keywords:
            item.add_marker(skip_broker)


# ── Shared fixtures ──────────────────────────────────────────────


@pytest.fixture
def btc_ticker() -> dict:
    """A fresh copy of the BTC ticker payload (mutations don't leak across tests)."""
    return dict(BTC_TICKER)


@pytest.fixture
def sol_ticker() -> dict:
    return dict(SOL_TICKER)


@pytest.fixture
def price_book() -> PriceBook:
    """A PriceBook seeded with BTC-USD and SOL-USD live quotes."""
    pb = PriceBook()
    pb.update(dict(BTC_TICKER))
    pb.update(dict(SOL_TICKER))
    return pb


@pytest.fixture
def make_store(price_book):
    """Factory for an AccountStore over the seeded price book at a chosen fee rate.

    Returns fresh instances each call, so tests never share account state — unlike
    the module-level singleton in arena.tools (see finding S3).
    """

    def _make(taker_bps: int = 0) -> AccountStore:
        return AccountStore(price_book, FeeModel(taker_bps=taker_bps))

    return _make
