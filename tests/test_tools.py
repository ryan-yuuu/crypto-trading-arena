"""Unit tests for arena.tools — calculator, hold-time formatting, portfolio render.

calculator is an @agent_tool (a ToolNodeDef); its underlying callable is reached via
`calculator._tool.function`, which only touches ctx.agent_name / ctx.tool_call_id for
logging, so a lightweight stub context is sufficient (no LLM, no broker).
"""

from __future__ import annotations

import time
import types
from datetime import datetime, timezone

import pytest

import arena.tools as tools
from arena.tools import _format_hold_time, _get_portfolio, calculator

_STUB_CTX = types.SimpleNamespace(agent_name="tester", tool_call_id="tc-1")


def _calc(expression: str) -> str:
    return calculator._tool.function(_STUB_CTX, expression)


# ── calculator: valid math ───────────────────────────────────────


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("2 + 3 * 4", "14"),  # PEMDAS
        ("2 ** 10", "1024"),
        ("max(10, 20)", "20"),
        ("min(10, 20)", "10"),
        ("sqrt(16)", "4"),
        ("abs(-5)", "5"),
        ("floor(3.7)", "3"),
    ],
)
def test_calculator_evaluates_valid_expressions(expression, expected):
    assert _calc(expression) == expected


def test_calculator_ceil_is_not_recognized():
    """The docstring advertises ceil(), but sympy's function is `ceiling` — `ceil`
    is silently treated as an undefined symbolic function (finding B3 / doc gap)."""
    assert _calc("ceil(3.2)") == "ceil(3.2)"  # not evaluated to 4
    assert _calc("ceiling(3.2)") == "4"  # the name sympy actually understands


def test_calculator_float_result():
    assert "5000" in _calc("50000 * 0.1")


def test_calculator_invalid_expression_returns_message():
    assert _calc("1 +").startswith("Invalid expression")
    assert _calc("import os").startswith("Invalid expression")
    assert _calc("").startswith("Invalid expression")


# ── calculator: documented sharp edges (characterization, see B3) ─


def test_calculator_division_by_zero_returns_zoo():
    """sympy yields complex-infinity 'zoo' rather than an error — documented quirk."""
    assert _calc("1/0") == "zoo"


def test_calculator_accepts_undefined_symbols_silently():
    """`x + 1` is not rejected; sympy treats x as a free symbol."""
    assert "x" in _calc("x + 1")


@pytest.mark.xfail(
    reason="B3: container/non-scalar input raises AttributeError instead of an error message",
    raises=AttributeError,
    strict=True,
)
def test_calculator_container_input_is_handled():
    # Desired: a friendly 'Invalid expression' message, not an unhandled crash.
    assert _calc("[1, 2, 3]").startswith("Invalid expression")


# ── _format_hold_time ────────────────────────────────────────────


@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze arena.tools' clock at epoch 1_000_000 for deterministic hold times."""

    class _Clock:
        @classmethod
        def now(cls):
            return datetime.fromtimestamp(1_000_000, tz=timezone.utc)

    monkeypatch.setattr(tools, "datetime", _Clock)
    return 1_000_000


def test_format_hold_time_none_is_na():
    assert _format_hold_time(None) == "N/A"


@pytest.mark.parametrize(
    "ago_seconds,expected",
    [
        (30, "30s"),
        (600, "10m"),  # 600s = 10 min
        (7_200, "2.0h"),  # 7200s = 2 h
        (172_800, "2.0d"),  # 172800s = 2 d
    ],
)
def test_format_hold_time_buckets(frozen_clock, ago_seconds, expected):
    assert _format_hold_time(frozen_clock - ago_seconds) == expected


# ── _get_portfolio render ────────────────────────────────────────


@pytest.fixture
def seeded_singleton():
    """Seed the arena.tools module singletons used by _get_portfolio, then reset."""
    tools.price_book.update(
        {"product_id": "BTC-USD", "price": "50000", "best_bid": "49990", "best_ask": "50010"}
    )
    tools.store._accounts.clear()
    tools.store._trade_log.clear()
    yield tools.store
    tools.store._accounts.clear()
    tools.store._trade_log.clear()


def test_get_portfolio_empty(seeded_singleton):
    out = _get_portfolio("agent")
    assert "Cash: $100,000.00" in out
    assert "Positions: none" in out
    assert "Total portfolio value: $100,000.00" in out


def test_get_portfolio_with_priced_position(seeded_singleton):
    acct = seeded_singleton.get_or_create("agent")
    acct.positions["BTC-USD"] = 1.0
    acct.cost_basis["BTC-USD"] = 48_000.0

    out = _get_portfolio("agent")
    assert "BTC-USD" in out
    assert "$50,000.00" in out  # current price column (last price)
    # Total value = cash (100k) + 1 * 50000 mark.
    assert "Total portfolio value: $150,000.00" in out


def test_get_portfolio_position_without_price_shows_na(seeded_singleton):
    acct = seeded_singleton.get_or_create("agent")
    acct.positions["DOGE-USD"] = 100.0  # no quote in the price book
    acct.cost_basis["DOGE-USD"] = 50.0

    out = _get_portfolio("agent")
    assert "DOGE-USD" in out
    assert "N/A" in out  # current price / mkt value / pnl rendered as N/A


# ── execute_trade: latency stopwatch via ctx.deps ────────────────
#
# The connector stamps deps={"invoked_at": <time>} on the agent invocation; the
# tool turns it into a per-fill latency. calfkit 0.12 exposes ToolContext.deps as
# the producer-supplied Mapping directly (`ctx.deps["k"]`), replacing 0.2's
# `ctx.deps.provided_deps`.


def _exec_ctx(deps: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(agent_name="agent", tool_call_id="tc-1", deps=deps)


def test_execute_trade_records_latency_from_invoked_at(seeded_singleton, monkeypatch):
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    ctx = _exec_ctx({"invoked_at": time.time() - 0.5})

    msg = tools.execute_trade._tool.function(ctx, "BTC-USD", 0.1, "buy")

    assert msg.startswith("Bought")
    latency = seeded_singleton.trade_log[-1].latency
    assert latency is not None and latency >= 0.5


def test_execute_trade_latency_none_when_dep_absent(seeded_singleton, monkeypatch):
    """No invoked_at dep (a manual/test invocation) → latency is None, not a crash."""
    monkeypatch.setattr(tools.view, "rerender", lambda: None)
    ctx = _exec_ctx({})

    tools.execute_trade._tool.function(ctx, "BTC-USD", 0.1, "buy")

    assert seeded_singleton.trade_log[-1].latency is None
