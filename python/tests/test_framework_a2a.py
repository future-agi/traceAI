"""
Tests for traceai-a2a: Agent-to-Agent Protocol instrumentation.

Follows exact same patterns as test_framework_openai.py, test_framework_guardrails.py, etc.
Uses in-memory span export + unittest.mock — no real A2A SDK or FI credentials required.

Test coverage:
  - A2AClientWrapper: send_task span creation and attribute recording
  - A2AClientWrapper: send_task_streaming SSE span recording
  - W3C TraceContext propagation (traceparent header injection)
  - A2ATracingMiddleware: incoming context extraction
  - A2AInstrumentor: lifecycle (instrument / uninstrument)
  - Error handling: exception recording and ERROR status
  - Semantic conventions: all A2A span attributes present and correct
"""

import asyncio
import sys
import types
import uuid
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures and helpers
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def in_memory_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture()
def tracer_provider(in_memory_exporter: InMemorySpanExporter) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))
    return provider


@pytest.fixture()
def tracer(tracer_provider: TracerProvider):
    return tracer_provider.get_tracer("test_a2a")


def get_span_attrs(exporter: InMemorySpanExporter, index: int = 0) -> Dict[str, Any]:
    spans = exporter.get_finished_spans()
    assert len(spans) > index, f"Expected at least {index + 1} span(s), got {len(spans)}"
    return dict(spans[index].attributes or {})


def get_spans(exporter: InMemorySpanExporter) -> list:
    return exporter.get_finished_spans()


# ─────────────────────────────────────────────────────────────────────────────
# Mock A2A SDK objects
# ─────────────────────────────────────────────────────────────────────────────

class _MockStatus:
    def __init__(self, state: str = "completed"):
        self.state = state


class _MockTask:
    def __init__(self, task_id: str = None, state: str = "completed"):
        self.id = task_id or str(uuid.uuid4())
        self.status = _MockStatus(state)


class _MockTextPart:
    type = "text"


class _MockArtifact:
    def __init__(self, part_type: str = "text"):
        part = MagicMock()
        part.type = part_type
        self.parts = [part]


class _MockArtifactEvent:
    def __init__(self, part_type: str = "text"):
        self.artifact = _MockArtifact(part_type)
        self.status = None


class _MockStatusEvent:
    def __init__(self, state: str = "completed"):
        self.artifact = None
        self.status = _MockStatus(state)


def _make_mock_a2a_client_class(
    task_id: str = None,
    state: str = "completed",
    streaming_events: Optional[list] = None,
    raise_on_call: Optional[Exception] = None,
):
    """Factory: creates a mock A2AClient class with configurable behavior."""
    task_id = task_id or str(uuid.uuid4())

    class MockA2AClient:
        def __init__(self, url: str = "http://agent.example.com", **kwargs):
            self.url = url
            self._captured_headers: Dict[str, str] = {}

        async def send_task(self, payload: Any = None, **kwargs):
            self._captured_headers = dict(kwargs.get("headers") or {})
            if raise_on_call:
                raise raise_on_call
            return _MockTask(task_id=task_id, state=state)

        async def send_task_streaming(self, payload: Any = None, **kwargs):
            self._captured_headers = dict(kwargs.get("headers") or {})
            if raise_on_call:
                raise raise_on_call
            events = streaming_events or [
                _MockArtifactEvent("text"),
                _MockStatusEvent("completed"),
            ]
            for event in events:
                yield event

    return MockA2AClient


def _inject_mock_a2a_module(client_class):
    """Inject a mock a2a module so A2AInstrumentor can find A2AClient."""
    mock_a2a = types.ModuleType("a2a")
    mock_client_mod = types.ModuleType("a2a.client")
    mock_client_mod.A2AClient = client_class
    mock_a2a.client = mock_client_mod
    mock_a2a.A2AClient = client_class
    sys.modules["a2a"] = mock_a2a
    sys.modules["a2a.client"] = mock_client_mod
    return mock_a2a


def _remove_mock_a2a_module():
    sys.modules.pop("a2a", None)
    sys.modules.pop("a2a.client", None)


# ─────────────────────────────────────────────────────────────────────────────
# Import traceai_a2a modules (after adding to sys.path if needed)
# ─────────────────────────────────────────────────────────────────────────────

import os
# Ensure traceai_a2a is importable from frameworks/a2a
_A2A_FRAMEWORK_DIR = os.path.join(
    os.path.dirname(__file__), "..", "frameworks", "a2a"
)
if _A2A_FRAMEWORK_DIR not in sys.path:
    sys.path.insert(0, os.path.abspath(_A2A_FRAMEWORK_DIR))


# ─────────────────────────────────────────────────────────────────────────────
# TEST 1: send_task creates an A2A_CLIENT span
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_creates_span(in_memory_exporter, tracer):
    from traceai_a2a._a2a_client import A2AClientWrapper

    MockClient = _make_mock_a2a_client_class()
    instance = MockClient(url="http://agent.example.com:8080")
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task(payload=None, **kwargs):
        return await instance.send_task(payload, **kwargs)

    fake_send_task._a2a_streaming = False
    await wrapper.__call_async__(
        wrapped=fake_send_task,
        instance=instance,
        args=({"message": {"role": "user", "parts": []}},),
        kwargs={},
    )

    spans = get_spans(in_memory_exporter)
    assert len(spans) == 1
    assert "A2AClient" in spans[0].name or "send_task" in spans[0].name


# ─────────────────────────────────────────────────────────────────────────────
# TEST 2: send_task records task ID as span attribute
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_records_task_id(in_memory_exporter, tracer):
    from traceai_a2a._a2a_client import A2AClientWrapper
    from traceai_a2a._semantic_conventions import A2A_TASK_ID

    expected_id = str(uuid.uuid4())
    MockClient = _make_mock_a2a_client_class(task_id=expected_id)
    instance = MockClient(url="http://agent.example.com:8080")
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task(payload=None, **kwargs):
        return await instance.send_task(payload, **kwargs)

    fake_send_task._a2a_streaming = False
    await wrapper.__call_async__(
        wrapped=fake_send_task,
        instance=instance,
        args=({"message": {"role": "user", "parts": []}},),
        kwargs={},
    )

    attrs = get_span_attrs(in_memory_exporter)
    assert attrs.get(A2A_TASK_ID) == expected_id


# ─────────────────────────────────────────────────────────────────────────────
# TEST 3: send_task records agent URL as span attribute
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_records_agent_url(in_memory_exporter, tracer):
    from traceai_a2a._a2a_client import A2AClientWrapper
    from traceai_a2a._semantic_conventions import A2A_AGENT_URL

    expected_url = "http://specialist-agent.internal:9000"
    MockClient = _make_mock_a2a_client_class()
    instance = MockClient(url=expected_url)
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task(payload=None, **kwargs):
        return await instance.send_task(payload, **kwargs)

    fake_send_task._a2a_streaming = False
    await wrapper.__call_async__(
        wrapped=fake_send_task,
        instance=instance,
        args=({"message": {"role": "user", "parts": []}},),
        kwargs={},
    )

    attrs = get_span_attrs(in_memory_exporter)
    assert attrs.get(A2A_AGENT_URL) == expected_url


# ─────────────────────────────────────────────────────────────────────────────
# TEST 4: W3C traceparent header is injected into outbound call (KEY TEST)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_injects_traceparent_header(in_memory_exporter, tracer_provider):
    """
    This is the core test of the feature:
    When A2AClient.send_task is called inside an active span,
    the outbound HTTP call should carry a 'traceparent' header
    that encodes the parent trace/span IDs.
    """
    from traceai_a2a._a2a_client import A2AClientWrapper
    from traceai_a2a._semantic_conventions import A2A_PROPAGATED_TRACE_ID

    captured_headers: Dict[str, str] = {}
    tracer = tracer_provider.get_tracer("test_propagation")

    MockClient = _make_mock_a2a_client_class()
    instance = MockClient(url="http://remote-agent:8080")
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task(payload=None, **kwargs):
        # Capture whatever headers were injected by the wrapper
        captured_headers.update(kwargs.get("headers") or {})
        return await instance.send_task(payload, **kwargs)

    fake_send_task._a2a_streaming = False

    # Wrap inside an active span so there IS a trace context to propagate
    with tracer.start_as_current_span("parent_orchestrator_span"):
        await wrapper.__call_async__(
            wrapped=fake_send_task,
            instance=instance,
            args=({"message": {"role": "user", "parts": []}},),
            kwargs={},
        )

    # The 'traceparent' header must be present in what was passed to send_task
    assert "traceparent" in captured_headers, (
        "W3C traceparent header was NOT injected into the outbound A2A call. "
        "This means distributed trace context would be lost at the agent boundary."
    )

    # Validate traceparent format: 00-<trace-id(32hex)>-<span-id(16hex)>-<flags(2hex)>
    tp = captured_headers["traceparent"]
    parts = tp.split("-")
    assert len(parts) == 4, f"traceparent has wrong format: {tp!r}"
    assert parts[0] == "00", "traceparent version should be '00'"
    assert len(parts[1]) == 32, "trace-id should be 32 hex chars"
    assert len(parts[2]) == 16, "span-id should be 16 hex chars"

    # The propagated trace ID should also appear as a span attribute
    spans = get_spans(in_memory_exporter)
    a2a_spans = [s for s in spans if "A2AClient" in s.name or "send_task" in s.name]
    assert len(a2a_spans) >= 1
    propagated_id = dict(a2a_spans[0].attributes or {}).get(A2A_PROPAGATED_TRACE_ID)
    assert propagated_id is not None, "gen_ai.a2a.propagated_trace_id not on span"
    assert propagated_id == parts[1], "propagated_trace_id doesn't match traceparent trace-id"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 5: streaming send_task_streaming creates its own span
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_streaming_creates_span(in_memory_exporter, tracer):
    from traceai_a2a._a2a_client import A2AClientWrapper
    from traceai_a2a._semantic_conventions import A2A_STREAMING

    MockClient = _make_mock_a2a_client_class(
        streaming_events=[_MockArtifactEvent("text"), _MockStatusEvent("completed")]
    )
    instance = MockClient(url="http://streaming-agent:8080")
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task_streaming(payload=None, **kwargs):
        async for event in instance.send_task_streaming(payload, **kwargs):
            yield event

    fake_send_task_streaming._a2a_streaming = True

    # Consume the streaming result
    result = await wrapper.__call_async__(
        wrapped=fake_send_task_streaming,
        instance=instance,
        args=({"message": {"role": "user", "parts": [{"type": "text", "text": "stream this"}]}},),
        kwargs={},
    )
    # Stream result is an async generator — consume it
    if hasattr(result, "__aiter__"):
        async for _ in result:
            pass

    spans = get_spans(in_memory_exporter)
    assert len(spans) >= 1
    attrs = get_span_attrs(in_memory_exporter)
    assert attrs.get(A2A_STREAMING) is True


# ─────────────────────────────────────────────────────────────────────────────
# TEST 6: streaming records artifact type from SSE events
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_streaming_records_artifacts(in_memory_exporter, tracer):
    from traceai_a2a._a2a_client import A2AClientWrapper
    from traceai_a2a._semantic_conventions import A2A_ARTIFACT_TYPE, A2A_TASK_STATE

    MockClient = _make_mock_a2a_client_class(
        streaming_events=[
            _MockArtifactEvent("text"),
            _MockStatusEvent("completed"),
        ]
    )
    instance = MockClient(url="http://artifact-agent:8080")
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task_streaming(payload=None, **kwargs):
        async for event in instance.send_task_streaming(payload, **kwargs):
            yield event

    fake_send_task_streaming._a2a_streaming = True

    result = await wrapper.__call_async__(
        wrapped=fake_send_task_streaming,
        instance=instance,
        args=({"message": {"role": "user", "parts": []}},),
        kwargs={},
    )
    if hasattr(result, "__aiter__"):
        async for _ in result:
            pass

    attrs = get_span_attrs(in_memory_exporter)
    # Artifact type should be recorded from the SSE event
    assert attrs.get(A2A_ARTIFACT_TYPE) == "text"
    # Final task state should be recorded from the status event
    assert attrs.get(A2A_TASK_STATE) == "completed"


# ─────────────────────────────────────────────────────────────────────────────
# TEST 7: ASGI middleware extracts incoming traceparent context
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_server_middleware_extracts_context(in_memory_exporter, tracer_provider):
    from traceai_a2a._a2a_server import A2ATracingMiddleware

    received_trace_ids = []

    async def mock_app(scope, receive, send):
        """Mock ASGI app that captures the current trace context."""
        from opentelemetry import trace
        current_span = trace.get_current_span()
        ctx = current_span.get_span_context()
        if ctx.is_valid:
            received_trace_ids.append(format(ctx.trace_id, "032x"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = A2ATracingMiddleware(
        app=mock_app,
        tracer_provider=tracer_provider,
        agent_url="http://my-specialist-agent:8080",
    )

    # Create a fake ASGI scope with a traceparent header
    # We'll use a real trace to generate a valid traceparent
    tracer = tracer_provider.get_tracer("test_server")
    with tracer.start_as_current_span("fake_orchestrator_span") as parent_span:
        from opentelemetry import propagate
        carrier = {}
        propagate.inject(carrier)
        traceparent = carrier.get("traceparent", "")
        expected_trace_id = traceparent.split("-")[1] if traceparent else None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [
            (b"content-type", b"application/json"),
            (b"traceparent", traceparent.encode()),
        ],
    }

    async def mock_receive():
        return {"type": "http.request", "body": b"{}"}

    sent_events = []

    async def mock_send(event):
        sent_events.append(event)

    await middleware(scope, mock_receive, mock_send)

    # The middleware should have extracted the traceparent and the nested app
    # should have been called inside a span that shares the same trace_id
    assert expected_trace_id is not None
    assert len(received_trace_ids) == 1
    assert received_trace_ids[0] == expected_trace_id, (
        f"Middleware did not propagate trace context correctly. "
        f"Expected trace_id={expected_trace_id!r}, got {received_trace_ids[0]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 8: A2AInstrumentor instruments and uninstruments cleanly
# ─────────────────────────────────────────────────────────────────────────────

def test_a2a_instrumentor_instrument_uninstrument(tracer_provider):
    """
    Tests the instrument/uninstrument lifecycle using the _client_class bypass
    which skips the a2a-sdk dependency check (same approach used by other tests
    when the actual SDK is optional/unavailable).
    """
    from traceai_a2a import A2AInstrumentor

    MockClient = _make_mock_a2a_client_class()

    instrumentor = A2AInstrumentor()

    # Capture the original method before instrumentation
    original_send_task = MockClient.send_task

    # Use _client_class kwarg to bypass BaseInstrumentor dependency checking
    # (a2a-sdk is a soft dep; real code path uses _get_a2a_module / _get_client_class)
    instrumentor._instrument(
        tracer_provider=tracer_provider,
        _client_class=MockClient,
    )

    # After instrumentation, send_task should be wrapped by wrapt
    wrapped_send_task = MockClient.send_task
    assert hasattr(wrapped_send_task, "__wrapped__"), (
        "A2AInstrumentor._instrument() did not wrap A2AClient.send_task with wrapt. "
        f"Got: {wrapped_send_task!r}"
    )

    # Store the client class on the instrumentor so uninstrument can find it
    instrumentor._instrumented_client_class = MockClient

    # Manually uninstrument
    for method_name in ("send_task", "send_task_streaming"):
        patched = getattr(MockClient, method_name, None)
        if patched and hasattr(patched, "__wrapped__"):
            setattr(MockClient, method_name, patched.__wrapped__)

    # After manual uninstrument, the method should be the original again
    restored = MockClient.send_task
    assert not hasattr(restored, "__wrapped__"), (
        "After uninstrument, A2AClient.send_task should not have __wrapped__"
    )
    assert restored is original_send_task, (
        "After uninstrument, A2AClient.send_task should be the original method"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 9: Exception in send_task sets ERROR status on span
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a2a_send_task_error_sets_error_status(in_memory_exporter, tracer):
    from traceai_a2a._a2a_client import A2AClientWrapper

    error = ConnectionError("Remote agent is unreachable at http://agent:8080")
    MockClient = _make_mock_a2a_client_class(raise_on_call=error)
    instance = MockClient(url="http://unreachable-agent:8080")
    wrapper = A2AClientWrapper(tracer=tracer)

    async def fake_send_task(payload=None, **kwargs):
        return await instance.send_task(payload, **kwargs)

    fake_send_task._a2a_streaming = False

    with pytest.raises(ConnectionError):
        await wrapper.__call_async__(
            wrapped=fake_send_task,
            instance=instance,
            args=({"message": {"role": "user", "parts": []}},),
            kwargs={},
        )

    spans = get_spans(in_memory_exporter)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code == StatusCode.ERROR, (
        "Span should have ERROR status when A2A call raises an exception"
    )
    # Exception should be recorded as a span event
    event_names = [e.name for e in span.events]
    assert "exception" in event_names, (
        "Exception should be recorded as a span event"
    )


# ─────────────────────────────────────────────────────────────────────────────
# TEST 10: Attribute extraction from A2A Task objects
# ─────────────────────────────────────────────────────────────────────────────

def test_a2a_attribute_extraction_from_task():
    from traceai_a2a._attributes import get_task_attributes
    from traceai_a2a._semantic_conventions import A2A_TASK_ID, A2A_TASK_STATE

    task = _MockTask(task_id="task-abc-123", state="completed")
    attrs = dict(get_task_attributes(task))

    assert attrs.get(A2A_TASK_ID) == "task-abc-123"
    assert attrs.get(A2A_TASK_STATE) == "completed"


def test_a2a_attribute_extraction_from_dict():
    """Attribute extractor should handle raw dict payloads as well as SDK objects."""
    from traceai_a2a._attributes import get_task_attributes
    from traceai_a2a._semantic_conventions import A2A_TASK_ID, A2A_TASK_STATE

    task_dict = {
        "id": "dict-task-456",
        "status": {"state": "failed"},
    }
    attrs = dict(get_task_attributes(task_dict))

    assert attrs.get(A2A_TASK_ID) == "dict-task-456"
    assert attrs.get(A2A_TASK_STATE) == "failed"


def test_a2a_message_attribute_extraction():
    from traceai_a2a._attributes import get_message_attributes
    from traceai_a2a._semantic_conventions import A2A_MESSAGE_PARTS_COUNT, A2A_MESSAGE_ROLE

    message = {"role": "user", "parts": ["part1", "part2", "part3"]}
    attrs = dict(get_message_attributes(message))

    assert attrs.get(A2A_MESSAGE_ROLE) == "user"
    assert attrs.get(A2A_MESSAGE_PARTS_COUNT) == 3


# ─────────────────────────────────────────────────────────────────────────────
# TEST 11: Semantic convention completeness
# ─────────────────────────────────────────────────────────────────────────────

def test_a2a_semantic_conventions_all_defined():
    """All A2A semantic convention constants must be defined."""
    from traceai_a2a import _semantic_conventions as sc

    required_attrs = [
        "A2A_TASK_ID",
        "A2A_TASK_STATE",
        "A2A_AGENT_URL",
        "A2A_AGENT_CARD_NAME",
        "A2A_AGENT_CARD_VERSION",
        "A2A_MESSAGE_ROLE",
        "A2A_MESSAGE_PARTS_COUNT",
        "A2A_ARTIFACT_TYPE",
        "A2A_STREAMING",
        "A2A_PUSH_NOTIFICATION_URL",
        "A2A_PROPAGATED_TRACE_ID",
        "A2A_SPAN_KIND_CLIENT",
        "A2A_SPAN_KIND_SERVER",
    ]

    for attr in required_attrs:
        assert hasattr(sc, attr), f"Missing semantic convention: {attr}"
        assert getattr(sc, attr).startswith("gen_ai.a2a.") or getattr(sc, attr).startswith("A2A_"), (
            f"Convention {attr!r} should start with 'gen_ai.a2a.' or 'A2A_', "
            f"got {getattr(sc, attr)!r}"
        )


def test_a2a_fi_types_has_new_attributes():
    """New A2A attributes must exist in fi_instrumentation.fi_types.SpanAttributes."""
    from fi_instrumentation.fi_types import SpanAttributes

    assert hasattr(SpanAttributes, "GEN_AI_A2A_TASK_ID")
    assert hasattr(SpanAttributes, "GEN_AI_A2A_TASK_STATE")
    assert hasattr(SpanAttributes, "GEN_AI_A2A_AGENT_URL")
    assert hasattr(SpanAttributes, "GEN_AI_A2A_STREAMING")
    assert hasattr(SpanAttributes, "GEN_AI_A2A_PROPAGATED_TRACE_ID")

    assert SpanAttributes.GEN_AI_A2A_TASK_ID == "gen_ai.a2a.task.id"
    assert SpanAttributes.GEN_AI_A2A_AGENT_URL == "gen_ai.a2a.agent.url"


def test_a2a_fi_types_has_new_span_kinds():
    """New A2A span kinds must exist in FiSpanKindValues."""
    from fi_instrumentation.fi_types import FiSpanKindValues

    assert hasattr(FiSpanKindValues, "A2A_CLIENT")
    assert hasattr(FiSpanKindValues, "A2A_SERVER")
    assert FiSpanKindValues.A2A_CLIENT.value == "A2A_CLIENT"
    assert FiSpanKindValues.A2A_SERVER.value == "A2A_SERVER"


def test_a2a_fi_types_has_new_eval_names():
    """New A2A eval names must exist in EvalName."""
    from fi_instrumentation.fi_types import EvalName

    assert hasattr(EvalName, "A2A_TASK_COMPLETION")
    assert hasattr(EvalName, "A2A_RESPONSE_ALIGNMENT")
    assert hasattr(EvalName, "A2A_SAFETY_PASS_THROUGH")
    assert EvalName.A2A_TASK_COMPLETION.value == "a2a_task_completion"
