# Arena review findings

A running log of bugs, robustness gaps, and simplification opportunities found
while building the unit/integration test suite. Each entry has a stable ID so
tests can reference it (`xfail(reason="B2 …")`).

Status legend: **open** (unfixed) · **fixed** · **wontfix/by-design**.

Severity reflects impact on a real deployment, not how hard it is to trigger.

---

## Confirmed bugs

### B1 — Coinbase connector never wires a `CandleBook` (no OHLCV to agents) · High · open
`exchanges/coinbase.py::run()` constructs `CoinbaseKafkaConnector(...)` **without**
a `candle_book=` argument, so `self._candle_book is None`. Two consequences:
1. `_publish_latest` skips the price-history block, so Coinbase agents receive
   **only** ticker price lines — no candlesticks.
2. `_consume_and_publish` gates the REST candle poller on `candle_book is not None`,
   so Coinbase makes **no** candle REST calls at all; `parse_coinbase_candle` /
   `poll_rest` are unreachable in production.

This contradicts every strategy prompt and the README, which promise "multi-timeframe
candlestick charts (1-min, 5-min, 15-min)." Binance's `run()` wires it correctly
(`CandleBook(parse_row=parse_binance_candle)`), so the two connectors are asymmetric.

*Test:* `test_exchanges.py` asserts Binance's `run()` builds a non-None candle book
and (xfail) that Coinbase's `run()` does too.

### B2 — Sub-rounding quantity crashes or records a phantom trade · Medium · open
`AccountStore.execute_trade` validates `quantity > 0` then `abs(quantity - round(quantity, 1)) <= 1e-9`.
A quantity in `(0, 1e-9]` passes **both** guards, then `quantity` is reassigned to
`round(quantity, 1) == 0.0`:
- On a **fresh** position the weighted `avg_entry_ts` computes `(0*ts + 0*now) / (0 + 0)`
  → **`ZeroDivisionError`** (the tool node crashes). *Reproduced.*
- On an **existing** position it "succeeds" as a zero-quantity fill: `trade_count`
  increments and a phantom row is written to the trade log / recorder. *Reproduced.*

Root cause: the precision guard treats "rounds to zero" as acceptable. Correct
behavior: reject any quantity that rounds to `0.0`.

*Test:* `test_account_store.py::test_subrounding_quantity_rejected` (xfail, strict,
raises `ZeroDivisionError` for the fresh-position case).

### B3 — `calculator` raises an uncaught `AttributeError` on non-scalar input · Medium · open
`arena/tools.py::calculator` does `result = sympy.sympify(expression)` then
`result.is_number`. For inputs that sympify to a Python container (e.g. `"[1,2,3]"`
→ `list`, `"(1,2)"` → `tuple`), `.is_number` raises **`AttributeError`**, which the
`except (sympy.SympifyError, TypeError)` clause does not catch → the tool node crashes
instead of returning an "Invalid expression" message. *Reproduced.*

Related quality issues (same tool, lower severity, documented by tests):
- `"1/0"` → `"zoo"` (sympy complex infinity) rather than an error.
- `"sqrt(-1)"` → `"I"` (imaginary unit).
- `"x + 1"` → `"x + 1.0"` — undefined symbols are silently accepted, not rejected.
- The docstring advertises `ceil()`, but sympy's function is `ceiling`; `ceil(3.2)`
  is silently treated as an undefined symbolic function (returns `"ceil(3.2)"`), not
  `4`. Likewise `log()` is natural log (ln), not log10 — `log(100)` ≈ `4.605`, not `2`.

*Test:* `test_tools.py::test_calculator_container_input` (xfail, strict, raises
`AttributeError`) plus characterization tests pinning the `zoo`/`I`/symbol behavior.

### B4 — Dead REST work in both connectors · Low · open
- `exchanges/binance.py::_consume_and_publish` passes a **throwaway** `PriceBook()`
  into `poll_rest`, so every per-symbol `get_24h_ticker` REST round-trip is parsed
  into a book that is never published or read. The trading price book is hydrated
  only by the Kafka subscriber in `tools_and_dashboard`.
- `exchanges/coinbase.py::poll_rest` fetches `/products/{id}/ticker`, calls
  `raise_for_status()`, then discards the response.

Wasted network/CPU; no correctness impact on live pricing (the WebSocket feeds it).

### B5 — `NaN` quantity bypasses every guard and permanently poisons an account · High · open
`AccountStore.execute_trade` guards with `quantity <= 0` and `abs(quantity - rounded) > 1e-9`.
**Both are `False` for `NaN`** (every `NaN` comparison is false), so a `NaN` buy runs
full execution: `cash`, `positions[pid]`, `cost_basis[pid]`, `avg_entry_ts[pid]` and
`total_fees_paid` all become `NaN`. The account is then unrecoverable — every later
guard compares against `NaN` (always false) so no trade can ever succeed, and
`portfolio_value` is `NaN` forever. *Reproduced:* `success=True, cash=nan, pos={'BTC-USD': nan}`.
Correct behavior: reject non-finite quantities up front (`math.isfinite`). `inf` is
incidentally caught on buys only (`inf > cash`) but not defensively rejected either.

*Test:* `test_account_store.py::test_nan_quantity_rejected` (xfail, strict — currently
returns success).

### B6 — Float drift in position accumulation strands a full-position sell · High · open
Buys accumulate raw floats (`positions[pid] = existing_qty + quantity`) while the sell
guard compares the user's rounded request against the drifted stored quantity
(`if quantity > held`). After buying `0.1 + 0.1 + 0.7`, `held == 0.8999999999999999`;
selling `0.9` is **rejected** with "Insufficient holdings … only hold 0.8999999999999999"
(the raw 17-digit float even leaks into the agent-facing message). *Reproduced.* A real
agent that tracks its own position size can be permanently unable to fully exit.
Correct behavior: store quantities quantized (the code already rounds elsewhere) so
`held` is exact, or compare with a tolerance.

*Test:* `test_account_store.py::test_full_position_sell_after_fractional_buys`
(xfail, strict — the sell currently fails).

### B7 — Snapshot/dashboard report a fabricated `unrealized_pnl` for unpriced positions · High · open
In `recorder.py::take_snapshot` (and identically `dashboard.py`), a held position with
no live quote gets `market_price = 0.0`, so `market_value = 0.0` and
`unrealized_pnl = market_value - cost_basis = -cost_basis`. But the **same row's**
`portfolio_value` comes from `AgentAccount.portfolio_value`, which *skips* unpriced
positions — so one CSV row claims `unrealized_pnl=-60000` while `portfolio_value=100000`
for that holding. *Reproduced.* The trading tool already does the right thing (emits
`N/A`), so the recorder/dashboard diverge from the codebase's own convention.

*Test:* `test_recorder.py::test_snapshot_unpriced_position_pnl_is_consistent`
(xfail, strict — asserts the row isn't internally contradictory).

---

## Robustness / silent-failure gaps

### R1 — Periodic publish loop dies silently on the first broker error · Medium · open
`coinbase.py::_periodic_agent_invoke` / `binance.py::_periodic_publish` run
`await self._publish_latest()` with no `try/except`. If `invoke_node` raises (broker
hiccup, serialization error), the task dies; ticker consumption keeps running, so no
reconnect is triggered. Net effect: **agents stop being invoked** for the lifetime of
the current WebSocket connection, with no log beyond the task's unretrieved exception.

### R2 — No staleness eviction in `_latest` · Low · open
`_publish_latest` snapshots `list(self._latest.values())` with no freshness check. A
product that stops updating keeps being published indefinitely at its last price; the
agent cannot tell the quote is stale.

### R3 — `get_default_symbols` raises on a malformed config / unset env var · Low · open
`config.get_default_symbols` calls the non-strict `load_config`, which only returns
defaults when the file is **absent**. If `config.json` exists but is invalid JSON,
the call raises instead of falling back. Worse, because the shipped `config.json`
references `${OPENAI_API_KEY}`, simply asking for default *symbols* fails with
`ValueError: Environment variable 'OPENAI_API_KEY' is not set` whenever that var is
unset — and that env error is raised *before* the exchange name is validated, so an
unknown exchange never reaches its own "Unknown exchange" error. Surprising for a
"get defaults" helper.

### R4 — Realized and unrealized P&L use inconsistent fee conventions · Medium · open
Realized P&L nets the exit fee (`cash_in` is post-fee) and the capitalized entry fee
(via `avg_cost`). Unrealized P&L everywhere is `market_value - cost_basis` with **no**
hypothetical exit fee subtracted. So unrealized P&L systematically overstates gains
relative to what realized P&L will book on close, and `realized + unrealized` does not
reconcile to actual cash deltas at the exit-fee level. Likely a deliberate "mark at
mid, fee on close" convention, but it's undocumented and surprising. *Documented; a
characterization test pins the current numbers so a future change is intentional.*

### R5 — `config.schema.json` has drifted from the Pydantic model · Medium · open
`FeeConfig.taker_bps` enforces `le=MAX_TAKER_BPS` (1000), but the committed
`config.schema.json` `taker_bps` has only `minimum: 0` and **no `maximum`** — so a
schema-aware editor accepts `taker_bps: 5000` that Pydantic then rejects at load. The
`FeeConfig` description is also stale. (Note: `model_json_schema()` omits the top-level
`$schema` key the committed file carries, so any regenerator must re-inject it.)

*Test:* `test_config.py::test_committed_schema_matches_model` (xfail, strict — drift
exists today; flips green when the schema is regenerated).

### R6 — `resolve_env_vars` silently passes through embedded `${VAR}` · Low · open
The regex is whole-string anchored (`^\$\{...\}$`), so `"Bearer ${TOKEN}"`,
`"${VAR}suffix"`, and `"${A}${B}"` are returned **verbatim, unresolved**, with no
warning — an operator templating a header would ship the literal `${...}` and get an
opaque auth failure. The whole-string and missing-var paths are correct (the latter
raises with the right dotted `path`). *Documented; a test pins the contract.*

### R7 — Advertised fee can desync from charged fee across processes · Low · open
The connector (advertises via `disclosure_prompt`) and the tools node (charges in
`execute_trade`) each independently `load_config_strict(args.config)`. They agree only
if the operator passes the **same** `--config` to both and doesn't edit it between
launches. The "single source of truth" is the file path, not a shared value — the
docstrings overstate the guarantee. *Documented; a test pins in-process consistency.*

### R8 — Unbounded in-memory growth in long sessions · Low · open
`AccountStore._trade_log` is appended to and never trimmed, and the dashboard
re-renders the entire reversed list every tick. `response_viewer`'s `_log` and `_seen`
also grow per turn forever. (The dashboard's balance history *is* correctly bounded
with `deque(maxlen=…)`, so the fix pattern already exists.)

### R9 — No invariant enforcement on account maps · Low · open
Nothing asserts `cost_basis >= 0`, `positions[pid] > 0`, or that `positions`,
`cost_basis`, and `avg_entry_ts` share one key set. Full-close cleanup is asymmetric
(`del cost_basis[pid]` / `del positions[pid]` vs `avg_entry_ts.pop(pid, None)`),
working only because line-ordering guarantees the key exists — fragile coupling rather
than an enforced contract. *A property test pins the keys-stay-in-sync invariant.*

---

## Simplifications / testability

### S1 — Duplicate candle parser
`arena/price_book.py::_default_parse_row` is byte-for-byte identical to
`exchanges/coinbase.py::parse_coinbase_candle`. Collapse to one.

### S2 — Near-duplicate connectors
`CoinbaseKafkaConnector` and `BinanceKafkaConnector` share ~90% of their bodies,
including a hand-duplicated `_exclude` set and identical prompt text, plus an
asymmetric periodic-task name (`_periodic_agent_invoke` vs `_periodic_publish`). The
duplication is what allowed B1 to regress on one side only. A shared base or an
exchange-adapter parameterization would remove the asymmetry.

### S3 — `AccountStore` has no public reset
Tests reach into `store._accounts` / `store._trade_log` to reset the module
singleton. A `reset()` method (or preferring fresh `AccountStore` instances in tests)
would remove the private-state coupling.

### S4 — Triplicated "unpriced position" logic
`recorder.py`, `dashboard.py`, and `tools.py` each re-derive market value from
`price_book.get(pid)` with three *different* fallbacks (`0.0`, `0.0`, `N/A`). A single
helper returning `(market_price, market_value, unrealized_pnl) | None` would make all
call sites — and `portfolio_value` — agree by construction and fix B7 in one place.

### S5 — Quantize positions/cash to avoid float drift
Storing quantities as a fixed step (integer tenths) or `Decimal` would eliminate B6 and
the residual-state class entirely, and make `quantity > held` / full-close detection
exact.

### S6 — Bound the trade log and viewer buffers
Apply `deque(maxlen=…)` to `_trade_log` and the response-viewer buffers to match the
already-bounded balance history (fixes R8 and makes memory behavior uniform/testable).

---

## Fixed during this pass
- **Lint:** removed unused imports (`arena/price_book.py`, `deploy/router_node.py`,
  `tests/test_arena.py`) and an f-string-without-placeholders (`deploy/router_node.py`)
  so the repo is `ruff`-clean and the CI lint gate is green.
</content>
</invoke>
