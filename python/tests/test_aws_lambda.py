"""Synthetic Lambda events and a real localhost OTLP/HTTP protobuf receiver."""

import asyncio
import functools
import inspect
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import baggage, context as context_api, trace
from opentelemetry.exporter.otlp.proto.http import Compression
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode

from fi_instrumentation import register, suppress_tracing
from fi_instrumentation.aws_lambda import LambdaTracer
from fi_instrumentation.fi_types import ProjectType
from fi_instrumentation.otel import SimpleSpanProcessor as FiSimpleSpanProcessor
from fi_instrumentation.otel import TracerProvider as FiTracerProvider

PARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
TRACE_ID = int("0123456789abcdef0123456789abcdef", 16)
SPAN_ID = int("0123456789abcdef", 16)
SECRET = "SYNTHETIC_CONTENT_MUST_NOT_BE_EXPORTED"


def invocation(remaining=10_000):
    return SimpleNamespace(
        aws_request_id="synthetic-request",
        get_remaining_time_in_millis=lambda: remaining,
    )


@pytest.fixture
def setup():
    exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource({"project_name": "lambda-test", "project_type": "observe"})
    )
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing = LambdaTracer(provider)
    yield tracing, provider, exporter
    provider.shutdown()


@pytest.mark.parametrize(
    "event",
    [
        {"version": "2.0", "headers": {"traceparent": PARENT}},
        {
            "version": "2.0",
            "requestContext": {"http": {}},
            "headers": {"TraceParent": PARENT},
        },
        {"httpMethod": "GET", "requestContext": {}, "headers": {"Traceparent": PARENT}},
        {"requestContext": {"elb": {}}, "multiValueHeaders": {"Traceparent": [PARENT]}},
        {
            "requestContext": {"elb": {}},
            "headers": None,
            "multiValueHeaders": {"TRACEPARENT": [PARENT]},
        },
        {
            "httpMethod": "POST",
            "headers": {"traceparent": "bad"},
            "multiValueHeaders": {"traceparent": [PARENT]},
        },
    ],
)
def test_http_parent_shapes(setup, event):
    tracing, provider, exporter = setup

    @tracing.wrap
    def handler(event, context):
        with provider.get_tracer("child").start_as_current_span("lookup"):
            return {"statusCode": 200}

    assert handler(event, invocation()) == {"statusCode": 200}
    child, root = exporter.get_finished_spans()
    assert root.name == "lambda.handler"
    assert root.kind == SpanKind.SERVER
    assert root.parent.span_id == SPAN_ID
    assert root.context.trace_id == TRACE_ID
    assert root.attributes["faas.trigger"] == "http"
    assert root.attributes["fi.span.kind"] == "CHAIN"
    assert child.parent.span_id == root.context.span_id


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "not an event",
        {},
        {"headers": {"traceparent": PARENT}},
        {"Records": [{"eventSource": "aws:sqs", "body": SECRET}]},
        {"version": "2.0", "headers": {"traceparent": "invalid"}},
        {"httpMethod": "GET", "headers": []},
        {"httpMethod": "GET", "headers": {"traceparent": [PARENT]}},
        {"httpMethod": "GET", "headers": {0: PARENT}},
        {"httpMethod": "GET", "multiValueHeaders": {"traceparent": []}},
        {
            "httpMethod": "GET",
            "headers": {"traceparent": PARENT},
            "multiValueHeaders": {"traceparent": [PARENT, PARENT]},
        },
    ],
)
def test_unknown_or_malformed_events_start_fresh(setup, event):
    tracing, provider, exporter = setup

    @tracing.wrap
    def handler(event, context):
        return event

    with provider.get_tracer("ambient").start_as_current_span("unrelated"):
        assert handler(event, invocation()) is event
    root = exporter.get_finished_spans()[0]
    assert root.parent is None
    assert root.context.trace_id != TRACE_ID


def test_cold_warm_context_privacy_and_result_identity(setup):
    tracing, provider, exporter = setup
    result = {"body": SECRET}
    event = {
        "version": "2.0",
        "body": SECRET,
        "rawQueryString": SECRET,
        "headers": {
            "traceparent": PARENT,
            "tracestate": "vendor=" + SECRET,
            "baggage": "password=" + SECRET,
        },
    }

    @tracing.wrap
    def handler(event, context):
        assert baggage.get_all() == {}
        return result

    with provider.get_tracer("ambient").start_as_current_span("ambient") as ambient:
        token = context_api.attach(baggage.set_baggage("ambient-secret", SECRET))
        try:
            assert handler(event, invocation()) is result
            assert trace.get_current_span() is ambient
            assert baggage.get_baggage("ambient-secret") == SECRET
        finally:
            context_api.detach(token)
        assert handler({}, invocation()) is result
    first, second, _ = exporter.get_finished_spans()
    assert first.attributes["faas.coldstart"] is True
    assert second.attributes["faas.coldstart"] is False
    assert first.context.trace_state.to_header() == ""
    assert second.parent is None
    assert all(
        SECRET not in str(s.attributes) and not s.events for s in (first, second)
    )


@pytest.mark.parametrize(
    "error", [ValueError(SECRET), SystemExit(SECRET), asyncio.CancelledError(SECRET)]
)
def test_error_and_baseexception_identity_status_and_flush(setup, error):
    tracing, provider, exporter = setup
    original_flush = provider.force_flush
    flushed = []

    def flush(timeout_millis):
        flushed.append(exporter.get_finished_spans())
        return original_flush(timeout_millis)

    provider.force_flush = flush

    @tracing.wrap
    def handler(event, context):
        raise error

    with pytest.raises(type(error)) as caught:
        handler({"body": SECRET}, invocation())
    assert caught.value is error
    assert len(flushed) == 1 and len(flushed[0]) == 1
    root = exporter.get_finished_spans()[0]
    assert root.status.status_code == StatusCode.ERROR
    assert root.status.description is None
    assert root.attributes["error.type"] == type(error).__name__
    assert root.events == ()
    assert SECRET not in str(root.attributes)
    assert not trace.get_current_span().get_span_context().is_valid


HTTP_EVENTS = [
    {"version": "2.0", "requestContext": {"http": {"method": "GET"}}},
    {"httpMethod": "GET", "requestContext": {}},
    {"requestContext": {"elb": {}}, "httpMethod": "GET"},
]


@pytest.fixture(params=["sdk", "fi"])
def any_provider(request):
    """The plain SDK provider and the FI provider ``register()`` builds."""
    exporter = InMemorySpanExporter()
    if request.param == "fi":
        provider = FiTracerProvider(verbose=False)
        provider.add_span_processor(FiSimpleSpanProcessor(span_exporter=exporter))
    else:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield request.param, LambdaTracer(provider), exporter
    provider.shutdown()


@pytest.mark.parametrize("event", HTTP_EVENTS)
@pytest.mark.parametrize("code", [500, 502, 503, 599])
def test_http_5xx_response_marks_server_span_error(any_provider, event, code):
    _, tracing, exporter = any_provider
    result = {"statusCode": code, "headers": {"x-secret": SECRET}, "body": SECRET}

    @tracing.wrap
    def handler(event, context):
        return result

    assert handler(event, invocation()) is result
    (root,) = exporter.get_finished_spans()
    assert root.kind == SpanKind.SERVER
    assert root.attributes["faas.trigger"] == "http"
    assert root.attributes["http.response.status_code"] == code
    assert root.attributes["error.type"] == str(code)
    assert root.status.status_code == StatusCode.ERROR
    assert root.status.description is None
    assert root.events == ()
    assert SECRET not in str(root.attributes)


@pytest.mark.parametrize("code", [200, 201, 302, 400, 404, 499])
def test_http_non_5xx_response_records_code_without_error(any_provider, code):
    kind, tracing, exporter = any_provider
    handler = tracing.wrap(lambda event, context: {"statusCode": code, "body": SECRET})
    assert handler(HTTP_EVENTS[0], invocation())["statusCode"] == code
    (root,) = exporter.get_finished_spans()
    assert root.attributes["http.response.status_code"] == code
    assert "error.type" not in root.attributes
    # OTel leaves 4xx SERVER spans unset; the FI processor turns unset into OK.
    expected = StatusCode.OK if kind == "fi" else StatusCode.UNSET
    assert root.status.status_code == expected
    assert SECRET not in str(root.attributes)


class _HostileDict(dict):
    def get(self, *args):
        raise AssertionError("dict subclass hooks must not run")

    def __getitem__(self, key):
        raise AssertionError("dict subclass hooks must not run")


@pytest.mark.parametrize(
    "event,result",
    [
        (HTTP_EVENTS[0], {"statusCode": "500"}),
        (HTTP_EVENTS[0], {"statusCode": True}),
        (HTTP_EVENTS[0], {"statusCode": 500.0}),
        (HTTP_EVENTS[0], {"statusCode": 99}),
        (HTTP_EVENTS[0], {"statusCode": 600}),
        (HTTP_EVENTS[0], {"body": SECRET}),
        (HTTP_EVENTS[0], [500]),
        (HTTP_EVENTS[0], SECRET),
        (HTTP_EVENTS[0], None),
        ({}, {"statusCode": 500}),
        ({"Records": []}, {"statusCode": 503}),
    ],
)
def test_unrecognized_or_non_http_results_leave_status_alone(
    any_provider, event, result
):
    kind, tracing, exporter = any_provider
    handler = tracing.wrap(lambda event, context: result)
    assert handler(event, invocation()) is result
    (root,) = exporter.get_finished_spans()
    assert "http.response.status_code" not in root.attributes
    assert "error.type" not in root.attributes
    expected = StatusCode.OK if kind == "fi" else StatusCode.UNSET
    assert root.status.status_code == expected


def test_dict_subclass_result_is_read_without_its_hooks(any_provider):
    _, tracing, exporter = any_provider
    result = _HostileDict(statusCode=500)
    handler = tracing.wrap(lambda event, context: result)
    assert handler(HTTP_EVENTS[0], invocation()) is result
    (root,) = exporter.get_finished_spans()
    assert root.attributes["http.response.status_code"] == 500
    assert root.status.status_code == StatusCode.ERROR


def test_broken_span_status_does_not_replace_http_result(setup, caplog):
    tracing, _, _ = setup
    span = Mock(spec=trace.Span)
    span.set_attribute.side_effect = RuntimeError(SECRET)
    tracing._tracer = Mock(start_span=Mock(return_value=span))
    result = {"statusCode": 500}
    handler = tracing.wrap(lambda event, context: result)
    assert handler(HTTP_EVENTS[0], invocation()) is result
    assert "response status could not be set" in caplog.text
    assert SECRET not in caplog.text
    span.end.assert_called_once()


def test_wrapping_is_idempotent_and_preserves_metadata(setup):
    tracing, provider, exporter = setup

    def handler(event, context):
        """Application docstring."""
        return 7

    wrapped = tracing.wrap(handler)
    assert wrapped.__name__ == handler.__name__
    assert wrapped.__doc__ == handler.__doc__
    assert inspect.signature(wrapped) == inspect.signature(handler)
    assert tracing.wrap(wrapped) is wrapped
    with pytest.raises(ValueError, match="already wrapped"):
        LambdaTracer(provider).wrap(wrapped)
    assert wrapped(event={}, context=invocation()) == 7
    assert len(exporter.get_finished_spans()) == 1


def test_rejects_async_and_generator_handlers(setup):
    tracing, _, _ = setup

    async def async_handler(event, context):
        return 1

    def generator(event, context):
        yield 1

    async def async_generator(event, context):
        yield 1

    for handler in (
        async_handler,
        generator,
        async_generator,
        functools.partial(async_handler),
    ):
        with pytest.raises(TypeError, match="synchronous"):
            tracing.wrap(handler)


@pytest.mark.parametrize(
    "key,value",
    [
        ("flush_timeout_millis", -1),
        ("reserve_millis", -1),
        ("flush_timeout_millis", 0.5),
        ("reserve_millis", True),
    ],
)
def test_validates_time_budgets(setup, key, value):
    with pytest.raises(ValueError, match="non-negative integer"):
        LambdaTracer(setup[1], **{key: value})


def test_suppression_does_not_emit_or_flush(setup):
    tracing, provider, exporter = setup
    provider.force_flush = Mock()
    handler = tracing.wrap(lambda event, context: 7)
    with suppress_tracing():
        assert handler({}, invocation()) == 7
    assert not exporter.get_finished_spans()
    provider.force_flush.assert_not_called()
    handler({}, invocation())
    assert exporter.get_finished_spans()[0].attributes["faas.coldstart"] is False


@pytest.mark.parametrize("remaining", [0, 100, 250])
def test_exhausted_reserve_skips_flush(setup, remaining):
    tracing, provider, _ = setup
    provider.force_flush = Mock()
    assert tracing.flush(invocation(remaining)) is False
    provider.force_flush.assert_not_called()


def test_budget_uses_time_remaining_after_work_and_flushes_ended_span(setup):
    tracing, provider, exporter = setup
    context = invocation()
    timeouts = []

    def force_flush(timeout_millis):
        assert len(exporter.get_finished_spans()) == 1
        timeouts.append(timeout_millis)
        return True

    provider.force_flush = force_flush

    @tracing.wrap
    def handler(event, context):
        context.get_remaining_time_in_millis = lambda: 350
        return "ok"

    assert handler({}, context) == "ok"
    assert 0 < timeouts[0] <= 100


def test_flush_failure_is_sanitized_and_does_not_change_application_result(
    setup, caplog
):
    tracing, provider, _ = setup
    provider.force_flush = Mock(side_effect=ValueError(SECRET))
    handler = tracing.wrap(lambda event, context: 42)
    assert handler({}, invocation()) == 42
    assert "Lambda trace flush failed" in caplog.text
    assert SECRET not in caplog.text


def test_bad_remaining_callback_skips_flush(setup, caplog):
    tracing, provider, _ = setup
    provider.force_flush = Mock()
    context = SimpleNamespace(
        get_remaining_time_in_millis=Mock(side_effect=ValueError(SECRET))
    )
    assert tracing.flush(context) is False
    provider.force_flush.assert_not_called()
    assert SECRET not in caplog.text


@pytest.mark.parametrize("remaining", [float("nan"), float("inf"), "1000", None])
def test_invalid_remaining_time_skips_flush(setup, remaining):
    tracing, provider, _ = setup
    provider.force_flush = Mock()
    assert tracing.flush(invocation(remaining)) is False
    provider.force_flush.assert_not_called()


def test_thread_start_failure_releases_slot(setup, monkeypatch):
    tracing, provider, _ = setup
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", Mock(side_effect=RuntimeError(SECRET)))
        assert tracing.flush(invocation()) is False
    assert tracing.flush(invocation()) is True


def test_timed_out_flush_does_not_accumulate_workers(setup):
    _, provider, _ = setup
    tracing = LambdaTracer(provider, flush_timeout_millis=40)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def blocking_flush(timeout_millis):
        calls.append(timeout_millis)
        entered.set()
        release.wait(3)
        finished.set()
        return True

    provider.force_flush = blocking_flush
    start = time.monotonic()
    try:
        assert tracing.flush(invocation()) is False
        assert time.monotonic() - start < 0.4
        assert entered.is_set() and not finished.is_set()
        for _ in range(20):
            assert tracing.flush(invocation()) is False
        assert len(calls) == 1
    finally:
        release.set()
        assert finished.wait(1)
    # Acquire waits for the worker's finally block, without polling or sleeping.
    with tracing._flush_lock:
        pass
    provider.force_flush = lambda timeout_millis: True
    assert tracing.flush(invocation()) is True


def test_provider_without_flush_and_missing_context_are_supported():
    tracing = LambdaTracer(trace.NoOpTracerProvider())
    assert tracing.wrap(lambda event, context: 11)({}, None) == 11
    assert tracing.flush() is True
    assert (
        LambdaTracer(trace.NoOpTracerProvider(), flush_timeout_millis=0).flush()
        is False
    )


def test_broken_tracer_does_not_skip_or_repeat_handler(setup, caplog):
    tracing, _, _ = setup
    tracing._tracer = Mock(start_span=Mock(side_effect=ValueError(SECRET)))
    handler = Mock(return_value=42)
    assert tracing.wrap(handler)({}, invocation()) == 42
    handler.assert_called_once()
    assert SECRET not in caplog.text


def test_broken_span_does_not_replace_application_exception(setup, caplog):
    tracing, _, _ = setup
    span = Mock(spec=trace.Span)
    span.set_attribute.side_effect = RuntimeError(SECRET)
    span.end.side_effect = RuntimeError(SECRET)
    tracing._tracer = Mock(start_span=Mock(return_value=span))
    error = ValueError("application")

    def handler(event, context):
        raise error

    with pytest.raises(ValueError) as caught:
        tracing.wrap(handler)({}, invocation())
    assert caught.value is error
    assert SECRET not in caplog.text


@pytest.fixture
def receiver():
    packets = []
    response_gate = threading.Event()
    response_gate.set()
    received = threading.Event()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            packets.append(
                (
                    self.path,
                    {k.lower(): v for k, v in self.headers.items()},
                    request,
                    body,
                )
            )
            received.set()
            response_gate.wait(3)
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", packets, response_gate, received
    response_gate.set()
    server.shutdown()
    thread.join(2)
    server.server_close()


def test_slow_otlp_receiver_does_not_extend_invocation_wait(receiver):
    endpoint, packets, response_gate, received = receiver
    provider = TracerProvider(
        resource=Resource({"project_name": "lambda-test", "project_type": "observe"})
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=endpoint + "/tracer/v1/traces",
                timeout=2,
                compression=Compression.NoCompression,
            ),
            schedule_delay_millis=60_000,
        )
    )
    tracing = LambdaTracer(provider, flush_timeout_millis=100)
    handler = tracing.wrap(lambda event, context: "returned")
    response_gate.clear()
    try:
        start = time.monotonic()
        assert handler({}, invocation()) == "returned"
        assert time.monotonic() - start < 0.5
        assert received.wait(1)
        assert packets
        # Response is still blocked, proving bounded waiting, not cancellation.
        assert not response_gate.is_set()
    finally:
        response_gate.set()
        with tracing._flush_lock:
            pass
        provider.shutdown()


def attributes(items):
    return {item.key: item.value for item in items}


@pytest.mark.parametrize("use_register", [False, True])
def test_real_otlp_wire_privacy_auth_resource_parent_and_flush(
    receiver, monkeypatch, use_register
):
    endpoint, packets, _, _ = receiver
    headers = {"X-Api-Key": "synthetic-api", "X-Secret-Key": "synthetic-secret"}
    if use_register:
        monkeypatch.setenv("FI_BASE_URL", endpoint)
        monkeypatch.setenv("FI_API_KEY", headers["X-Api-Key"])
        monkeypatch.setenv("FI_SECRET_KEY", headers["X-Secret-Key"])
        provider = register(
            project_name="lambda-test",
            project_type=ProjectType.OBSERVE,
            batch=True,
            schedule_delay_millis=60_000,
            timeout=2,
            verbose=False,
        )
    else:
        provider = TracerProvider(
            resource=Resource(
                {"project_name": "lambda-test", "project_type": "observe"}
            )
        )
        exporter = OTLPSpanExporter(
            endpoint=endpoint + "/tracer/v1/traces",
            headers=headers,
            timeout=2,
            compression=Compression.NoCompression,
        )
        provider.add_span_processor(
            BatchSpanProcessor(exporter, schedule_delay_millis=60_000)
        )
    tracing = LambdaTracer(provider)
    application_error = ValueError(SECRET)

    @tracing.wrap
    def handler(event, context):
        with provider.get_tracer("tool").start_as_current_span(
            "lookup_order",
            attributes={"fi.span.kind": "TOOL"},
            record_exception=False,
            set_status_on_exception=False,
        ):
            if event.get("fail"):
                raise application_error
            return {"body": SECRET}

    event = {
        "version": "2.0",
        "body": SECRET,
        "rawQueryString": SECRET,
        "headers": {
            "traceparent": PARENT,
            "Authorization": SECRET,
            "baggage": "secret=" + SECRET,
            "tracestate": "secret=" + SECRET,
        },
        "requestContext": {"http": {"userAgent": SECRET}},
    }
    try:
        assert handler(event, invocation()) == {"body": SECRET}
        assert packets, "must export before returning, not on the 60-second batch timer"
        with pytest.raises(ValueError) as caught:
            handler(dict(event, fail=True), invocation())
        assert caught.value is application_error
        spans = []
        for path, received_headers, request, body in packets:
            assert path == "/tracer/v1/traces"
            assert received_headers["x-api-key"] == "synthetic-api"
            assert received_headers["x-secret-key"] == "synthetic-secret"
            assert SECRET.encode() not in body
            for resource_spans in request.resource_spans:
                attrs = attributes(resource_spans.resource.attributes)
                assert attrs["project_name"].string_value == "lambda-test"
                assert attrs["project_type"].string_value == "observe"
                assert "host.name" not in attrs
                for scope in resource_spans.scope_spans:
                    spans.extend(scope.spans)
        roots = [span for span in spans if span.name == "lambda.handler"]
        children = [span for span in spans if span.name == "lookup_order"]
        assert len(roots) == len(children) == 2
        assert [
            attributes(s.attributes)["faas.coldstart"].bool_value for s in roots
        ] == [True, False]
        for root, child in zip(roots, children):
            assert root.kind == 2  # SERVER
            assert root.trace_id.hex() == "0123456789abcdef0123456789abcdef"
            assert root.parent_span_id.hex() == "0123456789abcdef"
            assert child.parent_span_id == root.span_id
            assert root.trace_state == ""
            assert not root.events
            assert attributes(root.attributes)["fi.span.kind"].string_value == "CHAIN"
        assert roots[1].status.code == 2  # ERROR
        assert roots[1].status.message == ""
        assert (
            attributes(roots[1].attributes)["error.type"].string_value == "ValueError"
        )
    finally:
        provider.shutdown()


def test_readme_register_setup_exports_5xx_response_as_error(receiver, monkeypatch):
    endpoint, packets, _, _ = receiver
    monkeypatch.setenv("FI_BASE_URL", endpoint)
    monkeypatch.setenv("FI_API_KEY", "synthetic-api")
    monkeypatch.setenv("FI_SECRET_KEY", "synthetic-secret")
    provider = register(
        project_name="lambda-test",
        project_type=ProjectType.OBSERVE,
        batch=True,
        schedule_delay_millis=60_000,
        timeout=2,
        verbose=False,
    )
    tracing = LambdaTracer(provider)

    @tracing.wrap
    def handler(event, context):
        return {"statusCode": event["code"], "body": SECRET}

    try:
        for code in (500, 404):
            event = {"version": "2.0", "code": code, "headers": {"traceparent": PARENT}}
            assert handler(event, invocation())["statusCode"] == code
        assert all(SECRET.encode() not in body for *_, body in packets)
        roots = [
            span
            for _, _, request, _ in packets
            for resource_spans in request.resource_spans
            for scope in resource_spans.scope_spans
            for span in scope.spans
            if span.name == "lambda.handler"
        ]
        assert len(roots) == 2
        failed, not_found = (attributes(span.attributes) for span in roots)
        assert failed["http.response.status_code"].int_value == 500
        assert failed["error.type"].string_value == "500"
        assert roots[0].status.code == 2  # ERROR
        assert not_found["http.response.status_code"].int_value == 404
        assert "error.type" not in not_found
        assert roots[1].status.code == 1  # OK: the FI processor's unset-to-OK
    finally:
        provider.shutdown()
