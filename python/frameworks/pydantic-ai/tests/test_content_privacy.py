"""T5-PY-002: fake Pydantic operations, real SDK capture/export boundaries.

No model requests or installed Pydantic AI are needed. Assert exported records,
including exception events and status descriptions, rather than display fields.
"""

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fi_instrumentation import TraceConfig, suppress_tracing
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_pydantic_ai import (
    PydanticAIInstrumentor, instrument_pydantic_ai, wrap_tool_function,
)
from traceai_pydantic_ai._attributes import PydanticAIAttributes as A


INPUT = "PRIVATE_INPUT_MARKER"
INSTRUCTIONS = "PRIVATE_INSTRUCTIONS_MARKER"
OUTPUT = "PRIVATE_OUTPUT_MARKER"
ERROR = "PRIVATE_ERROR_MARKER"


class CaptureStarts(SpanProcessor):
    def __init__(self):
        self.attributes = []

    def on_start(self, span, parent_context=None):
        self.attributes.append(dict(span.attributes))

    def on_end(self, span):
        pass

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=30000):
        return True


@pytest.fixture
def traced(monkeypatch):
    for name in (
        "HIDE_INPUTS", "HIDE_OUTPUTS", "HIDE_INPUT_MESSAGES", "HIDE_OUTPUT_MESSAGES",
        "HIDE_INPUT_TEXT", "HIDE_OUTPUT_TEXT", "HIDE_INPUT_IMAGES", "HIDE_ERROR_DETAILS",
        "HIDE_LLM_INVOCATION_PARAMETERS", "HIDE_EMBEDDING_VECTORS", "PII_REDACTION",
        "BASE64_IMAGE_MAX_LENGTH",
    ):
        monkeypatch.delenv("FI_" + name, raising=False)

    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeAgent:
        name = "privacy-agent"
        model = "openai:test-model"
        instructions = INSTRUCTIONS
        output_type = str
        builtin_calls = []

        def __init__(self):
            self.error = None
            self.received = None
            self.result = SimpleNamespace(
                output=OUTPUT,
                usage=SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5),
            )

        async def run(self, user_prompt, **kwargs):
            self.received = (user_prompt, kwargs)
            if self.error is not None:
                raise self.error
            return self.result

        def run_sync(self, user_prompt, **kwargs):
            return asyncio.run(self.run(user_prompt, **kwargs))

        async def run_stream(self, user_prompt, **kwargs):
            self.received = (user_prompt, kwargs)
            if self.error is not None:
                raise self.error
            return FakeStream()

        @classmethod
        def instrument_all(cls, *args, **kwargs):
            cls.builtin_calls.append((args, kwargs))

    module = ModuleType("pydantic_ai")
    module.Agent = FakeAgent
    module.usage = SimpleNamespace(RequestUsage=object)
    monkeypatch.setitem(sys.modules, "pydantic_ai", module)
    monkeypatch.setattr(PydanticAIInstrumentor, "_instance", None)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "_is_instrumented_by_opentelemetry", False)
    monkeypatch.setattr(PydanticAIInstrumentor, "instrumentation_dependencies", lambda self: ())

    exporter = InMemorySpanExporter()
    starts = CaptureStarts()
    provider = TracerProvider()
    provider.add_span_processor(starts)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = PydanticAIInstrumentor()

    def start(stream_method=None, convenience=False, **kwargs):
        if stream_method is not None:
            FakeAgent.run_stream = stream_method
        if convenience:
            assert instrument_pydantic_ai(tracer_provider=provider, **kwargs) is instrumentor
        else:
            instrumentor.instrument(tracer_provider=provider, **kwargs)
        return SimpleNamespace(
            agent=FakeAgent(), exporter=exporter, starts=starts, instrumentor=instrumentor,
        )

    yield start
    instrumentor.uninstrument()
    provider.shutdown()


def run_agent(state, method):
    options = {"message_history": ["harmless history"]}
    if method == "run_sync":
        result = state.agent.run_sync(INPUT, **options)
    else:
        result = asyncio.run(state.agent.run(INPUT, **options))
    assert result is state.agent.result
    assert state.agent.received == (INPUT, options)
    return state.exporter.get_finished_spans()


def captured(state):
    return repr(state.starts.attributes) + "\n" + "\n".join(
        span.to_json() for span in state.exporter.get_finished_spans()
    )


@pytest.mark.parametrize("method", ["run_sync", "run"])
@pytest.mark.parametrize("source", ["config", "environment"])
@pytest.mark.parametrize("flag", [
    "hide_inputs", "hide_input_messages", "hide_input_text",
    "hide_outputs", "hide_output_messages", "hide_output_text",
])
def test_directional_privacy_reaches_exporter(traced, monkeypatch, method, source, flag):
    if source == "environment":
        monkeypatch.setenv("FI_" + flag.upper(), "true")
        state = traced()
    else:
        state = traced(config=TraceConfig(**{flag: True}))
    spans = run_agent(state, method)
    assert len(spans) == 1
    assert spans[0].attributes[A.MODEL_NAME] == "openai:test-model"
    assert spans[0].attributes[A.USAGE_TOTAL_TOKENS] == 5
    text = captured(state)
    if flag.startswith("hide_input"):
        assert INPUT not in text
        assert INSTRUCTIONS not in text
        assert OUTPUT in text
    else:
        assert INPUT in text
        assert INSTRUCTIONS in text
        assert OUTPUT not in text


@pytest.mark.parametrize("method", ["run_sync", "run"])
def test_explicit_false_overrides_environment_without_changing_results(traced, monkeypatch, method):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    state = traced(config=TraceConfig(hide_inputs=False, hide_outputs=False))
    assert len(run_agent(state, method)) == 1
    for marker in (INPUT, INSTRUCTIONS, OUTPUT):
        assert marker in captured(state)


@pytest.mark.parametrize("method", ["run_sync", "run"])
@pytest.mark.parametrize("error_details", [None, False, True])
def test_exception_policy_covers_aliases_events_and_status(traced, method, error_details):
    state = traced(config=TraceConfig(
        hide_inputs=True, hide_outputs=True, hide_error_details=error_details,
    ))
    error = ValueError(ERROR)
    state.agent.error = error
    with pytest.raises(ValueError) as caught:
        run_agent(state, method)
    assert caught.value is error
    span, = state.exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.events
    assert span.attributes[A.ERROR_TYPE] == "ValueError"
    if error_details is False:
        assert ERROR in captured(state)
    else:
        assert ERROR not in captured(state)
        assert span.status.description is None
        assert all("exception.message" not in event.attributes for event in span.events)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_tool_aliases_and_errors_obey_policy(traced, asynchronous, failure):
    state = traced(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    error = ValueError(ERROR)

    def tool(context, value):
        assert value == INPUT
        if failure:
            raise error
        return OUTPUT

    async def async_tool(context, value):
        return tool(context, value)

    wrapped = wrap_tool_function(
        async_tool if asynchronous else tool,
        state.instrumentor._tracer,
        "local_tool",
        tool_description=INSTRUCTIONS,
    )

    def call():
        result = wrapped(None, value=INPUT)
        return asyncio.run(result) if asynchronous else result

    if failure:
        with pytest.raises(ValueError) as caught:
            call()
        assert caught.value is error
    else:
        assert call() == OUTPUT
    assert len(state.exporter.get_finished_spans()) == 1
    for marker in (INPUT, INSTRUCTIONS, OUTPUT, ERROR):
        assert marker not in captured(state)


@pytest.mark.parametrize("failure", ["none", "start", "body"])
def test_stream_prompt_and_errors_obey_policy(traced, failure):
    state = traced(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    error = ValueError(ERROR)
    if failure == "start":
        state.agent.error = error

    async def consume():
        async with await state.agent.run_stream(INPUT):
            if failure == "body":
                raise error

    if failure == "none":
        asyncio.run(consume())
    else:
        with pytest.raises(ValueError) as caught:
            asyncio.run(consume())
        assert caught.value is error
    assert len(state.exporter.get_finished_spans()) == 1
    for marker in (INPUT, OUTPUT, ERROR):
        assert marker not in captured(state)


def test_suppression_skips_export_but_not_application(traced):
    state = traced(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    with suppress_tracing():
        assert run_agent(state, "run_sync") == ()
        assert run_agent(state, "run") == ()
    assert state.starts.attributes == []
    assert len(run_agent(state, "run_sync")) == 1


def test_suppression_covers_tools_and_streams(traced):
    state = traced()
    tool = wrap_tool_function(lambda: OUTPUT, state.instrumentor._tracer, "local_tool")

    async def consume():
        async with await state.agent.run_stream(INPUT):
            pass

    with suppress_tracing():
        assert tool() == OUTPUT
        asyncio.run(consume())
    assert state.exporter.get_finished_spans() == ()
    assert state.starts.attributes == []


def test_convenience_function_forwards_config(traced):
    state = traced(convenience=True, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    assert len(run_agent(state, "run_sync")) == 1
    for marker in (INPUT, INSTRUCTIONS, OUTPUT):
        assert marker not in captured(state)


def test_builtin_option_cannot_bypass_content_policy(traced):
    state = traced(use_builtin=True, config=TraceConfig(hide_inputs=True, hide_outputs=True))
    assert len(run_agent(state, "run_sync")) == 1
    for marker in (INPUT, INSTRUCTIONS, OUTPUT):
        assert marker not in captured(state)
    assert state.agent.builtin_calls == []


@pytest.mark.parametrize("use_builtin", [False, True])
@pytest.mark.parametrize("failure", ["none", "enter", "exit", "body", "suppressed"])
def test_native_stream_protocol_keeps_cleanup_and_exception_behavior(traced, use_builtin, failure):
    from contextlib import asynccontextmanager

    error = ValueError(ERROR)
    closed = []

    @asynccontextmanager
    async def native_stream(self, user_prompt):
        assert user_prompt == INPUT
        if failure == "enter":
            raise error
        try:
            yield SimpleNamespace(content=OUTPUT)
        except ValueError:
            if failure != "suppressed":
                raise
        finally:
            closed.append(True)
        if failure == "exit":
            raise error

    state = traced(
        stream_method=native_stream, use_builtin=use_builtin,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )

    async def consume():
        async with state.agent.run_stream(INPUT) as result:
            assert result.content == OUTPUT
            if failure in ("body", "suppressed"):
                raise error

    if failure in ("enter", "exit", "body"):
        with pytest.raises(ValueError) as caught:
            asyncio.run(consume())
        assert caught.value is error
    else:
        asyncio.run(consume())
    assert closed == ([] if failure == "enter" else [True])
    assert len(state.exporter.get_finished_spans()) == 1
    for marker in (INPUT, OUTPUT, ERROR):
        assert marker not in captured(state)


def test_alias_policy_applies_at_start_and_late_updates(traced):
    state = traced(config=TraceConfig(hide_inputs=True, hide_outputs=True))
    private = {
        A.RUN_PROMPT: INPUT,
        A.AGENT_INSTRUCTIONS: INSTRUCTIONS,
        A.RUN_RESULT: OUTPUT,
        A.TOOL_ARGS: INPUT,
        A.GEN_AI_TOOL_DESCRIPTION: INSTRUCTIONS,
        A.TOOL_RESULT: OUTPUT,
        A.MESSAGE_CONTENT: OUTPUT,
        A.MESSAGE_TOOL_CALLS: INPUT,
        A.ERROR_MESSAGE: ERROR,
        A.TOOL_ERROR_MESSAGE: ERROR,
        A.RETRY_REASON: ERROR,
        A.VALIDATION_ERROR: ERROR,
    }
    with state.instrumentor._tracer.start_as_current_span("aliases", attributes=private) as span:
        span.set_attributes(private)
        span.add_event("aliases", attributes=private)
    assert len(state.exporter.get_finished_spans()) == 1
    for marker in (INPUT, INSTRUCTIONS, OUTPUT, ERROR):
        assert marker not in captured(state)
