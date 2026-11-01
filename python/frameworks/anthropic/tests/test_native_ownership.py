"""One span per Messages call in each tracer provider, with SDKs that trace natively.

Anthropic SDK 1.14.0 and later emit their own OpenTelemetry span for every API call.
When that span reaches the provider this instrumentor exports to, the instrumentor
leaves the call to the SDK. When it does not, the instrumentor keeps its own span,
so the instrumentor's provider never loses the call. Real SDK, synthetic transport.
"""

import asyncio
import inspect
import json
import logging

import anthropic
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import Decision, Sampler, SamplingResult
from traceai_anthropic import AnthropicInstrumentor

try:
    import httpx2 as httpx
except ImportError:
    import httpx

NATIVE = "open_telemetry" in inspect.signature(anthropic.Anthropic).parameters
pytestmark = pytest.mark.skipif(not NATIVE, reason="SDK predates native OpenTelemetry")

NATIVE_SCOPE = "com.anthropic.sdk.python"
PARAMS = dict(
    model="local-test",
    max_tokens=16,
    messages=[{"role": "user", "content": "synthetic prompt"}],
)
MESSAGE = {
    "id": "msg_local",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "synthetic answer"}],
    "model": "local-test",
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 3,
        "output_tokens": 2,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 5,
    },
}
EVENTS = [
    {"type": "message_start", "message": {**MESSAGE, "content": [], "stop_reason": None}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "synthetic answer"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
        "usage": {"output_tokens": 2},
    },
    {"type": "message_stop"},
]


def respond(request):
    if request.url.path.endswith("count_tokens"):
        return httpx.Response(200, json={"input_tokens": 12})
    if json.loads(request.content).get("stream"):
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in EVENTS)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    return httpx.Response(200, json=MESSAGE)


def fail(request):
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "synthetic error"}}
    return httpx.Response(400, json=error)


def make_provider(sampler=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(sampler=sampler) if sampler else TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def scopes(exporter):
    return [span.instrumentation_scope.name for span in exporter.get_finished_spans()]


@pytest.fixture
def providers():
    fi, fi_exporter = make_provider()
    other, other_exporter = make_provider()
    yield fi, fi_exporter, other, other_exporter
    fi.shutdown()
    other.shutdown()


@pytest.fixture
def global_provider(monkeypatch):
    """Stand in for trace.set_tracer_provider, which a process may call only once.

    Both the SDK and this instrumentor look the global provider up through
    opentelemetry.trace.get_tracer_provider at call time.
    """

    def install(provider):
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)

    return install


@pytest.fixture
def instrument():
    instrumentors = []

    def start(**kwargs):
        instrumentor = AnthropicInstrumentor()
        instrumentor.instrument(**kwargs)
        instrumentors.append(instrumentor)
        return instrumentor

    yield start
    for instrumentor in instrumentors:
        instrumentor.uninstrument()


def call(operation, asynchronous, handler=respond, **client_options):
    client_options.setdefault("max_retries", 0)
    options = dict(api_key="synthetic", base_url="http://localhost", **client_options)
    if asynchronous:

        async def run():
            transport = httpx.MockTransport(handler)
            async with anthropic.AsyncAnthropic(
                **options, http_client=httpx.AsyncClient(transport=transport)
            ) as client:
                if operation == "create":
                    message = await client.messages.create(**PARAMS)
                    assert message.content[0].text == "synthetic answer"
                elif operation == "raw_stream":
                    async with await client.messages.create(**PARAMS, stream=True) as stream:
                        assert [event.type async for event in stream] == [e["type"] for e in EVENTS]
                elif operation == "helper_stream":
                    async with client.messages.stream(**PARAMS) as stream:
                        assert (await stream.get_final_message()).content[0].text == "synthetic answer"
                else:
                    raise AssertionError(operation)

        asyncio.run(run())
        return
    with anthropic.Anthropic(
        **options, http_client=httpx.Client(transport=httpx.MockTransport(handler))
    ) as client:
        if operation == "create":
            assert client.messages.create(**PARAMS).content[0].text == "synthetic answer"
        elif operation == "raw_stream":
            with client.messages.create(**PARAMS, stream=True) as stream:
                assert [event.type for event in stream] == [e["type"] for e in EVENTS]
        elif operation == "helper_stream":
            with client.messages.stream(**PARAMS) as stream:
                assert stream.get_final_message().content[0].text == "synthetic answer"
        elif operation == "count_tokens":
            counted = client.messages.count_tokens(model=PARAMS["model"], messages=PARAMS["messages"])
            assert counted.input_tokens == 12
        else:
            raise AssertionError(operation)


OPERATIONS = [
    ("create", False),
    ("create", True),
    ("raw_stream", False),
    ("raw_stream", True),
    ("count_tokens", False),
]


@pytest.mark.parametrize("operation,asynchronous", OPERATIONS)
def test_native_span_in_the_instrumentors_provider_is_the_only_span(
    providers, instrument, operation, asynchronous
):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    call(operation, asynchronous, open_telemetry={"tracer_provider": fi})
    assert scopes(fi_exporter) == [NATIVE_SCOPE]


@pytest.mark.parametrize("operation,asynchronous", OPERATIONS)
@pytest.mark.parametrize("explicit", [True, False])
def test_global_provider_shared_with_the_sdk_gets_one_span(
    providers, global_provider, instrument, operation, asynchronous, explicit
):
    # register(set_global_tracer_provider=True) followed by instrument(), with or
    # without passing the same provider explicitly.
    fi, fi_exporter, _, _ = providers
    global_provider(fi)
    instrument(**({"tracer_provider": fi} if explicit else {}))
    call(operation, asynchronous)
    assert scopes(fi_exporter) == [NATIVE_SCOPE]


@pytest.mark.parametrize("operation,asynchronous", OPERATIONS)
@pytest.mark.parametrize("native_destination", ["global", "client"])
def test_instrumentors_provider_keeps_the_call_when_native_spans_go_elsewhere(
    providers, global_provider, instrument, operation, asynchronous, native_destination
):
    # Future AGI registered without becoming global, while another vendor owns the
    # global provider (or the client names another provider).
    fi, fi_exporter, other, other_exporter = providers
    if native_destination == "global":
        global_provider(other)
        call_options = {}
    else:
        call_options = {"open_telemetry": {"tracer_provider": other}}
    instrument(tracer_provider=fi)
    call(operation, asynchronous, **call_options)
    assert scopes(fi_exporter) == ["traceai_anthropic"]
    assert scopes(other_exporter) == [NATIVE_SCOPE]


@pytest.mark.parametrize("operation,asynchronous", OPERATIONS)
def test_non_global_provider_keeps_the_call_when_sdk_has_no_provider(
    providers, instrument, operation, asynchronous
):
    # The documented quickstart: register() without a global provider.
    fi, fi_exporter, _, _ = providers
    assert isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider)
    instrument(tracer_provider=fi)
    call(operation, asynchronous)
    assert scopes(fi_exporter) == ["traceai_anthropic"]


@pytest.mark.parametrize("operation,asynchronous", OPERATIONS)
@pytest.mark.parametrize("how", ["option", "environment"])
def test_disabled_native_tracing_keeps_the_instrumentor(
    providers, global_provider, instrument, monkeypatch, operation, asynchronous, how
):
    fi, fi_exporter, _, _ = providers
    global_provider(fi)
    instrument(tracer_provider=fi)
    if how == "environment":
        monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY", "false")
        call(operation, asynchronous)
    else:
        call(operation, asynchronous, open_telemetry=False)
    assert scopes(fi_exporter) == ["traceai_anthropic"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_helper_stream_is_traced_once_by_the_sdk(providers, instrument, asynchronous):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    call("helper_stream", asynchronous, open_telemetry={"tracer_provider": fi})
    assert scopes(fi_exporter) == [NATIVE_SCOPE]


def test_native_sampling_decision_is_not_overridden(instrument):
    # A sampler that drops the SDK's span must not make the instrumentor fall back.
    class DropNative(Sampler):
        def should_sample(self, parent_context, trace_id, name, *args, **kwargs):
            decision = Decision.DROP if name.startswith("anthropic.") else Decision.RECORD_AND_SAMPLE
            return SamplingResult(decision)

        def get_description(self):
            return "DropNative"

    fi, fi_exporter = make_provider(sampler=DropNative())
    try:
        instrument(tracer_provider=fi)
        call("create", False, open_telemetry={"tracer_provider": fi})
        assert scopes(fi_exporter) == []
    finally:
        fi.shutdown()


def test_client_copies_keep_their_own_choice(providers, instrument, monkeypatch):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    monkeypatch.setenv("ANTHROPIC_OPEN_TELEMETRY", "false")
    transport = httpx.MockTransport(respond)
    with anthropic.Anthropic(
        api_key="synthetic", base_url="http://localhost", http_client=httpx.Client(transport=transport)
    ) as original:
        traced = original.with_options(open_telemetry={"tracer_provider": fi})
        options_before = traced.open_telemetry
        with fi.get_tracer("application").start_as_current_span("parent") as parent:
            traced.messages.create(**PARAMS)
            original.messages.create(**PARAMS)
        assert traced.open_telemetry == options_before
        assert original.open_telemetry["traces"]["enabled"] is False
    spans = {span.instrumentation_scope.name: span for span in fi_exporter.get_finished_spans()}
    assert sorted(spans) == ["application", NATIVE_SCOPE, "traceai_anthropic"]
    for scope in (NATIVE_SCOPE, "traceai_anthropic"):
        assert spans[scope].parent.span_id == parent.get_span_context().span_id


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_partial_stream_is_returned_unwrapped(providers, instrument, asynchronous):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    options = dict(api_key="synthetic", base_url="http://localhost", open_telemetry={"tracer_provider": fi})
    transport = httpx.MockTransport(respond)
    if asynchronous:

        async def run():
            async with anthropic.AsyncAnthropic(**options, http_client=httpx.AsyncClient(transport=transport)) as client:
                async with await client.messages.create(**PARAMS, stream=True) as stream:
                    assert type(stream) is anthropic.AsyncStream
                    assert (await stream.__anext__()).type == "message_start"
                    assert not fi_exporter.get_finished_spans()

        asyncio.run(run())
    else:
        with anthropic.Anthropic(**options, http_client=httpx.Client(transport=transport)) as client:
            with client.messages.create(**PARAMS, stream=True) as stream:
                assert type(stream) is anthropic.Stream
                assert next(stream).type == "message_start"
                assert not fi_exporter.get_finished_spans()
    assert scopes(fi_exporter) == [NATIVE_SCOPE]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_error_is_recorded_once(providers, instrument, asynchronous):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    with pytest.raises(anthropic.BadRequestError):
        call("create", asynchronous, handler=fail, open_telemetry={"tracer_provider": fi})
    spans = fi_exporter.get_finished_spans()
    assert [span.instrumentation_scope.name for span in spans] == [NATIVE_SCOPE]
    assert spans[0].status.status_code == trace.StatusCode.ERROR


def test_deferral_is_logged_once_per_instrumentor(providers, instrument, caplog):
    fi, _, _, _ = providers
    instrument(tracer_provider=fi)
    with caplog.at_level(logging.WARNING, logger="traceai_anthropic"):
        call("create", False, open_telemetry={"tracer_provider": fi})
        call("create", True, open_telemetry={"tracer_provider": fi})
    notices = [r for r in caplog.records if "native OpenTelemetry" in r.getMessage()]
    assert len(notices) == 1
    assert "open_telemetry=False" in notices[0].getMessage()


def test_unreadable_client_options_keep_the_instrumentor(providers, instrument, monkeypatch):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)

    def broken(self):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(type(anthropic.Anthropic(api_key="synthetic")), "open_telemetry", property(broken))
    # Losing the call is worse than a second span: an unreadable option keeps the instrumentor.
    call("create", False, open_telemetry={"tracer_provider": fi})
    assert "traceai_anthropic" in scopes(fi_exporter)
