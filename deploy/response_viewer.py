"""Agent Activity Viewer — a standalone Rich Live dashboard that observes all agent
activity (tool calls, text responses, and tool results) as they happen, via calfkit's
typed caller-side event firehose (``client.events()``).

Agents route their per-turn step events to the *invoker's* inbox. The exchange connector
connects with ``inbox_topic="agent_router.output"`` (a shared, stable-named inbox with
best-effort/latest delivery — see docs/calfkit-0.12-migration.md, Phase 2), so this viewer —
binding the same inbox — sees every agent's activity through the typed ``RunEvent`` stream,
with no envelope internals.

Run this in a separate terminal alongside the main tools_and_dashboard to get visibility
into agent reasoning.

Example:
    uv run python -m deploy.response_viewer --bootstrap-servers <broker-url>

Prerequisites:
    - Kafka broker running
    - The exchange connector running (it publishes to inbox_topic="agent_router.output")
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table, box
from rich.text import Text

from calfkit import AgentMessageEvent, Client, RunFailed, ToolCallEvent, ToolResultEvent
from calfkit.models.payload import DataPart, FilePart, TextPart, ToolCallPart

from exchanges import AGENT_OUTPUT_TOPIC, quiet_shared_inbox_reply_log

load_dotenv()

logger = logging.getLogger(__name__)


# ── Data model ───────────────────────────────────────────────────


@dataclass
class ActivityEntry:
    timestamp: str  # HH:MM:SS
    agent_name: str
    kind: str  # "TOOL CALL", "RESPONSE", "TOOL RESULT", "RUN FAILED"
    details: str  # Formatted display string


# ── Style constants ──────────────────────────────────────────────

KIND_STYLES: dict[str, str] = {
    "TOOL CALL": "bold yellow",
    "RESPONSE": "bold green",
    "TOOL RESULT": "bold blue",
    "RUN FAILED": "bold red",
}

# Cap rows built per redraw: only a screenful is ever shown, so rendering the whole
# (unbounded) history each event would be O(n) per event for nothing.
_MAX_VISIBLE_ROWS = 200


# ── Rich Live view ───────────────────────────────────────────────


class ActivityView:
    """Builds and rerenders a Rich Live dashboard showing all agent activity."""

    def __init__(self) -> None:
        self._log: list[ActivityEntry] = []
        self._live: Live | None = None
        self.dropped: int = 0  # cumulative events dropped by the best-effort firehose

    def attach_live(self, live: Live) -> None:
        self._live = live

    def record(self, agent_name: str, kind: str, details: str) -> None:
        # Each RunEvent is emitted once by the runtime, so — unlike the old envelope
        # re-scan — no trace_id/history dedup is needed here.
        ts = datetime.now().strftime("%H:%M:%S")
        self._log.append(
            ActivityEntry(timestamp=ts, agent_name=agent_name, kind=kind, details=details)
        )
        self._rerender()

    def _rerender(self) -> None:
        if self._live is not None:
            self._live.update(self._build_layout(), refresh=True)

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body", ratio=1),
        )
        layout["header"].update(self._build_header())
        layout["body"].update(self._build_activity_log())
        return layout

    def _build_header(self) -> Panel:
        now = datetime.now().strftime("%H:%M:%S")
        count = len(self._log)
        # Surface firehose loss so a silently-incomplete feed is visible, not assumed whole.
        dropped = f"  [bold red]⚠ {self.dropped} dropped[/]" if self.dropped else ""
        return Panel(
            Text.from_markup(
                "[bold cyan]Agent Activity Viewer[/]  [bold red]●[/] "
                f"[bold green]LIVE[/]  [dim]|  {now}  |  "
                f"{count} event{'s' if count != 1 else ''}[/]" + dropped
            ),
            style="cyan",
            height=3,
        )

    def _build_activity_log(self) -> Panel:
        table = Table(expand=True, show_lines=True, show_header=True, box=box.HORIZONTALS)
        table.add_column("Time", style="dim", width=10, no_wrap=True)
        table.add_column("Agent", style="bold cyan", width=22, no_wrap=True)
        table.add_column("Type", width=13, no_wrap=True)
        table.add_column("Details", no_wrap=False)

        if not self._log:
            table.add_row("[dim italic]Waiting for agent activity...[/]", "", "", "")
        else:
            for entry in reversed(self._log[-_MAX_VISIBLE_ROWS:]):
                style = KIND_STYLES.get(entry.kind, "")
                kind_text = Text(entry.kind, style=style)
                table.add_row(entry.timestamp, entry.agent_name, kind_text, entry.details.strip())

        return Panel(
            table,
            title="[bold]Agent Activity (most recent first)[/]",
            border_style="bright_green",
        )


# ── Event formatting ─────────────────────────────────────────────


def _truncate(s: str, max_len: int) -> str:
    """Truncate a string with ellipsis if it exceeds max_len."""
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _parts_to_text(parts) -> str:
    """Render a list of payload parts (agent message / tool result) to a display string."""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            chunks.append(part.text)
        elif isinstance(part, ToolCallPart):
            chunks.append(f"{part.tool_name}({_truncate(json.dumps(part.kwargs), 80)})")
        elif isinstance(part, DataPart):
            chunks.append(_truncate(json.dumps(part.data), 200))
        elif isinstance(part, FilePart):
            chunks.append(f"<file {part.media_type}>")
    return " ".join(c for c in chunks if c)


def _format_tool_call(event: ToolCallEvent) -> str:
    """Format a tool call as tool_name(arg=val, ...). ``args`` is already parsed."""
    args = event.args
    if isinstance(args, dict) and args:
        params = ", ".join(f"{k}={_truncate(json.dumps(v), 80)}" for k, v in args.items())
        return f"{event.name}({params})"
    if isinstance(args, str) and args:
        return f"{event.name}({_truncate(args, 120)})"
    return f"{event.name}()"


def _format_tool_result(event: ToolResultEvent) -> str:
    prefix = "⚠ " if event.is_error else ""
    return f"{prefix}{event.name} → {_truncate(_parts_to_text(event.parts), 200)}"


def _format_run_failed(event: RunFailed) -> tuple[str, str]:
    """Return (agent, detail) for a failed run. RunFailed carries an ErrorReport
    (origin_node_id is the failing node), not an emitter."""
    report = event.report
    return report.origin_node_id or "unknown", f"{report.error_type}: {report.message}"


# ── CLI ──────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deploy a live agent activity viewer.",
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Kafka bootstrap servers address",
    )
    return parser.parse_args()


# ── Entrypoint ───────────────────────────────────────────────────

view = ActivityView()


async def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # This is a full-screen Rich Live dashboard; any stderr log line corrupts it. Quiet the
    # chatty broker/hub loggers — routine "Received/Processed" traffic and the hub's per-reply
    # "no pending handle" notices (this observer client holds no run handles). Genuine agent
    # faults are surfaced in-panel via the RUN FAILED row below, not on stderr.
    quiet_shared_inbox_reply_log(logging.CRITICAL)
    for _noisy in ("faststream", "aiokafka"):
        logging.getLogger(_noisy).setLevel(logging.ERROR)

    print("=" * 50)
    print("Agent Activity Viewer")
    print("=" * 50)

    print(f"\nConnecting to Kafka broker at {args.bootstrap_servers}...")
    # Bind the connector's shared invoker inbox so every agent's per-turn step events
    # land here; client.events() is the typed, best-effort firehose over that inbox.
    client = Client.connect(args.bootstrap_servers, inbox_topic=AGENT_OUTPUT_TOPIC)

    print(f"\nObserving agent activity on {AGENT_OUTPUT_TOPIC} (typed event firehose)...")

    # A ToolResultEvent's emitter is the tool node, not the invoking agent; map each
    # tool_call_id back to the agent from its ToolCallEvent, popped when the result arrives.
    # (A firehose-dropped result would orphan its entry — acceptable for a live dashboard.)
    pending_agent: dict[str, str] = {}

    with Live(view._build_layout(), auto_refresh=False, screen=True) as live:
        view.attach_live(live)
        async with client.events() as stream:
            async for event in stream:
                view.dropped = stream.dropped
                try:
                    match event:
                        case ToolCallEvent():
                            pending_agent[event.tool_call_id] = event.emitter
                            view.record(event.emitter, "TOOL CALL", _format_tool_call(event))
                        case ToolResultEvent():
                            agent = pending_agent.pop(event.tool_call_id, event.emitter)
                            view.record(agent, "TOOL RESULT", _format_tool_result(event))
                        case AgentMessageEvent():
                            text = _parts_to_text(event.parts).strip()
                            if text:
                                view.record(event.emitter, "RESPONSE", text)
                        case RunFailed():
                            agent, detail = _format_run_failed(event)
                            view.record(agent, "RUN FAILED", detail)
                        # RunCompleted / HandoffEvent are not part of the activity feed.
                except Exception:
                    # One malformed/edge-case event must never kill the whole dashboard.
                    logger.exception("Skipped an unrenderable %s event", type(event).__name__)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nActivity viewer stopped.")
