# Broadcast fan-out to agents via the `topic=` escape hatch

**Status:** accepted

## Context & decision

Every trading agent must see every market tick. We achieve this by having all agents
subscribe to one shared topic, `agent_router.input`, each under its own consumer group
(`group_id=<agent-name>`), so Kafka delivers every published tick to every agent
(group-per-agent fan-out). The exchange connector therefore invokes agents with
`client.agent(topic="agent_router.input").send(...)` — calfkit's **`topic=` escape
hatch** — rather than the idiomatic `client.agent(<name>)`.

## Why not the idiomatic `client.agent(<name>)`

calfkit 0.12's `client.agent(<name>)` derives a *per-agent private* input topic
(`agent.<name>.private.input`). Addressing agents by name would deliver each tick to exactly
**one** agent, silently collapsing the broadcast the arena is built on. The `topic=` form is
a deliberate, load-bearing deviation — do not "simplify" it to name addressing. The topic
name is a cross-process wire contract, centralized as `AGENT_INPUT_TOPIC` in
`exchanges/__init__.py`, and guarded by
`tests/test_broker_integration.py::test_connector_invoke_fans_out_to_all_agent_groups` (the
connector's `send()` reaching two distinct consumer groups on the agent topic over real Kafka).
