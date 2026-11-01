"""One span per Messages call in each tracer provider, with SDKs that trace natively.

Anthropic SDK 1.14.0 and later emit their own OpenTelemetry span for every API call.
When that span would reach the provider this instrumentor exports to, the instrumentor
keeps its own span (with the Future AGI context, input/output and TraceConfig) and
turns the SDK's span off for that call. When the SDK's span goes elsewhere, both are
left as they are, so each provider gets the call once. Real SDK, synthetic transport.
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
from fi_instrumentation import using_attributes
from traceai_anthropic import AnthropicInstrumentor

try:
    import httpx2 as httpx
except ImportError:
    import httpx

NATIVE = "open_telemetry" in inspect.signature(anthropic.Anthropic).parameters
pytestmark = pytest.mark.skipif(not NATIVE, reason="SDK predates native OpenTelemetry")

NATIVE_SCOPE = "com.anthropic.sdk.python"
FI_SCOPE = "traceai_anthropic"
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
def test_client_passed_the_instrumentors_provider_gets_only_the_instrumentor_span(
    providers, instrument, operation, asynchronous
):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    call(operation, asynchronous, open_telemetry={"tracer_provider": fi})
    assert scopes(fi_exporter) == [FI_SCOPE]


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
    assert scopes(fi_exporter) == [FI_SCOPE]


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
    assert scopes(fi_exporter) == [FI_SCOPE]
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
    assert scopes(fi_exporter) == [FI_SCOPE]


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
    assert scopes(fi_exporter) == [FI_SCOPE]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_helper_stream_is_traced_once_by_the_sdk(providers, instrument, asynchronous):
    # messages.stream() does not go through Messages.create, so the instrumentor
    # never wraps it and the SDK's span is the only one; the client is not changed.
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    call("helper_stream", asynchronous, open_telemetry={"tracer_provider": fi})
    assert scopes(fi_exporter) == [NATIVE_SCOPE]


def test_sampling_out_the_instrumentor_span_does_not_bring_back_the_native_span(instrument):
    # The SDK's span stays off whatever the sampler decides for the instrumentor's.
    class DropInstrumentor(Sampler):
        def should_sample(self, parent_context, trace_id, name, *args, **kwargs):
            decision = Decision.DROP if name == "Messages" else Decision.RECORD_AND_SAMPLE
            return SamplingResult(decision)

        def get_description(self):
            return "DropInstrumentor"

    fi, fi_exporter = make_provider(sampler=DropInstrumentor())
    try:
        instrument(tracer_provider=fi)
        call("create", False, open_telemetry={"tracer_provider": fi})
        assert scopes(fi_exporter) == []
    finally:
        fi.shutdown()


def test_application_clients_keep_their_option_and_parent_context(providers, instrument, monkeypatch):
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
        # The per-call copy never changes the application's clients.
        assert traced.open_telemetry == options_before
        assert traced.open_telemetry["traces"]["enabled"] is True
        assert original.open_telemetry["traces"]["enabled"] is False
    spans = fi_exporter.get_finished_spans()
    assert sorted(span.instrumentation_scope.name for span in spans) == ["application", FI_SCOPE, FI_SCOPE]
    for span in spans:
        if span.instrumentation_scope.name == FI_SCOPE:
            assert span.parent.span_id == parent.get_span_context().span_id


@pytest.mark.parametrize("asynchronous", [False, True])
def test_partial_stream_is_recorded_once(providers, instrument, asynchronous):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    options = dict(api_key="synthetic", base_url="http://localhost", open_telemetry={"tracer_provider": fi})
    transport = httpx.MockTransport(respond)
    if asynchronous:

        async def run():
            async with anthropic.AsyncAnthropic(**options, http_client=httpx.AsyncClient(transport=transport)) as client:
                async with await client.messages.create(**PARAMS, stream=True) as stream:
                    assert (await stream.__anext__()).type == "message_start"

        asyncio.run(run())
    else:
        with anthropic.Anthropic(**options, http_client=httpx.Client(transport=transport)) as client:
            with client.messages.create(**PARAMS, stream=True) as stream:
                assert next(stream).type == "message_start"
    assert scopes(fi_exporter) == [FI_SCOPE]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_error_is_recorded_once(providers, instrument, asynchronous):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)
    with pytest.raises(anthropic.BadRequestError):
        call("create", asynchronous, handler=fail, open_telemetry={"tracer_provider": fi})
    spans = fi_exporter.get_finished_spans()
    assert [span.instrumentation_scope.name for span in spans] == [FI_SCOPE]
    assert spans[0].status.status_code == trace.StatusCode.ERROR


def test_turning_off_the_native_span_is_logged_once_per_instrumentor(providers, instrument, caplog):
    fi, _, _, _ = providers
    instrument(tracer_provider=fi)
    with caplog.at_level(logging.INFO, logger="traceai_anthropic"):
        call("create", False, open_telemetry={"tracer_provider": fi})
        call("create", True, open_telemetry={"tracer_provider": fi})
    notices = [r for r in caplog.records if "native OpenTelemetry" in r.getMessage()]
    assert len(notices) == 1
    assert notices[0].levelno == logging.INFO
    assert "open_telemetry=False" in notices[0].getMessage()


def test_unreadable_client_options_keep_the_instrumentor(providers, instrument, monkeypatch):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)

    def broken(self):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(type(anthropic.Anthropic(api_key="synthetic")), "open_telemetry", property(broken))
    # Losing the call is worse than a second span: an unreadable option keeps the instrumentor.
    call("create", False, open_telemetry={"tracer_provider": fi})
    assert FI_SCOPE in scopes(fi_exporter)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_client_copy_failure_keeps_the_instrumentor_and_warns_once(
    providers, instrument, monkeypatch, caplog, asynchronous
):
    fi, fi_exporter, _, _ = providers
    instrument(tracer_provider=fi)

    def broken(self, **kwargs):
        raise RuntimeError("synthetic")

    client_class = anthropic.AsyncAnthropic if asynchronous else anthropic.Anthropic
    monkeypatch.setattr(client_class, "with_options", broken)
    with caplog.at_level(logging.WARNING, logger="traceai_anthropic"):
        call("create", asynchronous, open_telemetry={"tracer_provider": fi})
        call("create", asynchronous, open_telemetry={"tracer_provider": fi})
    # The instrumentor's span is never lost; the SDK's span may then be a second one.
    assert scopes(fi_exporter).count(FI_SCOPE) == 2
    warnings = [r for r in caplog.records if "recorded twice" in r.getMessage()]
    assert len(warnings) == 1


CONTEXT = dict(session_id="sess-1", user_id="user-1", metadata={"team": "synthetic"}, tags=["t1"])


@pytest.mark.parametrize("operation,asynchronous", OPERATIONS)
@pytest.mark.parametrize("native_destination", ["global", "client"])
def test_future_agi_context_reaches_the_one_span(
    providers, global_provider, instrument, operation, asynchronous, native_destination
):
    # register(set_global_tracer_provider=True), or the provider passed to the client,
    # with using_attributes: the single span must still carry session, user, metadata,
    # tags, span kind and input.
    fi, fi_exporter, _, _ = providers
    if native_destination == "global":
        global_provider(fi)
        call_options = {}
    else:
        call_options = {"open_telemetry": {"tracer_provider": fi}}
    instrument(tracer_provider=fi)
    with using_attributes(**CONTEXT):
        call(operation, asynchronous, **call_options)
    spans = fi_exporter.get_finished_spans()
    assert [span.instrumentation_scope.name for span in spans] == [FI_SCOPE]
    attributes = dict(spans[0].attributes)
    assert attributes["session.id"] == "sess-1"
    assert attributes["user.id"] == "user-1"
    assert json.loads(attributes["metadata"]) == {"team": "synthetic"}
    assert list(attributes["tag.tags"]) == ["t1"]
    assert attributes["gen_ai.span.kind"] == "LLM"
    assert "input.value" in attributes


def test_trace_config_applies_to_the_one_span(providers, global_provider, monkeypatch, instrument):
    fi, fi_exporter, _, _ = providers
    global_provider(fi)
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    instrument(tracer_provider=fi)
    call("create", False)
    spans = fi_exporter.get_finished_spans()
    assert [span.instrumentation_scope.name for span in spans] == [FI_SCOPE]
    payload = json.dumps(dict(spans[0].attributes), default=str)
    assert "synthetic prompt" not in payload and "synthetic answer" not in payload
