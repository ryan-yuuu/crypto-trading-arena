"""Unit tests for arena.dashboard — stateful logic, not pixel rendering.

Covers balance-history capture/cap, summary-card ranking, and that the chart renders
on both the empty and populated branches (guarding the max()-on-empty edge).
"""

from __future__ import annotations

import io

from rich.console import Console

from arena.dashboard import MAX_BALANCE_HISTORY, PlotextChart, PortfolioView


def _render(renderable, width=200) -> str:
    console = Console(file=io.StringIO(), width=width)
    console.print(renderable)
    return console.file.getvalue()


def test_capture_balance_snapshot_records_portfolio_value(make_store):
    store = make_store(taker_bps=0)
    store.execute_trade("a", "BTC-USD", 1.0, "buy")
    view = PortfolioView(store)

    view._capture_balance_snapshot()

    history = view._balance_history["a"]
    assert len(history) == 1
    _ts, value = history[0]
    assert value == store.get_or_create("a").portfolio_value(store.price_book)


def test_balance_history_is_capped_at_maxlen(make_store):
    store = make_store(taker_bps=0)
    store.get_or_create("a")
    view = PortfolioView(store)

    for _ in range(MAX_BALANCE_HISTORY + 25):
        view._capture_balance_snapshot()

    assert len(view._balance_history["a"]) == MAX_BALANCE_HISTORY


def test_summary_cards_ranked_by_portfolio_value_descending(make_store):
    store = make_store(taker_bps=0)
    store.get_or_create("winner").cash = 200_000.0
    store.get_or_create("loser").cash = 50_000.0
    view = PortfolioView(store)

    text = _render(view._build_summary_cards())
    # Highest value gets rank #1 and renders in the leftmost column.
    assert "#1" in text and "#2" in text
    assert text.find("#1") < text.find("#2")
    assert text.find("winner") < text.find("loser")


def test_summary_cards_handle_no_accounts(make_store):
    view = PortfolioView(make_store())
    assert "No accounts yet" in _render(view._build_summary_cards())


def test_chart_renders_for_empty_and_populated_history(make_store):
    store = make_store(taker_bps=0)
    store.get_or_create("a")
    view = PortfolioView(store)

    # Empty history → the "waiting" branch must render without max()-on-empty crashing.
    empty_out = _render(PlotextChart(view._balance_history))
    assert empty_out.strip()

    for _ in range(3):
        view._capture_balance_snapshot()
    populated_out = _render(PlotextChart(view._balance_history))
    assert populated_out.strip()
