"""Unit tests for config.py — loading, env-var resolution, and validation."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from arena.fees import DEFAULT_TAKER_BPS, MAX_TAKER_BPS
from config import (
    AgentConfig,
    ArenaConfig,
    FeeConfig,
    LLMProviderConfig,
    get_default_symbols,
    load_config,
    load_config_strict,
    resolve_env_vars,
)


def _write(tmp_path, payload):
    p = tmp_path / "config.json"
    p.write_text(json.dumps(payload))
    return p


# ── load_config / load_config_strict semantics ───────────────────


def test_absent_file_returns_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.trading.exchange == "coinbase"
    assert cfg.trading.fees.taker_bps == DEFAULT_TAKER_BPS
    assert cfg.trading.binance_symbols  # populated defaults
    assert cfg.trading.coinbase_products


def test_valid_file_is_parsed(tmp_path):
    path = _write(
        tmp_path,
        {"trading": {"exchange": "binance", "fees": {"taker_bps": 10}}},
    )
    cfg = load_config(path)
    assert cfg.trading.exchange == "binance"
    assert cfg.trading.fees.taker_bps == 10


def test_invalid_json_raises(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{ not valid json")
    with pytest.raises(Exception):
        load_config(path)


def test_strict_reraises_on_invalid_fee(tmp_path):
    path = _write(tmp_path, {"trading": {"fees": {"taker_bps": MAX_TAKER_BPS + 1}}})
    with pytest.raises(ValidationError):
        load_config_strict(path)


def test_strict_returns_defaults_when_absent(tmp_path):
    cfg = load_config_strict(tmp_path / "missing.json")
    assert cfg.trading.fees.taker_bps == DEFAULT_TAKER_BPS


# ── resolve_env_vars ─────────────────────────────────────────────


def test_resolve_whole_string_var(monkeypatch):
    monkeypatch.setenv("MY_KEY", "secret-value")
    assert resolve_env_vars("${MY_KEY}") == "secret-value"


def test_resolve_missing_var_raises_with_path(monkeypatch):
    monkeypatch.delenv("ABSENT_VAR", raising=False)
    with pytest.raises(ValueError, match=r"ABSENT_VAR.*llm_providers\.openai\.api_key"):
        resolve_env_vars({"llm_providers": {"openai": {"api_key": "${ABSENT_VAR}"}}})


def test_resolve_recurses_into_dicts_and_lists(monkeypatch):
    monkeypatch.setenv("A", "1")
    monkeypatch.setenv("B", "2")
    out = resolve_env_vars({"x": ["${A}", "${B}"], "y": {"z": "${A}"}})
    assert out == {"x": ["1", "2"], "y": {"z": "1"}}


def test_resolve_passes_through_non_strings():
    assert resolve_env_vars({"n": 5, "b": True, "f": 1.5}) == {"n": 5, "b": True, "f": 1.5}


def test_resolve_does_not_substitute_embedded_refs(monkeypatch):
    """Whole-string-only by design (finding R6): embedded ${VAR} is left verbatim."""
    monkeypatch.setenv("TOKEN", "xyz")
    assert resolve_env_vars("Bearer ${TOKEN}") == "Bearer ${TOKEN}"
    assert resolve_env_vars("${TOKEN}-suffix") == "${TOKEN}-suffix"


# ── get_default_symbols ──────────────────────────────────────────


def test_get_default_symbols_per_exchange(monkeypatch):
    # get_default_symbols reads the repo config.json, which references ${OPENAI_API_KEY};
    # a dummy value keeps resolve_env_vars from raising (see finding R3).
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    assert "BTCUSDT" in get_default_symbols("binance")
    assert "BTC-USD" in get_default_symbols("coinbase")
    assert get_default_symbols("BINANCE") == get_default_symbols("binance")  # case-insensitive


def test_get_default_symbols_unknown_exchange_raises(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    with pytest.raises(ValueError, match="Unknown exchange"):
        get_default_symbols("kraken")


# ── Model validation ─────────────────────────────────────────────


@pytest.mark.parametrize("bps", [-1, MAX_TAKER_BPS + 1])
def test_fee_config_rejects_out_of_range(bps):
    with pytest.raises(ValidationError):
        FeeConfig(taker_bps=bps)


@pytest.mark.parametrize("bps", [0, DEFAULT_TAKER_BPS, MAX_TAKER_BPS])
def test_fee_config_accepts_in_range(bps):
    assert FeeConfig(taker_bps=bps).taker_bps == bps


def test_agent_config_requires_positive_workers():
    with pytest.raises(ValidationError):
        AgentConfig(name="a", provider="openai", model="m", max_workers=0)


def test_agent_config_rejects_unknown_reasoning_effort():
    with pytest.raises(ValidationError):
        AgentConfig(name="a", provider="openai", model="m", reasoning_effort="extreme")


def test_arena_config_lookup_helpers():
    cfg = ArenaConfig(
        llm_providers={"openai": LLMProviderConfig(api_key="k")},
        agents=[AgentConfig(name="momentum", provider="openai", model="m")],
    )
    assert cfg.get_provider_config("openai").api_key == "k"
    assert cfg.get_provider_config("missing") is None
    assert cfg.get_agent_config("momentum").model == "m"
    assert cfg.get_agent_config("missing") is None


# ── Committed schema vs model (finding R5) ───────────────────────


def _find_key(obj, key):
    """First value whose parent dict has `key`, searched depth-first."""
    if isinstance(obj, dict):
        if key in obj and isinstance(obj[key], dict):
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


@pytest.mark.xfail(
    reason="R5: committed config.schema.json omits taker_bps maximum; it has drifted from the model",
    strict=True,
)
def test_committed_schema_encodes_taker_bps_upper_bound():
    schema = json.loads((__import__("pathlib").Path("config.schema.json")).read_text())
    taker = _find_key(schema, "taker_bps")
    assert taker is not None
    assert taker.get("maximum") == MAX_TAKER_BPS
