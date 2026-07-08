# Migration plan: calfkit 0.2.1 → 0.12.6

**Status:** Phases 0–4 implemented and verified (see progress log)
**Date:** 2026-07-06
**Dependency bump:** applied (`calfkit>=0.12.6` in `pyproject.toml`, `uv.lock` resolved).

### Progress log

- **Phase 0 (guardrails)** — ✅ `AGENT_INPUT_TOPIC`/`AGENT_OUTPUT_TOPIC` centralized in
  `exchanges/__init__.py`; duplicate literals removed from both connectors, `deploy/agent.py`,
  and 2 test modules.
- **Phase 1 (unbreak)** — ✅ all 6 breaking-change items migrated (TDD). Verified: unit suite
  **131 passed**; broker-integration suite **3 passed** against real Redpanda (random port);
  in-memory `send()` smoke; ruff clean.
- **Phase 2 (viewer rewrite)** — ✅ `response_viewer.py` 286→239 LOC, vendored-`pydantic_ai`
  import eliminated; connectors bind `inbox_topic=AGENT_OUTPUT_TOPIC`. Verified **end-to-end**
  (real Redpanda + real gpt-5-nano): a separate viewer client observed a live agent's
  `ToolCallEvent`/`ToolResultEvent` via `client.events()` while the connector's `send()`
  drove a real trade.
- **Phase 3 (`ck run`)** — ⛔ **gate failed, no change (by evidence).** `ck run` runs static
  `module:attr` node targets; `deploy/agent.py` builds its `Agent` dynamically from CLI args
  (`--strategy`/`--model-id`/`--from-config`). Forcing it into `ck run` would move all config
  to import-time env/global state — a worse design. `deploy/agent.py` already ends in
  `worker.run()` (owns the broker lifecycle), so there was no lifecycle boilerplate to reclaim.
- **Phase 4 (docs + ADRs)** — ✅ ADRs `docs/adr/0001` (broadcast via `topic=` escape hatch) and
  `docs/adr/0002` (viewer via shared inbox + `events()`). README/CLI_REFERENCE viewer sections
  are still accurate (CLI unchanged); `deploy/router_node.py`→`deploy/agent.py` rename done.
- **Post-review hardening** — ✅ a deep multi-agent review round found and fixed: the periodic
  publish loop now guards `send()` failures (was a silent multi-hour trading halt if `send()`
  raised); the viewer isolates per-event render errors, surfaces the dropped-event counter,
  renders `RUN FAILED` rows, and restores tool-result→agent attribution via `tool_call_id`
  correlation; connector + viewer quiet the shared-inbox hub log spam. Added tests: agent-topic
  fan-out (real broker), `invoked_at` wire survival, `inbox_topic` binding, and 8 viewer-formatter
  unit tests. Suite: **143 passed**.

---

## 1. Goal & scope

Migrate the arena from calfkit **0.2.1 → 0.12.6**, achieving a **net decrease in
application code** by replacing hand-rolled machinery with the SDK's now-native
primitives — **without changing how the arena trades**.

### In scope (approved)

- **Mandatory "unbreak"** — replace the removed/changed APIs so the app runs on 0.12.6.
- **`response_viewer.py` rewrite** on the typed `client.events()` firehose (deletes the
  codebase's only vendored-internal import and ~half the file).
- **Centralize topic-name constants** and modernize the client lifecycle.
- **`ck run` boilerplate reduction** for `deploy/agent.py` — *gated* on the CLI actually
  fitting the app's dynamic config resolution (fallback: keep argparse, bank the lifecycle
  cleanup only).
- **Rename `deploy/router_node.py` → `deploy/agent.py`** — ✅ already done (one agent per
  process; the file does no routing itself despite the `agent_router.*` topic names).

### Explicitly out of scope (deferred to separate initiatives)

- `AnthropicModelClient` + prompt caching (arena is OpenAI-compatible only today).
- `ck dev` bundled zero-setup local broker.
- Structured trade output (`Agent(final_output_type=…)`).
- **Agent mesh / peers / handoff** — this changes *how the arena trades* (agents delegating
  to a shared risk/execution agent), so it is a product decision, not a migration.

---

## 2. Context: what changed in calfkit (37 releases, no official migration guide)

- **Not wire-compatible with 0.2.1.** The on-the-wire `Envelope` gained a `reply` slot and a
  `WIRE`/`x-calf-wire` stamp; Kafka headers changed (`x-calf-emitter*`, `x-calf-kind`). A
  0.12.6 producer and a 0.2.1 consumer cannot share a topic. **See constraint #1.**
- **Client invocation redesigned.** `Client.invoke_node` / `Client.execute_node` **removed**
  → address a destination via `client.agent(name | topic=…)` returning an `AgentGateway`
  with `.send()` (fire-and-forget → `Dispatch`), `.start()` (→ `InvocationHandle`),
  `.execute()` (request/reply → `InvocationResult`).
- **Lifecycle:** `client.close()` **removed** → `client.aclose()` (or `async with client:`).
  `__aenter__` does **not** start the broker; startup is lazy (`_ensure_started()` on first
  `send()`), or eager via `client.broker.start()`.
- **Deps reshaped:** `ctx.deps.provided_deps["k"]` → `ctx.deps["k"]` (read-only `Mapping`;
  values must be JSON-serializable).
- **Typed caller-side observability:** `client.events()` yields a `RunEvent` union
  (`AgentMessageEvent`, `ToolCallEvent`, `ToolResultEvent`, `HandoffEvent`, terminals
  `RunCompleted`/`RunFailed`) with public `calfkit.models.payload` parts — replacing the
  `Envelope`-internals + vendored-`pydantic_ai` message introspection.
- **CLI renamed** `calfkit` → `ck` (adds `ck run`, `ck dev`, `ck chat`).
- New but deferred: agent mesh, fault rail, MCP toolbox, control plane / ktables,
  provisioning, structured output, Anthropic client.

---

## 3. Hard constraints (non-negotiable)

1. **Atomic, lockstep cutover.** No mixed 0.2.1/0.12.6 fleet. The exchange connector, **every**
   `deploy.agent` worker, and the tools-&-dashboard process must be deployed together off the
   0.12.6 build. There is no on-the-wire back-compat shim.
2. **Preserve the broadcast fan-out via the `topic=` escape hatch.** The arena's "every agent
   sees every tick" model works because all agents subscribe to the *same*
   `agent_router.input` topic under *distinct* consumer groups (`group_id=<agent-name>`).
   `client.agent(<name>)` targets a *per-agent private* topic — using it would silently
   deliver each tick to only one agent. **Always** use
   `client.agent(topic="agent_router.input")` on the publish side and keep
   `Agent(subscribe_topics="agent_router.input")` on the subscribe side.
3. **TDD.** For every change, update the failing test/double **first**, then the code
   (`/test-driven-development`). The unit suite (`-m "not llm and not broker"`) stays green at
   every step; the broker-integration suite (`--run-broker`, testcontainers) is the wire-level
   safety net and must pass after Phases 1–2.

---

## 4. Breaking-change inventory (removed/changed APIs the app uses)

| # | File:line | Old (0.2.1) | New (0.12.6) |
|---|-----------|-------------|--------------|
| 1 | `arena/tools.py:141-142` | `ctx.deps.provided_deps["invoked_at"]` | `ctx.deps.get("invoked_at")` |
| 2 | `exchanges/coinbase.py:200`, `exchanges/binance.py:332` | `client.invoke_node(user_prompt=…, topic=…, deps=…)` | `client.agent(topic="agent_router.input").send(prompt, deps=…)` |
| 3 | `exchanges/coinbase.py:154`, `exchanges/binance.py:259` | `await client.close()` | `await client.aclose()` |
| 4 | `tests/test_arena.py` (9 sites) | `client.execute_node(user_prompt=…, topic=…, timeout=…, message_history=…)` | `client.agent(topic=…).execute(prompt, timeout=…, message_history=…)` |
| 5 | `tests/test_exchanges.py:41` | `_FakeClient.invoke_node(...)` | fake `.agent(topic=…)` → object with `async def send(prompt, *, deps)` |
| 6 | `tests/test_broker_integration.py:63` | `await c.close()` | `await c.aclose()` |
| 7 | `deploy/response_viewer.py:34-42,176,223-232` | `calfkit._vendor.pydantic_ai.messages`, `Envelope` internals, raw `broker.subscriber` | `client.events()` + typed `RunEvent` (Phase 2) |

**Unchanged / still valid** (verified): `Agent`, `Worker`, `OpenAIModelClient`,
`@agent_tool`, `ToolContext.agent_name`/`.tool_call_id`/`.correlation_id`,
`worker.register_handlers()`, `client.broker` (escape hatch for the raw price side-channel),
`InvocationResult.output` / `.message_history`.

---

## 5. Code-reduction opportunities

- **`response_viewer.py` 286 → 239 LOC (−47 net; ~115 lines of introspection deleted, partly
  offset by explanatory comments + the post-review hardening).** Delete the 5 vendored `pydantic_ai` imports, the
  `Envelope` import, `_extract_agent_name` (`event.emitter` replaces it), the `_seen` dedup
  (each event is emitted once), the `FastStream` app, and the `isinstance` message-part
  branching (→ `match event`). Keep only the Rich `ActivityView` and the event→row mapping.
- **`deploy/agent.py` boilerplate** via `ck run` — gated (§ Phase 3).
- **Legitimately unchanged (do NOT "modernize"):**
  - The **price side-channel** — `client.broker.publish(ticker, PRICE_TOPIC)` /
    `@client.broker.subscriber(PRICE_TOPIC)` carry *plain `TickerMessage` pydantic models*,
    not calfkit envelopes. A `@consumer` node would fail to envelope-decode them. Raw broker
    pub/sub is the correct tool here.
  - The connectors' **coalesce-and-throttle** logic (`_latest` + `_periodic_*`) and WebSocket
    reconnect/ping plumbing — app domain logic with no SDK equivalent.

**Net target: −150 to −200 LOC, zero behavior change.**

---

## 6. Phased plan

### Phase 0 — Guardrails (no behavior change)

- **Centralize topic literals.** `agent_router.input` / `agent_router.output` are scattered as
  string literals across the connectors, `deploy/agent.py`, `deploy/response_viewer.py`, and
  tests. Hoist them next to `PRICE_TOPIC` in `exchanges/__init__.py` (or a small
  `arena/topics.py`) as `AGENT_INPUT_TOPIC` / `AGENT_OUTPUT_TOPIC`. This single source of truth
  is what enforces constraint #2. **Do this first** — every later phase imports from it.
- Confirm the broker-integration suite can run locally (Docker/testcontainers available).

### Phase 1 — Mandatory unbreak (TDD, breaking-change table §4 items 1–6)

Order: adjust each test/double, then the code, keeping the unit suite green.

1. **`arena/tools.py`** — `ctx.deps.get("invoked_at")` (drops the `isinstance`/`provided_deps`
   guard). Update any `ToolContext` doubles in `tests/test_tools.py`.
2. **Connectors** (`coinbase.py`, `binance.py`) — hoist `gw = client.agent(topic=AGENT_INPUT_TOPIC)`
   once; `invoke_node(user_prompt=P, deps=D)` → `await gw.send(P, deps=D)`. Reshape
   `_FakeClient` in `tests/test_exchanges.py` first.
3. **Connectors** — `close()` → `aclose()` (keep the explicit `broker.start()`; it sets
   `broker.running`, which `_ensure_started()` fast-paths). Update
   `tests/test_broker_integration.py`.
4. **`tests/test_arena.py`** — `execute_node(...)` → `client.agent(topic=…).execute(...)`
   (9 sites); `result.output` / `.message_history` assertions stay valid.

Exit: full unit suite + broker-integration suite green.

### Phase 2 — `response_viewer.py` rewrite (Option A: shared durable inbox)

- **Connector** connects with `Client.connect(inbox_topic=AGENT_OUTPUT_TOPIC)`. Because
  `send()`/`start()` stamp `callback_topic = client.inbox_topic`, all agent step-events +
  terminals route to `agent_router.output`.
- **Viewer** drops the vendored imports / `Envelope` / `FastStream` / `_seen` /
  `_extract_agent_name` and becomes:
  ```python
  async with client.events() as stream:
      async for event in stream:
          match event:
              case ToolCallEvent():   view.record(event.emitter, "TOOL CALL", …)
              case ToolResultEvent():  view.record(event.emitter, "TOOL RESULT", …)
              case AgentMessageEvent(): view.record(event.emitter, "RESPONSE", …)
  ```
  `event.emitter` is the agent id; `ToolCallEvent.args` is already parsed. Keep the Rich
  `ActivityView` rendering.
- **Best-effort, by design.** `events()` is a bounded drop-oldest firehose — acceptable for a
  live dashboard (the old viewer was also lossy + deduped). Durable capture would use a
  `@consumer` node; not needed here.
- Verify against a live broker: viewer shows tool calls/results as agents run.

### Phase 3 — `ck run` boilerplate cut (GATED)

- **Verify first:** can `ck run deploy.agent:agent …` pass through the app's dynamic config
  (`--from-config` / `--strategy` / `--model-id` / `--reasoning-effort`) to build the `Agent`?
  Expose the configured `Agent` as a module attribute / factory.
- **If it fits:** delete the `Client.connect` + `Worker` + `worker.run()` wiring (~30–50 LOC).
- **If it doesn't:** keep the argparse front-end, but still adopt `async with client:` /
  `aclose()` for the lifecycle. No regression either way — decide from evidence.

### Phase 4 — Docs + ADRs

- Update `README.md`, `docs/CLI_REFERENCE.md` (viewer topology note; any `ck` references).
- Write two ADRs (per `.agents/skills/grill-with-docs/ADR-FORMAT.md`):
  - **ADR: shared-topic broadcast via the `topic=` escape hatch** (rejecting per-agent private
    topics — preserves the fan-out).
  - **ADR: activity-viewer observation via a shared durable inbox + `events()`** (rejecting the
    `@consumer` and merge-into-connector alternatives).
- Opportunistically harden the connector `_publish_latest` loop (`REVIEW_FINDINGS` R1: the
  periodic loop dies silently on the first broker error) while that call site is being rewritten.

---

## 7. Testing strategy

- **Unit suite** `uv run pytest -q -m "not llm and not broker"` — green at every step.
- **Broker-integration suite** `uv run pytest --run-broker` (testcontainers/Redpanda) — the
  wire-level net; must pass after Phases 1 and 2 because the envelope format changed.
- **`/pytest-coverage`** on changed files; keep the `arena`/`config` coverage badge scope green.
- **Manual smoke:** one connector + one `deploy.agent` + `tools_and_dashboard` + `response_viewer`
  against a local broker — confirm a tick becomes a trade and the viewer shows activity.

## 8. Rollout

Single atomic cutover (wire-incompatible). Build all five process types from 0.12.6 and deploy
together — connector, every agent, tools-&-dashboard, and the viewer. No canary, no mixed fleet.
The dependency bump is already committed to the working tree.

## 9. Risk register

| Risk | Mitigation |
|------|------------|
| Mixed-version fleet → envelope-incompatible silent failures | Lockstep deploy; document in rollout runbook. |
| Naive `agent(name)` breaks broadcast fan-out | Centralized `AGENT_INPUT_TOPIC` + `agent(topic=…)`; guarded by `test_connector_invoke_fans_out_to_all_agent_groups` (broker test). |
| `invoked_at` latency dep silently becomes `None` if only one side migrates | Read + write change land in the same PR; keep the existing `None`-guard; add an assertion. |
| `events()` best-effort drop under load | Acceptable for a live view; the viewer surfaces the `.dropped` counter in its header; `@consumer` if durable capture is later needed. |
| `ck run` doesn't fit dynamic config | Gated; fall back to argparse + lifecycle cleanup only. |

## 10. Decisions to capture as ADRs (Phase 4)

1. Broadcast fan-out preserved via the `topic=` escape hatch (not per-agent private topics).
2. Activity-viewer observation via a shared durable inbox + `client.events()`.

---

## Appendix A — API mapping (old → new), quick reference

| Concern | 0.2.1 | 0.12.6 |
|---------|-------|--------|
| Fire-and-forget agent invoke | `client.invoke_node(user_prompt=P, topic=T, deps=D)` | `client.agent(topic=T).send(P, deps=D)` → `Dispatch` |
| Request/reply invoke | `client.execute_node(user_prompt=P, topic=T, timeout=…)` | `client.agent(topic=T).execute(P, timeout=…)` → `InvocationResult` |
| Get a run handle | — | `client.agent(topic=T).start(P)` → `InvocationHandle` |
| Client shutdown | `await client.close()` | `await client.aclose()` / `async with client:` |
| Start broker for raw publish | `await client.broker.start()` | unchanged (or lazy via first `send()`) |
| Read a producer-supplied dep | `ctx.deps.provided_deps["k"]` | `ctx.deps.get("k")` |
| Observe agent turns | subscribe `agent_router.output`, walk `Envelope.context.state.message_history`, vendored `pydantic_ai` parts | `async with client.events() as s: async for ev in s` → typed `RunEvent` |
| Agent id of an event | `envelope.internal_workflow_state.current_frame.callback_topic.split(".")[0]` | `event.emitter` |
| Message parts | `calfkit._vendor.pydantic_ai.messages.*` | `calfkit.models.payload.{TextPart,ToolCallPart,DataPart,FilePart}` |
| Raw price side-channel | `client.broker.publish` / `@client.broker.subscriber` | unchanged (plain `TickerMessage`, not envelopes) |
