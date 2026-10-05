"""examples/agent_with_tools.py runs end to end through the shared harness.

harness.run starts ``_example_app.py`` as a subprocess with FI_BASE_URL
pointed at harness.Receiver. The example's own code runs: register(), an
MCPClient on the stdio calculator server next to it, and an MCPAgent with
FutureAGICallback. Only the LLM is replaced by the fake. The process exits
without flushing explicitly; the spans it exported prove the flush at exit
(PRD J7).

The same run with the existing transport package traceai_mcp instrumented
first gives the same spans: traceai_mcp 0.1.2 records no spans of its own
(it only carries trace context in MCP request metadata), so the agent span
is there and no tool span is duplicated under the same name and parent.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest
from _mcp_use_support import AGENT, LLM_KEY, LLM_SPAN, MODEL

pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes, run  # noqa: E402

APP = Path(__file__).resolve().parent / "_example_app.py"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"


def _run_example(*flags: str) -> Tuple[Any, List[Dict[str, Any]], List[Dict[str, Any]]]:
    with Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "OTEL_", "LANGFUSE_", "LAMINAR_", "MCP_USE_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                # mcp-use's own usage telemetry posts to PostHog and Scarf.
                "MCP_USE_ANONYMIZED_TELEMETRY": "false",
                # The subprocess imports exactly what this test process imports.
                "PYTHONPATH": os.pathsep.join(sys.path),
            }
        )
        result = run([sys.executable, str(APP), *flags], env, None, timeout=600)
        spans = receiver.spans()
        exports = receiver.requests()
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-4000:]
    return result, spans, exports


def _shape(spans: List[Dict[str, Any]]) -> List[Tuple[str, str, bool]]:
    """(name, kind, parented to the agent span) in start order."""
    (agent,) = [span for span in spans if span["name"] == AGENT]
    ordered = sorted(spans, key=lambda span: int(span["startTimeUnixNano"]))
    return [
        (
            span["name"],
            _flatten_attributes(span["attributes"])["gen_ai.span.kind"],
            span.get("parentSpanId") == agent["spanId"],
        )
        for span in ordered
    ]


def _check(result: Any, spans: List[Dict[str, Any]], exports: List[Dict[str, Any]]) -> None:
    assert b"answer: EXAMPLE-ANSWER 2 + 3 = 5" in result.stdout
    assert _shape(spans) == [
        (AGENT, "AGENT", False),
        (LLM_SPAN, "LLM", True),
        ("execute_tool add", "TOOL", True),
        (LLM_SPAN, "LLM", True),
    ]
    assert len({span["traceId"] for span in spans}) == 1
    assert all(span["status"]["code"] == "STATUS_CODE_OK" for span in spans)
    tool = next(span for span in spans if span["name"] == "execute_tool add")
    assert _flatten_attributes(tool["attributes"])["gen_ai.tool.name"] == "add"
    llm = next(span for span in spans if span["name"] == LLM_SPAN)
    assert _flatten_attributes(llm["attributes"])["gen_ai.request.model"] == MODEL

    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        assert export["headers"]["x-api-key"] == FI_API_KEY
        assert export["headers"]["x-secret-key"] == FI_SECRET_KEY
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == "mcp-use-agent"
            assert resource["project_type"] == "observe"

    wire = json.dumps(spans)
    for text in (FI_API_KEY, FI_SECRET_KEY, LLM_KEY, "What is 2 + 3", "EXAMPLE-ANSWER"):
        assert text not in wire, text


def test_example_exports_one_agent_tree_at_exit():
    _check(*_run_example())


def test_with_traceai_mcp_the_agent_span_is_there_and_no_tool_span_is_duplicated():
    result, spans, exports = _run_example("--with-traceai-mcp")
    assert b"traceai_mcp wraps stdio_client: FunctionWrapper" in result.stdout
    _check(result, spans, exports)
    by_name_and_parent = Counter((span["name"], span.get("parentSpanId")) for span in spans)
    assert by_name_and_parent[("execute_tool add", next(s["spanId"] for s in spans if s["name"] == AGENT))] == 1
    assert [name for (name, _), count in by_name_and_parent.items() if count > 1] == [LLM_SPAN]
