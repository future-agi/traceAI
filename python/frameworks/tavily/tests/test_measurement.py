"""TH-8327 step 1: measure what Tavily already emits, before any wrapper.

D-8327 (PRD r2, architecture TH-8327): a traceAI-tavily wrapper ships only if
the bare client emits nothing. Both measurements run a script through the
shared harness (``harness.run``) against the loopback Tavily fake and export
through the real ``fi_instrumentation.register()`` to ``harness.Receiver``.
Nothing calls api.tavily.com and every key is a placeholder.

* Bare client (PRD G2 / AC-02): ``TavilyClient`` and ``AsyncTavilyClient``
  ``search`` and ``extract`` with ``register()`` only (also as the global
  provider), then with traceAI-langchain instrumented as well. Every call runs
  inside one ``measurement.control`` span, so an export that arrives proves the
  pipeline works and the Tavily span count is what is left.
* LangGraph path (PRD G1 / AC-01): the example's
  ``langchain_community.tools.tavily_search.TavilySearchResults`` run by a
  LangGraph ``ToolNode`` with traceAI-langchain on.

Measured with tavily-python 0.8.4, langchain-community 0.4.2, langchain-core
1.5.2 and langgraph 1.2.2:

* Bare client: 0 Tavily spans for 4 real HTTP calls, with or without
  traceAI-langchain. tavily-python 0.8.4 has no OpenTelemetry code.
* LangGraph path: 1 TOOL span, ``tavily_search_results_json``, with the keys
  in ``LANGCHAIN_TOOL_KEYS``. It carries the tool's result text in
  ``output.value``, and the next agent turn carries it in ``input.value``;
  only ``FI_HIDE_INPUTS`` plus ``FI_HIDE_OUTPUTS`` keeps it all in-process.

Each test prints one ``MEASUREMENT`` line (run with ``-s`` to see it).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pytest

pytest.importorskip("tavily", reason="tavily-python must be installed to measure it")
pytest.importorskip("opentelemetry.proto", reason="the harness decodes OTLP protobuf")

from _tavily_fake import CONTENT_MARKERS, TAVILY_KEY, FakeTavily  # noqa: E402
from harness import Receiver, _flatten_attributes, run  # noqa: E402

TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
PYTHON_ROOT = PACKAGE.parents[1]
LANGCHAIN_PACKAGE = PYTHON_ROOT / "frameworks" / "langchain"
MEASUREMENT = TESTS / "measurement"
FI_API_KEY = "placeholder-fi-api-key"
FI_SECRET_KEY = "placeholder-fi-secret-key"
BARE_CLIENT_CALLS = ["/search", "/extract", "/search", "/extract"]

# Attribute keys traceAI-langchain puts on the Tavily TOOL span (measured).
LANGCHAIN_TOOL_KEYS = [
    "checkpoint_ns",
    "gen_ai.span.kind",
    "gen_ai.tool.description",
    "gen_ai.tool.name",
    "input.value",
    "langgraph_checkpoint_ns",
    "langgraph_node",
    "langgraph_path",
    "langgraph_step",
    "langgraph_triggers",
    "ls_integration",
    "metadata",
    "output.mime_type",
    "output.value",
]
# Every span the LangGraph measurement exports: the graph root, two agent
# turns, two router runs, the tools node and the Tavily tool.
LANGGRAPH_SPANS = sorted(
    ["LangGraph", "agent", "agent", "route", "route", "tools", "tavily_search_results_json"]
)


def measure(
    script: str,
    args: Sequence[str] = (),
    project: str = "tavily-measurement",
    extra_env: Optional[Mapping[str, str]] = None,
) -> Tuple[Dict[str, Any], List[dict], List[dict], List[str]]:
    """Run one measurement script; return its summary, spans, exports and fake calls."""
    with FakeTavily() as fake, Receiver() as receiver:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("FI_", "TAVILY_", "OTEL_"))
        }
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": FI_API_KEY,
                "FI_SECRET_KEY": FI_SECRET_KEY,
                "TAVILY_API_KEY": TAVILY_KEY,
                "TAVILY_BASE_URL": fake.origin,
                "MEASURE_PROJECT": project,
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(PACKAGE),
                        str(LANGCHAIN_PACKAGE),
                        str(PYTHON_ROOT),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ),
            }
        )
        env.update(extra_env or {})
        result = run([sys.executable, str(MEASUREMENT / script), *args], env, None, timeout=180)
        spans = receiver.spans()
        exports = receiver.requests()
        paths = fake.paths()
    assert not result.timed_out
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    summary = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert exports
    for export in exports:
        assert export["path"] == "/tracer/v1/traces"
        for resource in export["resource_attributes"]:
            assert resource["project_name"] == project
            assert resource["project_type"] == "observe"
    return summary, spans, exports, paths


def kind(span: dict) -> Any:
    # traceAI's span-kind key (fi_instrumentation SpanAttributes.GEN_AI_SPAN_KIND).
    return _flatten_attributes(span.get("attributes", [])).get("gen_ai.span.kind")


def report(label: str, spans: List[dict], **extra: Any) -> None:
    table = [{"name": span["name"], "kind": kind(span)} for span in spans]
    print("MEASUREMENT {0} {1}".format(label, json.dumps(dict(extra, spans=table))))


def tavily_spans(spans: List[dict]) -> List[dict]:
    return [span for span in spans if span["name"] != "measurement.control"]


def test_bare_client_emits_no_span_with_register_only():
    summary, spans, _, paths = measure("bare_client.py", project="tavily-bare")
    report("bare_client register_only", spans, summary=summary, fake_paths=paths)

    # The real client made four real HTTP calls and got results back...
    assert summary["instrumented"] == []
    assert paths == BARE_CLIENT_CALLS
    assert summary["search_results"] == summary["async_search_results"] == 2
    assert summary["extract_results"] == summary["async_extract_results"] == 2
    # ...the export path works (the control span arrived)...
    assert [span["name"] for span in spans] == ["measurement.control"]
    # ...and Tavily added nothing to it. AC-02: 0.
    assert tavily_spans(spans) == []


def test_bare_client_emits_no_span_with_the_langchain_instrumentor():
    pytest.importorskip("langchain_core", reason="traceAI-langchain needs langchain-core")
    summary, spans, _, paths = measure(
        "bare_client.py", ["--with-langchain-instrumentor"], "tavily-bare-langchain"
    )
    report("bare_client langchain_instrumentor", spans, summary=summary, fake_paths=paths)

    assert summary["instrumented"] == ["traceai_langchain"]
    assert paths == BARE_CLIENT_CALLS
    assert [span["name"] for span in spans] == ["measurement.control"]
    assert tavily_spans(spans) == []


def langgraph_tool_span(spans: List[dict]) -> dict:
    tools = [span for span in spans if kind(span) == "TOOL"]
    assert [span["name"] for span in tools] == ["tavily_search_results_json"]
    return tools[0]


def test_langgraph_tool_path_emits_one_tool_span():
    pytest.importorskip("langchain_community", reason="the example's Tavily tool")
    pytest.importorskip("langgraph", reason="the example is a LangGraph graph")
    summary, spans, _, paths = measure("langgraph_tool.py", project="tavily-langgraph")
    tool = langgraph_tool_span(spans)
    values = _flatten_attributes(tool["attributes"])
    report(
        "langgraph_tool",
        spans,
        summary=summary,
        fake_paths=paths,
        tool_keys=sorted(values),
        tool_input_value=values.get("input.value"),
    )

    assert summary["tool_messages"] == 1 and summary["tool_status"] == ["success"]
    assert paths == ["/search"]
    # AC-01: one TOOL span for one tool call, with these attribute keys.
    assert sorted(values) == LANGCHAIN_TOOL_KEYS
    assert values["gen_ai.tool.name"] == "tavily_search_results_json"
    assert "th-8327 langgraph tool measurement" in values["input.value"]
    assert sorted(span["name"] for span in spans) == LANGGRAPH_SPANS
    wire = json.dumps(spans)
    assert TAVILY_KEY not in wire
    # traceAI-langchain records the tool's output, so result text is exported.
    assert [marker for marker in CONTENT_MARKERS if marker in wire]


def _content_keys(spans: List[dict]) -> List[Tuple[str, str]]:
    """(span name, attribute key) pairs whose value carries fake result text."""
    found = []
    for span in spans:
        for key, value in _flatten_attributes(span["attributes"]).items():
            if any(marker in json.dumps(value) for marker in CONTENT_MARKERS):
                found.append((span["name"], key))
    return sorted(found)


def test_langgraph_tool_path_fi_hide_outputs_masks_only_the_tool_output():
    pytest.importorskip("langchain_community", reason="the example's Tavily tool")
    pytest.importorskip("langgraph", reason="the example is a LangGraph graph")
    summary, spans, _, _ = measure(
        "langgraph_tool.py",
        project="tavily-langgraph-hide-outputs",
        extra_env={"FI_HIDE_OUTPUTS": "true"},
    )
    values = _flatten_attributes(langgraph_tool_span(spans)["attributes"])
    report("langgraph_tool FI_HIDE_OUTPUTS", spans, content_keys=_content_keys(spans))

    assert values["output.value"] == "__REDACTED__"
    # The result text is the next agent turn's input, so it still leaves the
    # process through the agent and router spans' input.value.
    assert _content_keys(spans) == [("agent", "input.value"), ("route", "input.value")]


def test_langgraph_tool_path_drops_result_text_with_inputs_and_outputs_hidden():
    pytest.importorskip("langchain_community", reason="the example's Tavily tool")
    pytest.importorskip("langgraph", reason="the example is a LangGraph graph")
    summary, spans, _, _ = measure(
        "langgraph_tool.py",
        project="tavily-langgraph-hidden",
        extra_env={"FI_HIDE_OUTPUTS": "true", "FI_HIDE_INPUTS": "true"},
    )
    values = _flatten_attributes(langgraph_tool_span(spans)["attributes"])
    report("langgraph_tool FI_HIDE_INPUTS+FI_HIDE_OUTPUTS", spans, content_keys=_content_keys(spans))

    assert values["input.value"] == values["output.value"] == "__REDACTED__"
    assert _content_keys(spans) == []
    assert sorted(span["name"] for span in spans) == LANGGRAPH_SPANS
