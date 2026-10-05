"""In-process tests for setup(): one provider, resource/headers, and real AG2 Classic runs.

Real ``autogen`` 0.14.x agents talk to a loopback fake OpenAI server; spans go
to an in-memory exporter. Each test undoes the global ``OpenAIWrapper.create``
patch through ``AG2ClassicTracing.uninstrument()``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

pytest.importorskip("autogen.opentelemetry")

from _fake_openai import COMPLETION_TOKENS, FAKE_MODEL, PROMPT_TOKENS, FakeOpenAI  # noqa: E402
from _scenarios import (  # noqa: E402
    SECRET_PROMPT,
    TOOL_SECRET_CITY,
    broken_tool_chat,
    group_chat,
    two_agent_tool_chat,
)

from traceai_ag2_classic import CONTENT_KEYS, AG2ClassicSpanProcessor, setup  # noqa: E402

PACKAGE_DIR = Path(__file__).resolve().parent.parent
PYTHON_DIR = PACKAGE_DIR.parent.parent


@pytest.fixture
def pipeline():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    handles = []

    def make(**kwargs):
        handle = setup(tracer_provider=provider, **kwargs)
        handles.append(handle)
        return handle

    yield provider, exporter, make
    for handle in handles:
        handle.uninstrument()


@pytest.fixture
def fake():
    with FakeOpenAI(tool_arguments={"city": TOOL_SECRET_CITY}) as server:
        yield server


def _by_name(exporter):
    grouped = {}
    for span in exporter.get_finished_spans():
        grouped.setdefault(span.name, []).append(span)
    return grouped


def _exporter_of(processor):
    exporter = getattr(processor, "span_exporter", None)
    if exporter is None:
        exporter = getattr(getattr(processor, "_batch_processor", None), "_exporter", None)
    return exporter


# One provider, resource attributes and headers -----------------------------------


class _RecordingCollector:
    """Loopback HTTP server that records path, headers and body of each POST.

    The shared harness Receiver drops headers and resource attributes, so this
    unit test records them itself.
    """

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                owner.requests.append((self.path, dict(self.headers.items()), body))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def test_register_then_setup_keeps_one_provider_resource_and_headers(monkeypatch):
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    from traceai_ag2_classic import AG2_SCOPE

    collector = _RecordingCollector()
    monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")
    monkeypatch.setenv("FI_BASE_URL", collector.origin)
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    global_before = trace_api.get_tracer_provider()
    provider = register(project_type=ProjectType.OBSERVE, project_name="ag2-classic-unit", verbose=False)
    tracing = setup(tracer_provider=provider)
    try:
        processors = provider._active_span_processor._span_processors
        assert isinstance(processors[0], AG2ClassicSpanProcessor)
        exporting = [p for p in processors if _exporter_of(p) is not None]
        assert len(exporting) == 1, "setup() must not add a second exporter"
        assert type(_exporter_of(exporting[0])).__name__ == "HTTPSpanExporter"
        # setup() never set or replaced the global provider.
        assert trace_api.get_tracer_provider() is global_before
        assert tracing.autogen_version.startswith("0.14.")

        with provider.get_tracer(AG2_SCOPE).start_as_current_span("chat m") as span:
            span.set_attribute("ag2.span.type", "llm")
        assert provider.force_flush(timeout_millis=10000)

        assert len(collector.requests) == 1
        path, headers, body = collector.requests[0]
        assert path == "/tracer/v1/traces"
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered["x-api-key"] == "placeholder-api-key"
        assert lowered["x-secret-key"] == "placeholder-secret-key"
        assert "authorization" not in lowered
        assert lowered["content-type"] == "application/x-protobuf"

        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        resource = {kv.key: kv.value.string_value for kv in request.resource_spans[0].resource.attributes}
        assert resource["project_name"] == "ag2-classic-unit"
        assert resource["project_type"] == "observe"
        assert "openinference.project.name" not in resource
        exported = request.resource_spans[0].scope_spans[0].spans[0]
        attrs = {kv.key: kv.value.string_value for kv in exported.attributes}
        assert attrs["gen_ai.span.kind"] == "LLM"
    finally:
        tracing.uninstrument()
        provider.shutdown()
        collector.close()
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_add_span_processor_after_register_and_setup_keeps_our_processor_first(monkeypatch, fake):
    """fi's TracerProvider.add_span_processor shuts down and clears every
    processor, ours included (fi_instrumentation/otel.py:336-339). setup()
    re-prepends and re-enables ours so the new processor still sees filtered spans.
    """
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:9")
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="ag2-classic-add",
        verbose=False,
        set_global_tracer_provider=False,
    )
    tracing = setup(tracer_provider=provider)
    memory = InMemorySpanExporter()
    try:
        added = SimpleSpanProcessor(memory)
        provider.add_span_processor(added)
        processors = provider._active_span_processor._span_processors
        assert processors == (tracing.processor, added)
        assert tracing.processor._disabled is False

        two_agent_tool_chat(tracing, fake)
        spans = memory.get_finished_spans()
        assert any(s.name == "conversation user" for s in spans)
        _assert_no_content_and_no_promoted_usage_off_llm(spans)
        conversation = next(s for s in spans if s.name == "conversation user").attributes
        assert conversation["gen_ai.span.kind"] == "CHAIN"

        # A second add keeps exactly one copy of ours, still first.
        second = SimpleSpanProcessor(InMemorySpanExporter())
        provider.add_span_processor(second)
        assert provider._active_span_processor._span_processors == (tracing.processor, added, second)
    finally:
        tracing.uninstrument()
        provider.shutdown()
        for sig, handler in saved.items():
            signal.signal(sig, handler)


def test_add_span_processor_on_a_plain_sdk_provider_keeps_one_processor_first(pipeline):
    provider, _exporter, make = pipeline
    first = make()
    make()  # a second setup() must not stack a second guard or processor
    extra = SimpleSpanProcessor(InMemorySpanExporter())
    provider.add_span_processor(extra)
    processors = provider._active_span_processor._span_processors
    assert processors[0] is first.processor
    assert processors[-1] is extra
    assert sum(isinstance(p, AG2ClassicSpanProcessor) for p in processors) == 1


def test_setup_is_idempotent_per_provider_and_uninstrument_restores(pipeline):
    from autogen.oai.client import OpenAIWrapper

    provider, _exporter, make = pipeline
    original = OpenAIWrapper.create
    first = make()
    assert getattr(OpenAIWrapper.create, "__otel_wrapped__", False) is True
    second = make()
    ours = [p for p in provider._active_span_processor._span_processors if isinstance(p, AG2ClassicSpanProcessor)]
    assert len(ours) == 1 and first.processor is second.processor
    assert second.owns_llm_wrapper is False  # upstream wraps once (llm_wrapper.py:64-65)
    first.uninstrument()
    assert OpenAIWrapper.create is original
    # Upstream has no per-agent undo, so agents instrumented earlier keep
    # emitting spans on this provider. The processor stays installed and live
    # so those spans are still filtered; the second handle keeps working too.
    ours = [p for p in provider._active_span_processor._span_processors if isinstance(p, AG2ClassicSpanProcessor)]
    assert ours == [first.processor]
    assert provider._active_span_processor._span_processors[0] is first.processor
    assert first.processor._disabled is False


PROMOTED_USAGE_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens",
    "gen_ai.cost.total",
    "llm.cost.total",
)


def _assert_no_content_and_no_promoted_usage_off_llm(spans):
    assert spans, "no spans were exported"
    for span in spans:
        attrs = dict(span.attributes)
        for key in CONTENT_KEYS:
            assert key not in attrs, (key, span.name)
        blob = json.dumps(attrs, default=str)
        assert SECRET_PROMPT not in blob and TOOL_SECRET_CITY not in blob, span.name
        if attrs.get("gen_ai.span.kind") != "LLM":
            leaked = [k for k in attrs if k in PROMOTED_USAGE_KEYS]
            assert not leaked, (span.name, leaked)


def test_previously_instrumented_agents_stay_filtered_after_uninstrument(pipeline, fake):
    from autogen import ConversableAgent

    _provider, exporter, make = pipeline
    tracing = make()
    assistant = ConversableAgent(
        "assistant",
        llm_config=fake.llm_config(),
        human_input_mode="NEVER",
        is_termination_msg=lambda m: "TERMINATE" in str(m.get("content") or ""),
    )
    user = ConversableAgent("user", llm_config=False, human_input_mode="NEVER", max_consecutive_auto_reply=1)
    tracing.instrument_agent(assistant)
    tracing.instrument_agent(user)
    tracing.uninstrument()
    exporter.clear()

    user.initiate_chat(assistant, message=SECRET_PROMPT, max_turns=1, silent=True)
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    # Upstream patched these agents for good; their spans still arrive.
    assert {"conversation user", "invoke_agent assistant"} <= names
    _assert_no_content_and_no_promoted_usage_off_llm(spans)
    conversation = next(s for s in spans if s.name == "conversation user").attributes
    assert conversation["gen_ai.span.kind"] == "CHAIN"
    assert conversation["ag2.usage.input_tokens"] == PROMPT_TOKENS


def test_second_handle_keeps_filtering_after_first_handle_uninstruments(pipeline, fake):
    _provider, exporter, make = pipeline
    first = make()
    second = make()
    first.uninstrument()
    exporter.clear()
    two_agent_tool_chat(second, fake)
    spans = exporter.get_finished_spans()
    assert any(s.name == "conversation user" for s in spans)
    _assert_no_content_and_no_promoted_usage_off_llm(spans)


def test_setup_rejects_a_non_sdk_provider():
    # NoOpTracerProvider has no span processors: setup() refuses instead of creating a provider.
    with pytest.raises(TypeError, match="SDK TracerProvider"):
        setup(tracer_provider=trace_api.NoOpTracerProvider())


def test_setup_rolls_back_the_llm_patch_when_an_agent_fails(fake):
    from autogen import ConversableAgent
    from autogen.oai import client as oai_client_module
    from autogen.oai.client import OpenAIWrapper

    provider = TracerProvider()
    original = OpenAIWrapper.create
    good = ConversableAgent("good", llm_config=fake.llm_config(), human_input_mode="NEVER")
    try:
        # Upstream instrument_agent raises AttributeError on a non-agent.
        with pytest.raises(AttributeError):
            setup(tracer_provider=provider, agents=[good, object()])
        assert OpenAIWrapper.create is original
        assert oai_client_module.OpenAIWrapper.create is original
        # A later setup() can still own the global LLM patch.
        retry = setup(tracer_provider=provider)
        assert retry.owns_llm_wrapper is True
        retry.uninstrument()
        assert OpenAIWrapper.create is original
    finally:
        OpenAIWrapper.create = original
        oai_client_module.OpenAIWrapper.create = original


# AC-01 / AC-03 / AC-06: two-agent chat with a tool, content off --------------------


def test_two_agent_chat_kinds_usage_session_and_no_content(pipeline, fake):
    _provider, exporter, make = pipeline
    tracing = make()
    result = two_agent_tool_chat(tracing, fake)
    spans = _by_name(exporter)

    assert {"conversation user", "invoke_agent assistant", "invoke_agent user", "execute_tool get_weather"} <= set(
        spans
    )
    llm_spans = spans["chat {0}".format(FAKE_MODEL)]
    assert len(llm_spans) == 2
    for span in llm_spans:
        attrs = span.attributes
        assert attrs["gen_ai.span.kind"] == "LLM"
        assert attrs["gen_ai.request.model"] == FAKE_MODEL
        assert attrs["gen_ai.provider.name"] == "openai"
        assert attrs["gen_ai.usage.input_tokens"] == PROMPT_TOKENS
        assert attrs["gen_ai.usage.output_tokens"] == COMPLETION_TOKENS
        assert attrs["gen_ai.usage.total_tokens"] == PROMPT_TOKENS + COMPLETION_TOKENS
        assert attrs["gen_ai.cost.total"] > 0

    assert spans["invoke_agent assistant"][0].attributes["gen_ai.span.kind"] == "AGENT"
    tool = spans["execute_tool get_weather"][0]
    assert tool.attributes["gen_ai.span.kind"] == "TOOL"
    assert tool.attributes["gen_ai.tool.name"] == "get_weather"
    assert tool.status.status_code is StatusCode.UNSET

    conversation = spans["conversation user"][0].attributes
    assert conversation["gen_ai.span.kind"] == "CHAIN"
    assert conversation["session.id"] == str(result.chat_id)
    # Upstream aggregate on the conversation span (chat.py:73-82) is kept under
    # ag2.usage.* so the trace total counts each LLM call once.
    assert "gen_ai.usage.input_tokens" not in conversation
    assert conversation["ag2.usage.input_tokens"] == 2 * PROMPT_TOKENS
    promoted = sum(
        s.attributes.get("gen_ai.usage.input_tokens", 0)
        for group in spans.values()
        for s in group
    )
    assert promoted == 2 * PROMPT_TOKENS

    # LLM spans are children of the agent span, all in one trace.
    trace_ids = {s.context.trace_id for group in spans.values() for s in group}
    assert len(trace_ids) == 1

    for group in spans.values():
        for span in group:
            for key in CONTENT_KEYS:
                assert key not in span.attributes, (key, span.name)
            blob = json.dumps(dict(span.attributes), default=str)
            assert SECRET_PROMPT not in blob and TOOL_SECRET_CITY not in blob


def test_capture_content_on_keeps_and_lifts_content(pipeline, fake):
    _provider, exporter, make = pipeline
    tracing = make(capture_content=True)
    two_agent_tool_chat(tracing, fake)
    spans = _by_name(exporter)
    conversation = spans["conversation user"][0].attributes
    assert SECRET_PROMPT in conversation["input.value"]
    tool = spans["execute_tool get_weather"][0].attributes
    assert json.loads(tool["gen_ai.tool.call.arguments"]) == {"city": TOOL_SECRET_CITY}
    assert tool["output.value"] == "sunny in {0}".format(TOOL_SECRET_CITY)
    llm = spans["chat {0}".format(FAKE_MODEL)][0].attributes
    assert SECRET_PROMPT in llm["gen_ai.input.messages"]
    assert SECRET_PROMPT in llm["input.value"]


# AC-07: tool exception -> ERROR ---------------------------------------------------


def test_tool_exception_sets_error_status(pipeline, fake):
    _provider, exporter, make = pipeline
    tracing = make()
    broken_tool_chat(tracing, fake)
    tool = _by_name(exporter)["execute_tool broken_tool"][0]
    assert tool.attributes["error.type"] == "ExecutionError"
    assert tool.status.status_code is StatusCode.ERROR
    assert tool.status.description == "ExecutionError"


# AC-04: group chat, one trace id ----------------------------------------------------


def test_group_chat_is_one_trace_with_one_session(pipeline, fake):
    _provider, exporter, make = pipeline
    tracing = make()
    result = group_chat(tracing, fake)
    finished = exporter.get_finished_spans()
    names = {s.name for s in finished}
    assert {"conversation writer", "speaker_selection", "invoke_agent critic", "conversation checking_agent"} <= names
    assert len({s.context.trace_id for s in finished}) == 1

    sessions = {s.attributes.get("session.id") for s in finished} - {None}
    assert sessions == {str(result.chat_id)}
    selection = next(s for s in finished if s.name == "speaker_selection")
    assert selection.attributes["gen_ai.span.kind"] == "CHAIN"
    assert selection.attributes["ag2.speaker_selection.selected"] == "critic"


def test_user_span_around_an_inner_chat_keeps_one_session(pipeline, fake):
    """Agent-as-tool: a tool runs an inner chat inside the user's own span."""
    from autogen import ConversableAgent

    provider, exporter, make = pipeline
    tracing = make()
    user_tracer = provider.get_tracer("user.app")
    terminates = lambda m: "TERMINATE" in str(m.get("content") or "")  # noqa: E731
    expert = ConversableAgent("expert", llm_config=fake.llm_config(), human_input_mode="NEVER")
    asker = ConversableAgent("asker", llm_config=False, human_input_mode="NEVER")
    tracing.instrument_agent(expert)
    tracing.instrument_agent(asker)

    def ask_expert(city: str) -> str:
        """Ask the expert about a city."""
        with user_tracer.start_as_current_span("ask_expert work"):
            inner = asker.initiate_chat(expert, message="about " + city, max_turns=1, silent=True)
        return str(inner.summary)

    assistant = ConversableAgent(
        "assistant", llm_config=fake.llm_config(), human_input_mode="NEVER", is_termination_msg=terminates
    )
    user = ConversableAgent(
        "user", llm_config=False, human_input_mode="NEVER", max_consecutive_auto_reply=3, is_termination_msg=terminates
    )
    assistant.register_for_llm(description="Ask the expert")(ask_expert)
    user.register_for_execution()(ask_expert)
    tracing.instrument_agent(assistant)
    tracing.instrument_agent(user)
    result = user.initiate_chat(assistant, message="hi", max_turns=3, silent=True)

    finished = exporter.get_finished_spans()
    names = {s.name for s in finished}
    assert {"conversation user", "execute_tool ask_expert", "ask_expert work", "conversation asker"} <= names
    assert len({s.context.trace_id for s in finished}) == 1
    assert {s.attributes.get("session.id") for s in finished} - {None} == {str(result.chat_id)}
    inner = next(s for s in finished if s.name == "conversation asker")
    assert inner.attributes["gen_ai.conversation.id"] != str(result.chat_id)
    assert "session.id" not in inner.attributes


# Context attributes and initiate_chats sessions -----------------------------------


def test_using_attributes_sets_session_and_user_on_ag2_spans(pipeline, fake):
    from fi_instrumentation import using_attributes

    _provider, exporter, make = pipeline
    tracing = make()
    with using_attributes(session_id="my-session", user_id="u-1"):
        result = two_agent_tool_chat(tracing, fake)
    finished = exporter.get_finished_spans()
    assert {s.name for s in finished} >= {"conversation user", "chat {0}".format(FAKE_MODEL), "execute_tool get_weather"}
    for span in finished:
        assert span.attributes["session.id"] == "my-session", span.name
        assert span.attributes["user.id"] == "u-1", span.name
    conversation = next(s for s in finished if s.name == "conversation user").attributes
    assert conversation["gen_ai.conversation.id"] == str(result.chat_id)


def _initiate_chats(tracing, fake):
    from autogen import ConversableAgent

    first = ConversableAgent("first", llm_config=fake.llm_config(), human_input_mode="NEVER")
    second = ConversableAgent("second", llm_config=fake.llm_config(), human_input_mode="NEVER")
    sender = ConversableAgent("sender", llm_config=False, human_input_mode="NEVER")
    for agent in (first, second, sender):
        tracing.instrument_agent(agent)
    return sender.initiate_chats(
        [
            {"recipient": first, "message": "one", "max_turns": 1, "silent": True},
            {"recipient": second, "message": "two", "max_turns": 1, "silent": True},
        ]
    )


def test_initiate_chats_has_one_session_per_chat_by_default(pipeline, fake):
    _provider, exporter, make = pipeline
    results = _initiate_chats(make(), fake)
    finished = exporter.get_finished_spans()
    assert len({s.context.trace_id for s in finished}) == 1
    sessions = {s.attributes.get("session.id") for s in finished} - {None}
    assert sessions == {str(r.chat_id) for r in results} and len(sessions) == 2
    multi = next(s for s in finished if s.attributes.get("ag2.span.type") == "multi_conversation")
    assert "session.id" not in multi.attributes


def test_using_session_gives_initiate_chats_one_session(pipeline, fake):
    from fi_instrumentation import using_session

    _provider, exporter, make = pipeline
    tracing = make()
    with using_session("batch-1"):
        _initiate_chats(tracing, fake)
    finished = exporter.get_finished_spans()
    assert any(s.attributes.get("ag2.span.type") == "multi_conversation" for s in finished)
    assert {s.attributes.get("session.id") for s in finished} == {"batch-1"}


def test_patterns_are_passed_to_upstream_instrument_pattern(pipeline, fake):
    from autogen import ConversableAgent
    from autogen.agentchat import initiate_group_chat
    from autogen.agentchat.group.patterns import RoundRobinPattern

    _provider, exporter, make = pipeline
    alpha = ConversableAgent("alpha", llm_config=fake.llm_config(), human_input_mode="NEVER")
    beta = ConversableAgent("beta", llm_config=fake.llm_config(), human_input_mode="NEVER")
    pattern = RoundRobinPattern(initial_agent=alpha, agents=[alpha, beta])
    make(patterns=[pattern])
    assert getattr(pattern.prepare_group_chat, "__otel_wrapped__", False) is True
    result, _ctx, _last = initiate_group_chat(pattern=pattern, messages="hello pattern", max_rounds=3)
    finished = exporter.get_finished_spans()
    names = {s.name for s in finished}
    assert {"invoke_agent alpha", "invoke_agent beta", "chat {0}".format(FAKE_MODEL)} <= names
    assert len({s.context.trace_id for s in finished}) == 1
    assert {s.attributes.get("session.id") for s in finished} - {None} == {str(result.chat_id)}


# Export failure never fails the agent ---------------------------------------------


def test_export_failure_does_not_fail_the_agent(monkeypatch, fake):
    import socket

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    with socket.socket() as sock:  # a loopback port with nothing listening
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    monkeypatch.setenv("FI_BASE_URL", "http://127.0.0.1:{0}".format(closed_port))
    monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
    monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    provider = register(project_type=ProjectType.OBSERVE, project_name="ag2-classic-down", verbose=False, timeout=1)
    tracing = setup(tracer_provider=provider)
    try:
        result = two_agent_tool_chat(tracing, fake)
        assert result.chat_id
        assert "TERMINATE" in result.chat_history[-1]["content"]
        provider.force_flush(timeout_millis=3000)  # export fails; must not raise
    finally:
        tracing.uninstrument()
        provider.shutdown()
        for sig, handler in saved.items():
            signal.signal(sig, handler)


# AC-08 (partial): importing the package never imports ag2 ------------------------------


def test_import_does_not_import_ag2_or_autogen_until_setup():
    code = (
        "import sys, importlib.metadata as md\n"
        "import traceai_ag2_classic\n"
        "assert 'autogen' not in sys.modules, 'package import pulled autogen'\n"
        "traceai_ag2_classic.check_autogen_classic()\n"
        "assert 'ag2' not in sys.modules, 'an ag2 module was imported'\n"
        "try:\n"
        "    print('AG2_DIST', md.version('ag2'))\n"
        "except md.PackageNotFoundError:\n"
        "    print('AG2_DIST none')\n"
        "print('AUTOGEN_DIST', md.version('autogen'))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(PACKAGE_DIR), str(PYTHON_DIR), env.get("PYTHONPATH", "")])
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-3000:]
    lines = dict(line.split(" ", 1) for line in proc.stdout.splitlines() if line.startswith(("AG2_DIST", "AUTOGEN_DIST")))
    if lines["AUTOGEN_DIST"] == "0.14.0":
        # PyPI autogen==0.14.0 is an alias that requires ag2==0.14.0 (see _guard.py).
        assert lines["AG2_DIST"] == "0.14.0"
    else:
        assert lines["AG2_DIST"] == "none"
