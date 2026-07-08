# Agent-activity observation via a shared durable inbox + `client.events()`

**Status:** accepted

## Context & decision

The standalone activity viewer (`deploy/response_viewer.py`) shows every agent's tool calls,
responses, and tool results live. In calfkit 0.12 an agent's per-turn step events route to the
**invoker's** inbox topic (its `callback_topic`), not a fixed topic. So the exchange connector
connects with `inbox_topic="agent_router.output"` (a shared, stable-named inbox with
best-effort/latest delivery — `AGENT_OUTPUT_TOPIC`),
and the viewer binds the same inbox and reads the typed `RunEvent` firehose via `client.events()`.
This replaced ~115 lines of hand-rolled `Envelope` traversal + vendored-`pydantic_ai` message
introspection with a typed, first-class API (and removed the codebase's only vendored-internal import).

## Considered options

- **Shared durable inbox + `events()` (chosen)** — preserves the standalone-terminal viewer and the
  `agent_router.output` topic name; best-effort (drop-oldest), which is fine for a live dashboard.
- **`@consumer` node on each agent's `publish_topic`** — durable/guaranteed delivery, but a consumer
  sees terminal output projections (`ctx.output` is `None` on intermediate hops) — a poor fit for a
  per-*step* tool-call feed.
- **Merge the viewer into the connector process** — fewest moving parts, but loses the independent
  "run it in another terminal" viewer.

## Consequences

Observation is best-effort: the `events()` firehose drops oldest events under load, so the viewer
surfaces a `⚠ N dropped` counter in its header (acceptable for a live view; the old viewer was also
lossy).

A `ToolResultEvent`'s `emitter` is the tool node (e.g. `execute_trade`), not the invoking agent — only
tool-*call* and message events carry the agent name. The viewer restores agent attribution on result
rows by correlating `tool_call_id` back to the calling agent (falling back to the tool node if the call
wasn't observed).

Because the connector binds `agent_router.output` as its inbox but never reads replies (fire-and-forget
`send()`), every agent reply landing there logs a hub "no pending handle" notice; both the connector and
the viewer quiet the `calfkit.client.hub` logger to suppress it. A cleaner fix — stamping a callback
topic without also consuming it — is a calfkit ergonomics gap worth an upstream issue.
