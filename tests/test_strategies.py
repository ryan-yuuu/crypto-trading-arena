"""Unit tests for the strategy registry that deploy/router_node selects from."""

from __future__ import annotations

from arena.strategies import STRATEGIES


def test_expected_strategies_are_registered():
    assert set(STRATEGIES) == {"default", "momentum", "brainrot", "scalper"}


def test_every_strategy_prompt_requests_reasoning():
    """Each prompt ends with the shared reasoning addendum so agents explain trades."""
    for name, prompt in STRATEGIES.items():
        assert prompt.strip(), name
        assert "Reasoning:" in prompt, name


def test_strategy_prompts_are_distinct():
    assert len(set(STRATEGIES.values())) == len(STRATEGIES)
