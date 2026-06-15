"""Integration tests for the crypto daytrading arena.

Uses FastStream's TestKafkaBroker for in-memory Kafka simulation
(no real broker required). Requires an OpenAI API key for LLM inference.
"""

import asyncio
import os

import pytest
from dotenv import load_dotenv
from faststream.kafka import TestKafkaBroker

from calfkit import Agent, Client, OpenAIModelClient, Worker

from arena.fees import MAX_TAKER_BPS, FeeModel, Fill
from arena.models import INITIAL_CASH
from arena.tools import calculator, execute_trade, get_portfolio, price_book, store

load_dotenv()

# The deployed agent processes all requests; trades are recorded under its name.
AGENT_NAME = "arena_agent"
AGENT_INPUT_TOPIC = "agent_router.input"

skip_if_no_openai_key = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="Skipping: OPENAI_API_KEY not set",
)

# ── Test market data ────────────────────────────────────────────

TEST_PRICES = {
    "BTC-USD": {
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
    },
    "SOL-USD": {
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
    },
}


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def seed_price_book():
    """Seed shared PriceBook with test data and reset accounts between tests.

    Pins the fee model to 0 bps so cash-math assertions below are independent
    of whatever production default is in effect. Fee application is covered
    separately by arena/account_store.py unit paths.
    """
    for data in TEST_PRICES.values():
        price_book.update(data)
    store._accounts.clear()
    store._trade_log.clear()
    store.set_fee_model(FeeModel(taker_bps=0))
    yield
    store._accounts.clear()
    store._trade_log.clear()
    store.set_fee_model(FeeModel())


@pytest.fixture(scope="session")
def deploy_client() -> Client:
    """Wire up all arena worker nodes on a Client.

    Registers: Agent (with embedded model client) and tool nodes.
    The agent processes all incoming requests via the Worker.

    NOTE: temp_instructions on execute_node() are silently dropped by
    pydantic-ai's UserPromptNode — only the Agent's system_prompt reaches
    the model. This system_prompt must work for all tests.
    """
    client = Client.connect()

    model_client = OpenAIModelClient("gpt-5-nano", reasoning_effort="high")

    # Agent node (subscriber for agent_router.input)
    agent = Agent(
        AGENT_NAME,
        system_prompt=(
            "You are an obedient trading bot. Execute trades immediately when asked. "
            "Use tools to check your portfolio and make trades. Never ask for confirmation. "
            "When you see large unrealized gains, sell to lock in profits. Always act decisively."
        ),
        subscribe_topics=AGENT_INPUT_TOPIC,
        model_client=model_client,
        tools=[execute_trade, get_portfolio, calculator],
    )

    worker = Worker(client, nodes=[agent, execute_trade, get_portfolio, calculator])
    worker.register_handlers()

    return client


def _account():
    """Get the arena agent's account from the shared store."""
    return store.get_or_create(AGENT_NAME)


# ── Fee model unit tests (no LLM required) ──────────────────────


def test_buy_fee_capitalized_into_cost_basis():
    """A buy with fees charges notional + fee and rolls the fee into cost basis."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_buy"

    result = store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    assert result.success, result.message

    account = store.get_or_create(agent)
    price = 50010.00  # best_ask
    notional = price * 0.5
    expected_fee = notional * 0.006
    expected_cash_out = notional + expected_fee

    assert account.cash == pytest.approx(INITIAL_CASH - expected_cash_out)
    assert account.cost_basis["BTC-USD"] == pytest.approx(expected_cash_out)


def test_sell_fee_deducted_from_proceeds():
    """A sell with fees credits notional − fee to cash."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_sell"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    account = store.get_or_create(agent)
    cash_after_buy = account.cash

    result = store.execute_trade(agent, "BTC-USD", 0.5, "sell")
    assert result.success, result.message

    sell_price = 49990.00  # best_bid
    sell_notional = sell_price * 0.5
    sell_fee = sell_notional * 0.006
    expected_proceeds = sell_notional - sell_fee

    assert account.cash == pytest.approx(cash_after_buy + expected_proceeds)
    # Fully closed position — round-trip in a flat market always loses both fees
    assert "BTC-USD" not in account.positions


def test_zero_fee_matches_notional():
    """taker_bps=0 recovers the fee-free behavior."""
    store.set_fee_model(FeeModel(taker_bps=0))
    agent = "fee_test_zero"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    account = store.get_or_create(agent)

    assert account.cash == pytest.approx(INITIAL_CASH - 50010.00 * 0.5)
    assert account.cost_basis["BTC-USD"] == pytest.approx(50010.00 * 0.5)


def test_buy_fee_blocks_when_cash_insufficient_for_fee():
    """A buy that fits notional-only but not notional+fee is rejected."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_insufficient"
    account = store.get_or_create(agent)
    # Just enough cash for notional, not for the 0.6% fee on top.
    account.cash = 50010.00 * 0.5

    result = store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    assert not result.success
    assert "Insufficient cash" in result.message


def test_total_fees_paid_accumulates_buy_and_sell():
    """total_fees_paid sums the taker fee from every fill (buys and sells)."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_total"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    store.execute_trade(agent, "BTC-USD", 0.5, "sell")
    account = store.get_or_create(agent)

    buy_fee = 50010.00 * 0.5 * 0.006
    sell_fee = 49990.00 * 0.5 * 0.006
    assert account.total_fees_paid == pytest.approx(buy_fee + sell_fee)


def test_realized_pnl_zero_until_position_closed():
    """Opening a position accrues fees but realizes no P&L until a sell."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_open_only"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    account = store.get_or_create(agent)

    assert account.realized_pnl == 0.0
    assert account.total_fees_paid == pytest.approx(50010.00 * 0.5 * 0.006)


def test_realized_pnl_nets_both_fees_on_round_trip():
    """A flat-market round trip realizes a loss equal to the spread plus both fees.

    Buy fees enter realized P&L through the capitalized cost basis; sell fees are
    deducted from proceeds — so the figure already reflects the full round-trip cost.
    """
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_realized"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    store.execute_trade(agent, "BTC-USD", 0.5, "sell")
    account = store.get_or_create(agent)

    cost_basis = 50010.00 * 0.5 * 1.006  # notional + capitalized buy fee
    proceeds = 49990.00 * 0.5 * 0.994  # notional − sell fee
    assert account.realized_pnl == pytest.approx(proceeds - cost_basis)
    assert account.realized_pnl < 0  # spread + round-trip fees guarantee a loss
    assert "BTC-USD" not in account.positions


def test_partial_sell_reduces_basis_proportionally_and_realizes_pnl():
    """Selling part of a position keeps per-unit cost constant and realizes P&L on the sold portion."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_partial"

    store.execute_trade(agent, "BTC-USD", 1.0, "buy")
    account = store.get_or_create(agent)
    avg_cost = account.avg_cost_per_unit("BTC-USD")  # fee-inclusive

    store.execute_trade(agent, "BTC-USD", 0.4, "sell")

    cash_in = 49990.00 * 0.4 * 0.994  # best_bid notional − sell fee
    assert account.positions["BTC-USD"] == pytest.approx(0.6)
    # Per-unit cost is unchanged by a partial sell; only quantity and basis shrink.
    assert account.avg_cost_per_unit("BTC-USD") == pytest.approx(avg_cost)
    assert account.cost_basis["BTC-USD"] == pytest.approx(avg_cost * 0.6)
    assert account.realized_pnl == pytest.approx(cash_in - avg_cost * 0.4)


def test_multiple_buys_use_weighted_average_cost_including_fees():
    """Two buys at different prices blend into a single fee-inclusive weighted-average cost."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_avg"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    cost1 = 50010.00 * 0.5 * 1.006  # notional + capitalized fee

    # Raise BTC's price, then buy again. The fixture re-seeds the book each test,
    # so this mutation does not leak.
    higher = dict(
        TEST_PRICES["BTC-USD"], price="60000.00", best_bid="59990.00", best_ask="60010.00"
    )
    price_book.update(higher)
    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    cost2 = 60010.00 * 0.5 * 1.006

    account = store.get_or_create(agent)
    assert account.positions["BTC-USD"] == pytest.approx(1.0)
    assert account.cost_basis["BTC-USD"] == pytest.approx(cost1 + cost2)
    assert account.avg_cost_per_unit("BTC-USD") == pytest.approx(cost1 + cost2)


def test_realized_pnl_accumulates_across_multiple_closes():
    """realized_pnl sums P&L across separate closes rather than overwriting."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_accum"

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    store.execute_trade(agent, "BTC-USD", 0.5, "sell")
    account = store.get_or_create(agent)
    first_close = account.realized_pnl
    assert first_close < 0

    store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    store.execute_trade(agent, "BTC-USD", 0.5, "sell")
    assert account.realized_pnl == pytest.approx(2 * first_close)


def test_rejected_buy_leaves_account_state_unchanged():
    """A buy rejected for insufficient cash must not mutate any account state."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_reject"
    account = store.get_or_create(agent)
    account.cash = 100.0  # nowhere near enough for 0.5 BTC

    result = store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    assert not result.success
    assert account.cash == 100.0
    assert account.positions == {}
    assert account.cost_basis == {}
    assert account.total_fees_paid == 0.0
    assert account.realized_pnl == 0.0
    assert account.trade_count == 0


def test_recorder_receives_fee_on_trade():
    """The taker fee is forwarded to an attached TradeRecorder."""
    store.set_fee_model(FeeModel(taker_bps=60))
    agent = "fee_test_recorder"
    captured: dict[str, float] = {}

    class _FakeRecorder:
        def record_trade(
            self, *, agent_id, action, product_id, quantity, price, fee, cash_after, latency
        ):
            captured.update(fee=fee, cash_after=cash_after)

    store.attach_recorder(_FakeRecorder())
    try:
        store.execute_trade(agent, "BTC-USD", 0.5, "buy")
    finally:
        store.attach_recorder(None)  # detach so other tests are unaffected

    assert captured["fee"] == pytest.approx(50010.00 * 0.5 * 0.006)


# ── FeeModel unit tests (no account store) ──────────────────────


def test_fee_model_unit_behavior():
    """FeeModel computes taker_rate and fills as pure functions."""
    fm = FeeModel(taker_bps=60)
    assert fm.taker_rate == pytest.approx(0.006)

    buy = fm.buy_cost(1000.0)
    assert isinstance(buy, Fill)
    assert (buy.cash, buy.fee) == pytest.approx((1006.0, 6.0))

    sell = fm.sell_proceeds(1000.0)
    assert (sell.cash, sell.fee) == pytest.approx((994.0, 6.0))

    # Zero fee is a pass-through.
    assert FeeModel(taker_bps=0).buy_cost(1000.0) == (1000.0, 0.0)


def test_fee_model_rejects_out_of_range_bps():
    """taker_bps must lie within [0, MAX_TAKER_BPS]."""
    with pytest.raises(ValueError):
        FeeModel(taker_bps=-1)
    with pytest.raises(ValueError):
        FeeModel(taker_bps=MAX_TAKER_BPS + 1)


def test_fee_model_disclosure_prompt():
    """The agent-facing disclosure reports the rate and round-trip cost, or fee-free at 0."""
    line = FeeModel(taker_bps=60).disclosure_prompt()
    assert "60 bps" in line and "0.60%" in line and "120 bps" in line  # round trip = 2x
    assert FeeModel(taker_bps=0).disclosure_prompt() == "Trading is fee-free in this deployment."


# ── Tests ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_executes_trade(deploy_client):
    """Agent receives a prompt and executes a BTC buy via the execute_trade tool."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        await client.execute_node(
            user_prompt="Buy 0.1 BTC-USD right now.",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("BTC-USD", 0) > 0, "Agent should have bought BTC"
        assert account.cash < INITIAL_CASH, "Cash should have decreased after buying"
        assert account.trade_count > 0


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_checks_portfolio(deploy_client):
    """Agent uses get_portfolio tool and reports back."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        result = await client.execute_node(
            user_prompt="What does my portfolio look like?",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        assert result.output is not None
        assert "100,000" in str(result.output) or "100000" in str(result.output)


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_multi_turn_trading(deploy_client):
    """Multi-turn conversation: buy, then check portfolio across turns."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        # Turn 1: Buy SOL
        result = await client.execute_node(
            user_prompt="Buy 5 SOL-USD",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("SOL-USD", 0) > 0, "Should have bought SOL"

        # Turn 2: Check portfolio (pass message_history for multi-turn)
        result = await client.execute_node(
            user_prompt="Show me my current portfolio",
            topic=AGENT_INPUT_TOPIC,
            message_history=result.message_history,
            timeout=30.0,
        )

        assert result.output is not None
        assert "sol" in str(result.output).lower(), "Portfolio should mention SOL position"


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_agent_uses_calculator(deploy_client):
    """Agent uses the calculator tool for a math question."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        result = await client.execute_node(
            user_prompt="Use the calculator to compute 50000 * 0.1",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        assert result.output is not None
        assert "5000" in str(result.output)


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_full_trading_session(deploy_client):
    """End-to-end session: buy, sell, check portfolio."""
    client = deploy_client

    async with TestKafkaBroker(client.broker):
        # Buy BTC
        result = await client.execute_node(
            user_prompt="Buy 0.5 BTC-USD",
            topic=AGENT_INPUT_TOPIC,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("BTC-USD", 0) == 0.5
        expected_cost = 50010.00 * 0.5  # best_ask * qty
        assert account.cash == pytest.approx(INITIAL_CASH - expected_cost, rel=1e-2)

        # Sell some
        result = await client.execute_node(
            user_prompt="Sell 0.2 BTC-USD",
            topic=AGENT_INPUT_TOPIC,
            message_history=result.message_history,
            timeout=30.0,
        )

        account = _account()
        assert account.positions.get("BTC-USD", 0) == 0.3
        assert account.trade_count == 2

        # Check portfolio mentions BTC
        result = await client.execute_node(
            user_prompt="Show my portfolio",
            topic=AGENT_INPUT_TOPIC,
            message_history=result.message_history,
            timeout=30.0,
        )

        assert result.output is not None
        assert "btc" in str(result.output).lower()


@pytest.mark.asyncio
@skip_if_no_openai_key
async def test_autonomous_portfolio_check_and_trade(deploy_client):
    """Default-strategy agent checks portfolio and sells into a price spike in one turn.

    Setup:
    - Account pre-seeded with 1.0 BTC-USD at $50,010 cost basis
    - BTC-USD live price spiked to $500,000 (10x unrealized gain)

    The agent should autonomously:
    1. Call get_portfolio — discover BTC position with massive unrealized P&L
    2. Call execute_trade — sell some/all BTC to lock in profits

    Both tool calls occur within a single execute_node() invocation, proving the
    agent makes multiple autonomous tool calls in one turn.
    """
    client = deploy_client

    # Pre-seed the arena_agent account with a BTC position
    account = store.get_or_create(AGENT_NAME)
    account.positions["BTC-USD"] = 1.0
    account.cost_basis["BTC-USD"] = 50_010.0  # avg cost $50,010 (original best_ask)
    account.cash = INITIAL_CASH - 50_010.0  # $49,990 remaining

    # Spike BTC price to $500,000 — a 10x move over cost basis
    price_book.update({
        "product_id": "BTC-USD",
        "price": "500000.00",
        "best_bid": "499500.00",
        "best_bid_size": "5.0",
        "best_ask": "500500.00",
        "best_ask_size": "3.0",
        "side": "buy",
        "last_size": "0.5",
        "volume_24h": "25000.0",
        "time": "2024-01-01T12:00:00Z",
    })

    ticker_json = (
        '[{"product_id": "BTC-USD", "price": "500000.00", '
        '"best_bid": "499500.00", "best_ask": "500500.00"}, '
        '{"product_id": "SOL-USD", "price": "100.00", '
        '"best_bid": "99.90", "best_ask": "100.10"}]'
    )

    async with TestKafkaBroker(client.broker):
        await client.execute_node(
            user_prompt=(
                "Here is the latest ticker information. You should view your "
                "portfolio first before making any decisions to trade.\n"
                "price = last traded price, best_bid = price you sell at, "
                "best_ask = price you buy at.\n\n"
                f"{ticker_json}"
            ),
            topic=AGENT_INPUT_TOPIC,
            timeout=45.0,
        )

    account = _account()
    assert account.trade_count > 0, "Agent should have executed at least one trade"
    pre_trade_cash = INITIAL_CASH - 50_010.0
    assert account.cash > pre_trade_cash, "Cash should have increased from selling BTC"
    assert account.positions.get("BTC-USD", 0) < 1.0, "Agent should have sold some/all BTC"
