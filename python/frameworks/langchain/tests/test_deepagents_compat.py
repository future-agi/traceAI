"""Deep Agents compatibility test for traceai-langchain (TH-8245).

Deep Agents (``deepagents``) is a LangGraph graph built by ``create_deep_agent``.
There is no Deep Agents instrumentor; these tests check that the existing
``LangChainInstrumentor`` traces it. They drive ``examples/deep_agents.py`` (the
cookbook) with its scripted fake model: no network, no vendor call, placeholder
keys only.

Skipped when ``deepagents`` is not installed. Python >= 3.11 (deepagents' floor).

Architecture acceptance criteria covered here:

* AC-01 LLM spans carry model and usage (the fake model returns usage_metadata);
  TOOL spans exist.
* AC-02 tool span names are the tools the fixture called: built-ins
  ``write_file``, ``read_file``, ``ls``, ``task`` plus a custom tool. Never
  ``write_todos`` (not a default tool at deepagents 0.7.21).
* AC-03 the ``task`` subagent's spans are under the ``task`` TOOL span, one trace.
* AC-04 ``configurable.thread_id`` -> ``session.id`` on every span.
* AC-05 ``astream`` (consumed and cancelled) leaves no open span; a tool
  exception sets ERROR.
* AC-07 the cookbook script itself runs against the shared harness ``Receiver``
  through the real ``register()`` exporter.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import importlib.util
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pytest

pytest.importorskip("deepagents")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

HERE = Path(__file__).resolve().parent
PACKAGE_DIR = HERE.parent
PYTHON_DIR = PACKAGE_DIR.parent.parent
COOKBOOK = PACKAGE_DIR / "examples" / "deep_agents.py"

PINNED_DEEPAGENTS = "0.7.21"
# Tools create_deep_agent binds at 0.7.21 with StateBackend, read from the
# installed package (deepagents/middleware/filesystem.py _FS_TOOL_ORDER plus
# SubAgentMiddleware's `task`). `execute` is withheld because StateBackend is
# not a SandboxBackendProtocol; `delete` is bound although graph.py's docstring
# does not list it.
DEFAULT_TOOLS_AT_PIN = {"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "task"}
CALLED_TOOLS = Counter({"write_file": 1, "read_file": 1, "lookup_weather": 1, "task": 1, "ls": 1})
TOKEN_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "llm.token_count.prompt",
    "llm.token_count.completion",
    "llm.token_count.total",
)


def _load_cookbook():
    spec = importlib.util.spec_from_file_location("deep_agents_cookbook", COOKBOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cookbook = _load_cookbook()


@pytest.fixture
def traced(request):
    """Instrument with an in-memory exporter. Yields (exporter, instrumentor).

    ``@pytest.mark.parametrize("traced", [{"hide": True}], indirect=True)``
    turns on the cookbook's hide_inputs/hide_outputs config.
    """
    from fi_instrumentation import TraceConfig

    from traceai_langchain import LangChainInstrumentor

    hide = bool(getattr(request, "param", {}).get("hide", False))
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hide, hide_outputs=hide),
    )
    try:
        yield exporter, instrumentor
    finally:
        with contextlib.suppress(Exception):
            instrumentor.uninstrument()


def _attrs(span) -> Dict[str, Any]:
    return dict(span.attributes or {})


def _kind(span) -> str:
    return _attrs(span).get("gen_ai.span.kind")


def _by_kind(spans, kind: str) -> List[Any]:
    return sorted((s for s in spans if _kind(s) == kind), key=lambda s: s.start_time)


def _descendants(spans, root) -> List[Any]:
    children: Dict[int, List[Any]] = {}
    for span in spans:
        if span.parent is not None:
            children.setdefault(span.parent.span_id, []).append(span)
    found, stack = [], [root.context.span_id]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child.context.span_id)
    return found


def _assert_no_open_spans(instrumentor) -> None:
    tracer = instrumentor._tracer
    assert len(tracer._spans_by_run) == 0, "spans left open"
    assert len(tracer.run_map) == 0, "runs left in run_map"


def _run_cookbook_scenario(exporter):
    model = cookbook.build_model()
    result = cookbook.run(cookbook.build_agent(model=model))
    return model, result, exporter.get_finished_spans()


def test_tested_versions(capsys):
    from traceai_langchain.version import __version__ as traceai_langchain_version

    versions = {
        "python": "{0}.{1}.{2}".format(*sys.version_info[:3]),
        "deepagents": importlib.metadata.version("deepagents"),
        "langchain": importlib.metadata.version("langchain"),
        "langchain-core": importlib.metadata.version("langchain-core"),
        "langgraph": importlib.metadata.version("langgraph"),
        "traceai-langchain": traceai_langchain_version,
    }
    with capsys.disabled():
        print("\nDEEPAGENTS_TESTED_VERSIONS " + json.dumps(versions, sort_keys=True))
    assert versions["deepagents"] == PINNED_DEEPAGENTS


def test_bound_tools_match_the_pin_and_exclude_write_todos(traced):
    exporter, _ = traced
    model, _, _ = _run_cookbook_scenario(exporter)

    main_agent_tools = set(model.bound_tool_names[0])
    assert main_agent_tools == DEFAULT_TOOLS_AT_PIN | {"lookup_weather"}
    for names in model.bound_tool_names:
        assert "write_todos" not in names
        assert "execute" not in names  # StateBackend is not a sandbox


def test_ac01_llm_spans_have_model_and_usage_and_tool_spans_exist(traced):
    exporter, _ = traced
    _, result, spans = _run_cookbook_scenario(exporter)
    assert result["messages"][-1].content == "Paris is sunny and your notes are saved."

    llm_spans = _by_kind(spans, "LLM")
    turns = cookbook.scripted_turns()
    assert len(llm_spans) == len(turns)
    for span, turn in zip(llm_spans, turns):
        attrs = _attrs(span)
        usage = turn.usage_metadata
        assert usage, "the fake model must return usage_metadata or AC-01 is vacuous"
        assert attrs["gen_ai.request.model"] == "scripted-deep-agent-model"
        assert attrs["gen_ai.usage.input_tokens"] == usage["input_tokens"]
        assert attrs["gen_ai.usage.output_tokens"] == usage["output_tokens"]
        assert attrs["gen_ai.usage.total_tokens"] == usage["total_tokens"]
    assert _by_kind(spans, "TOOL"), "no TOOL span"


def test_ac02_tool_span_names_are_the_tools_called(traced):
    exporter, _ = traced
    _, _, spans = _run_cookbook_scenario(exporter)

    tool_spans = _by_kind(spans, "TOOL")
    assert Counter(s.name for s in tool_spans) == CALLED_TOOLS
    for span in tool_spans:
        assert _attrs(span)["gen_ai.tool.name"] == span.name
        assert span.status.status_code.name == "OK"
    assert not [s for s in spans if s.name == "write_todos"]


def test_ac03_subagent_spans_are_under_the_task_span_in_one_trace(traced):
    exporter, _ = traced
    _, _, spans = _run_cookbook_scenario(exporter)

    assert len({s.context.trace_id for s in spans}) == 1
    [root] = [s for s in spans if s.parent is None]
    assert root.name == "LangGraph"

    [task] = [s for s in spans if s.name == "task" and _kind(s) == "TOOL"]
    under_task = _descendants(spans, task)
    [subagent] = [s for s in under_task if s.name == "general-purpose"]
    assert subagent.parent.span_id == task.context.span_id
    assert _kind(subagent) == "CHAIN"

    subagent_llm = [s for s in under_task if _kind(s) == "LLM"]
    subagent_tools = [s.name for s in under_task if _kind(s) == "TOOL"]
    assert len(subagent_llm) == 2
    assert subagent_tools == ["ls"]
    for span in under_task:
        assert _attrs(span).get("lc_agent_name") == "general-purpose", span.name

    main_llm = [s for s in _by_kind(spans, "LLM") if s not in under_task]
    assert len(main_llm) == 5


def test_ac04_thread_id_becomes_session_id_on_every_span(traced):
    exporter, _ = traced
    _, _, spans = _run_cookbook_scenario(exporter)

    assert spans
    for span in spans:
        attrs = _attrs(span)
        assert attrs.get("session.id") == cookbook.THREAD_ID, span.name
        assert attrs.get("gen_ai.conversation.id") == cookbook.THREAD_ID, span.name


def test_token_counts_only_on_llm_spans_no_double_counting(traced):
    exporter, _ = traced
    _, _, spans = _run_cookbook_scenario(exporter)

    for span in spans:
        if _kind(span) != "LLM":
            leaked = [key for key in TOKEN_KEYS if key in _attrs(span)]
            assert not leaked, "{0} ({1}) carries {2}".format(span.name, _kind(span), leaked)
    total = sum(
        _attrs(s).get("gen_ai.usage.input_tokens", _attrs(s).get("llm.token_count.prompt", 0))
        for s in spans
    )
    assert total == cookbook.EXPECTED_INPUT_TOKENS


def test_ac05_astream_closes_every_span(traced):
    exporter, instrumentor = traced

    async def consume():
        agent = cookbook.build_agent()
        chunks = 0
        async for _ in agent.astream(
            {"messages": [{"role": "user", "content": "go"}]},
            config={"configurable": {"thread_id": cookbook.THREAD_ID}},
        ):
            chunks += 1
        return chunks

    assert asyncio.run(consume()) > 0
    spans = exporter.get_finished_spans()
    _assert_no_open_spans(instrumentor)
    assert len(_by_kind(spans, "LLM")) == len(cookbook.scripted_turns())
    assert Counter(s.name for s in _by_kind(spans, "TOOL")) == CALLED_TOOLS
    [root] = [s for s in spans if s.parent is None]
    assert root.status.status_code.name == "OK"
    assert {_attrs(s).get("session.id") for s in spans} == {cookbook.THREAD_ID}


def test_ac05_cancelled_astream_closes_every_span(traced, capsys):
    exporter, instrumentor = traced

    async def cancel_after_first_chunk():
        agent = cookbook.build_agent()
        stream = agent.astream(
            {"messages": [{"role": "user", "content": "go"}]},
            config={"configurable": {"thread_id": cookbook.THREAD_ID}},
        )
        async for _ in stream:
            break
        await stream.aclose()

    asyncio.run(cancel_after_first_chunk())
    spans = exporter.get_finished_spans()
    _assert_no_open_spans(instrumentor)
    [root] = [s for s in spans if s.parent is None]
    assert root.end_time is not None
    # Recorded, not asserted: LangChain reports the consumer's aclose() as a
    # chain error, so the root span ends ERROR with a GeneratorExit description.
    with capsys.disabled():
        print(
            "\nDEEPAGENTS_CANCELLED_ASTREAM spans={0} root_status={1} root_description={2!r}".format(
                len(spans), root.status.status_code.name, (root.status.description or "")[:40]
            )
        )


def test_ac05_tool_exception_sets_error(traced):
    exporter, instrumentor = traced

    @tool
    def broken_tool(city: str) -> str:
        """Always fails."""
        raise ValueError("weather service down")

    turns = [
        AIMessage(
            content="",
            tool_calls=[{"name": "broken_tool", "args": {"city": "Paris"}, "id": "call_x", "type": "tool_call"}],
            usage_metadata={"input_tokens": 5, "output_tokens": 1, "total_tokens": 6},
        ),
        AIMessage(content="unreachable", usage_metadata={"input_tokens": 6, "output_tokens": 1, "total_tokens": 7}),
    ]
    agent = cookbook.build_agent(model=cookbook.build_model(turns), tools=[broken_tool])

    # langgraph's default ToolNode handler re-raises non-validation errors.
    with pytest.raises(ValueError, match="weather service down"):
        cookbook.run(agent)

    spans = exporter.get_finished_spans()
    _assert_no_open_spans(instrumentor)
    [broken] = [s for s in spans if s.name == "broken_tool"]
    assert _kind(broken) == "TOOL"
    assert broken.status.status_code.name == "ERROR"
    assert "weather service down" in broken.status.description
    assert any(event.name == "exception" for event in broken.events)
    [root] = [s for s in spans if s.parent is None]
    assert root.status.status_code.name == "ERROR"


@pytest.mark.parametrize("traced", [{"hide": False}], indirect=True)
def test_privacy_control_file_contents_flow_when_capture_is_on(traced):
    exporter, _ = traced
    _, _, spans = _run_cookbook_scenario(exporter)
    blob = json.dumps([_attrs(s) for s in spans], default=str)
    assert cookbook.NOTE_CONTENT in blob


@pytest.mark.parametrize("traced", [{"hide": True}], indirect=True)
def test_privacy_cookbook_config_hides_file_contents(traced):
    exporter, _ = traced
    _, _, spans = _run_cookbook_scenario(exporter)
    for span in spans:
        attrs = _attrs(span)
        assert cookbook.NOTE_CONTENT not in json.dumps(attrs, default=str), span.name
        assert attrs.get("input.value", "__REDACTED__") == "__REDACTED__", span.name


# --- Shared harness contract (TH-8339 Receiver) ------------------------------


def _otlp_value(any_value: Dict[str, Any]) -> Any:
    if "stringValue" in any_value:
        return any_value["stringValue"]
    if "intValue" in any_value:
        return int(any_value["intValue"])
    if "doubleValue" in any_value:
        return float(any_value["doubleValue"])
    if "boolValue" in any_value:
        return bool(any_value["boolValue"])
    return any_value


def _otlp_attrs(span: Dict[str, Any]) -> Dict[str, Any]:
    return {item["key"]: _otlp_value(item.get("value", {})) for item in span.get("attributes", [])}


def test_harness_contract_cookbook_round_trip():
    """Run the cookbook in a subprocess; the real register() exporter posts to the Receiver."""
    sys.path.insert(0, str(PYTHON_DIR / "tests"))
    from harness import Receiver, run

    with Receiver() as receiver:
        env = dict(os.environ)
        for key in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            env.pop(key, None)
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": "placeholder-api-key",
                "FI_SECRET_KEY": "placeholder-secret-key",
                "FI_PROJECT_NAME": "deep-agents-contract",
                "PYTHONPATH": os.pathsep.join([str(PACKAGE_DIR), str(PYTHON_DIR), env.get("PYTHONPATH", "")]),
            }
        )
        result = run([sys.executable, str(COOKBOOK)], env=env, stdin=None, timeout=180)
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace")
        assert not result.timed_out, "cookbook timed out\nSTDERR:\n" + stderr[-4000:]
        assert result.returncode == 0, "cookbook failed\nSTDERR:\n" + stderr[-4000:]
        spans = receiver.spans()
        requests = receiver.requests()

    assert "FINAL_ANSWER Paris is sunny and your notes are saved." in stdout
    assert "FLUSHED True" in stdout

    # Transport: collector path, auth headers, project resource attributes.
    assert requests, "receiver got no export"
    for request in requests:
        assert request["path"] == "/tracer/v1/traces"
        assert request["headers"]["x-api-key"] == "placeholder-api-key"
        assert request["headers"]["x-secret-key"] == "placeholder-secret-key"
        assert request["resource_attributes"], "export carried no resource"
        for resource in request["resource_attributes"]:
            assert resource["project_name"] == "deep-agents-contract"
            assert resource["project_type"] == "observe"

    # Spans: kinds, model, usage, tools, session, one trace.
    assert spans, "receiver got no spans"
    flat = [(span, _otlp_attrs(span)) for span in spans]
    llm = [attrs for _, attrs in flat if attrs.get("gen_ai.span.kind") == "LLM"]
    tools = Counter(span["name"] for span, attrs in flat if attrs.get("gen_ai.span.kind") == "TOOL")
    assert len(llm) == len(cookbook.scripted_turns())
    for attrs in llm:
        assert attrs["gen_ai.request.model"] == "scripted-deep-agent-model"
        assert attrs["gen_ai.usage.input_tokens"] > 0
    assert tools == CALLED_TOOLS
    assert len({span["traceId"] for span in spans}) == 1
    for span, attrs in flat:
        assert attrs.get("session.id") == cookbook.THREAD_ID, span["name"]

    # Observe sums promoted token keys over every span of a trace (fi-collector
    # promotes them on any span kind), so only model calls may carry them.
    for span, attrs in flat:
        if attrs.get("gen_ai.span.kind") != "LLM":
            assert not [key for key in TOKEN_KEYS if key in attrs], span["name"]
    trace_input_tokens = sum(
        attrs.get("gen_ai.usage.input_tokens", attrs.get("llm.token_count.prompt", 0)) for _, attrs in flat
    )
    assert trace_input_tokens == cookbook.EXPECTED_INPUT_TOKENS

    # Cookbook default hides content: no file contents leave the process.
    for span, attrs in flat:
        assert cookbook.NOTE_CONTENT not in json.dumps(attrs), span["name"]
