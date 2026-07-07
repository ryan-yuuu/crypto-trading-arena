"""Unit tests for deploy.response_viewer's pure event formatters.

The viewer renders calfkit RunEvents; these lock the formatting helpers against the 0.12
event/payload schemas, so a future SDK field rename fails here (a cheap unit test) rather
than at runtime on a live dashboard.
"""

from __future__ import annotations

from calfkit import RunFailed, ToolCallEvent, ToolResultEvent
from calfkit.models.error_report import ErrorReport
from calfkit.models.payload import DataPart, FilePart, TextPart, ToolCallPart

from deploy.response_viewer import (
    _format_run_failed,
    _format_tool_call,
    _format_tool_result,
    _parts_to_text,
)


def _tool_call(args) -> ToolCallEvent:
    return ToolCallEvent(
        correlation_id="c", depth=0, frame_id="f", emitter="momentum",
        tool_call_id="t1", name="execute_trade", args=args,
    )


def _tool_result(parts, is_error: bool = False) -> ToolResultEvent:
    return ToolResultEvent(
        correlation_id="c", depth=0, frame_id="f", emitter="execute_trade",
        tool_call_id="t1", name="execute_trade", parts=parts, is_error=is_error,
    )


def _run_failed(origin_node_id: str | None) -> RunFailed:
    return RunFailed(
        report=ErrorReport(
            report_id="r", error_type="calf.tool.error", message="boom",
            retryable=False, origin_node_id=origin_node_id,
        ),
        correlation_id="c",
    )


# ── _format_tool_call ────────────────────────────────────────────


def test_format_tool_call_dict_args():
    out = _format_tool_call(_tool_call({"product_id": "BTC-USD", "quantity": 0.1}))
    assert out.startswith("execute_trade(")
    assert 'product_id="BTC-USD"' in out  # json.dumps keeps the quotes
    assert "quantity=0.1" in out


def test_format_tool_call_string_args():
    out = _format_tool_call(_tool_call('{"expression": "2+2"}'))
    assert out.startswith("execute_trade(") and "expression" in out


def test_format_tool_call_no_args():
    assert _format_tool_call(_tool_call(None)) == "execute_trade()"
    assert _format_tool_call(_tool_call({})) == "execute_trade()"


# ── _format_tool_result ──────────────────────────────────────────


def test_format_tool_result_success():
    assert _format_tool_result(_tool_result([TextPart(text="Bought 0.1 BTC-USD")])) == (
        "execute_trade → Bought 0.1 BTC-USD"
    )


def test_format_tool_result_error_is_prefixed():
    out = _format_tool_result(_tool_result([TextPart(text="No live price")], is_error=True))
    assert out.startswith("⚠ execute_trade → ")


# ── _parts_to_text (one branch per payload part type) ────────────


def test_parts_to_text_covers_each_part_type():
    assert _parts_to_text([TextPart(text="hi")]) == "hi"
    assert "calculator(" in _parts_to_text(
        [ToolCallPart(tool_call_id="t", tool_name="calculator", kwargs={"expression": "2+2"})]
    )
    assert '"k"' in _parts_to_text([DataPart(data={"k": 1})])
    assert "image/png" in _parts_to_text([FilePart(media_type="image/png")])
    # Multiple parts join on a space; empty renders are dropped.
    assert _parts_to_text([TextPart(text="a"), TextPart(text="b")]) == "a b"


# ── _format_run_failed ───────────────────────────────────────────


def test_format_run_failed_uses_origin_node_and_message():
    agent, detail = _format_run_failed(_run_failed("momentum"))
    assert agent == "momentum"
    assert detail == "calf.tool.error: boom"


def test_format_run_failed_unknown_when_no_origin_node():
    agent, _ = _format_run_failed(_run_failed(None))
    assert agent == "unknown"
