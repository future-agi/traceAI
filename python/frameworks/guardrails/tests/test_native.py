"""Offline Guardrails integration tests; no hub validators or provider calls."""

import asyncio
from importlib.util import find_spec

import pytest
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from opentelemetry import context, trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_guardrails import GuardrailsInstrumentor
from traceai_guardrails._native import NativeTracer


@pytest.fixture
def native(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("GUARDRAILS_NO_METRICS", "true")
    if find_spec("guardrails.telemetry.guard_tracing") is None:
        pytest.skip("This SDK uses the legacy adapter, covered by the core suite")
    from guardrails.settings import settings
    from guardrails import Guard

    monkeypatch.setattr(settings.rc, "enable_metrics", False)
    monkeypatch.setattr(Guard, "_load_rc", lambda self: None)
    monkeypatch.setattr(settings, "disable_tracing", False)
    exporter = InMemorySpanExporter()
    provider = register(
        project_name="offline",
        project_type=ProjectType.OBSERVE,
        span_exporter=exporter,
        batch=False,
        verbose=False,
    )
    instrumentor = GuardrailsInstrumentor()

    def install(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return provider, exporter

    yield install
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


def validator():
    from guardrails.validators import Validator, register_validator, PassResult

    @register_validator(name="offline-local", data_type="string")
    class Local(Validator):
        def _validate(self, value, metadata):
            return PassResult(metadata={"private": "PRIVATE metadata"})

    return Local(on_fail="exception")


def wire(spans):
    # Exercise the production OTLP protobuf encoder, not repr(span.attributes).
    encoded = encode_spans(spans).SerializeToString()
    parsed = ExportTraceServiceRequest.FromString(encoded)
    assert sum(
        len(scope.spans)
        for resource in parsed.resource_spans
        for scope in resource.scope_spans
    ) == len(spans)
    return encoded


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    "hide_inputs,hide_outputs",
    [(False, False), (True, False), (False, True), (True, True)],
)
def test_real_guard_native_spans(native, asynchronous, hide_inputs, hide_outputs):
    from guardrails import Guard, AsyncGuard

    provider, exporter = native(hide_inputs=hide_inputs, hide_outputs=hide_outputs)
    guard = (AsyncGuard() if asynchronous else Guard()).use(validator())
    guard.configure(allow_metrics_collection=False)
    with provider.get_tracer("application").start_as_current_span("parent") as parent:
        result = guard.validate("PRIVATE payload")
        if asynchronous:
            result = asyncio.run(result)
        assert result.validated_output == "PRIVATE payload"
    spans = exporter.get_finished_spans()
    native_spans = [span for span in spans if span.name != "parent"]
    assert sorted(span.name for span in native_spans) == [
        "call",
        "guard",
        "offline-local.validate",
        "step",
    ]
    assert all(
        span.attributes["gen_ai.span.kind"] == "GUARDRAIL" for span in native_spans
    )
    guard_span = next(span for span in spans if span.name == "guard")
    assert guard_span.parent.span_id == parent.get_span_context().span_id
    assert all(
        span.context.trace_id == parent.get_span_context().trace_id for span in spans
    )
    encoded = wire(spans)
    assert b"PRIVATE metadata" not in encoded
    if hide_inputs and hide_outputs:
        assert b"PRIVATE payload" not in encoded
    if not hide_outputs:
        assert guard_span.attributes["output.value"] == "PRIVATE payload"


def test_execution_methods_and_context_untouched(native):
    import guardrails as gd
    from guardrails.run import Runner
    from guardrails.llm_providers import PromptCallableBase, AsyncPromptCallableBase
    from guardrails.validator_service import ValidatorServiceBase
    from guardrails.telemetry import guard_tracing, runner_tracing, validator_tracing

    originals = (
        Runner.step,
        PromptCallableBase.__call__,
        AsyncPromptCallableBase.__call__,
        ValidatorServiceBase.after_run_validator,
        gd.guard.contextvars,
        gd.async_guard.contextvars,
    )
    bindings = (
        guard_tracing.trace,
        guard_tracing.get_tracer,
        runner_tracing.trace,
        validator_tracing.trace,
        runner_tracing.add_step_attributes,
        runner_tracing.add_call_attributes,
    )
    global_provider = trace.get_tracer_provider()
    native()
    assert (
        Runner.step,
        PromptCallableBase.__call__,
        AsyncPromptCallableBase.__call__,
        ValidatorServiceBase.after_run_validator,
        gd.guard.contextvars,
        gd.async_guard.contextvars,
    ) == originals
    assert trace.get_tracer_provider() is global_provider
    GuardrailsInstrumentor().uninstrument()
    assert (
        guard_tracing.trace,
        guard_tracing.get_tracer,
        runner_tracing.trace,
        validator_tracing.trace,
        runner_tracing.add_step_attributes,
        runner_tracing.add_call_attributes,
    ) == bindings


def test_partial_setup_failure_restores_bindings(native, monkeypatch):
    import traceai_guardrails
    from guardrails.telemetry import guard_tracing

    original = guard_tracing.trace
    importer = traceai_guardrails.import_module

    def failing_import(name):
        if name.endswith("runner_tracing"):
            raise ImportError("synthetic missing telemetry")
        return importer(name)

    monkeypatch.setattr(traceai_guardrails, "import_module", failing_import)
    with pytest.raises(ImportError, match="synthetic missing telemetry"):
        native()
    assert guard_tracing.trace is original
    assert not GuardrailsInstrumentor().is_instrumented_by_opentelemetry


def test_idempotency_and_concurrent_async_guards(native):
    from guardrails import AsyncGuard

    provider, exporter = native(hide_inputs=True, hide_outputs=True)
    GuardrailsInstrumentor().instrument(tracer_provider=provider)

    async def run_one(index):
        with provider.get_tracer("application").start_as_current_span(
            f"parent-{index}"
        ) as parent:
            await asyncio.sleep(0)
            result = await AsyncGuard().use(validator()).validate("PRIVATE concurrent")
            assert result.validated_output == "PRIVATE concurrent"
            return parent.get_span_context().trace_id

    async def run():
        return await asyncio.gather(*(run_one(i) for i in range(3)))

    traces = asyncio.run(run())
    assert len(set(traces)) == 3
    spans = exporter.get_finished_spans()
    for trace_id in traces:
        assert len([span for span in spans if span.context.trace_id == trace_id]) == 5
    assert b"PRIVATE" not in wire(spans)


def test_otlp_http_payload_is_private(native):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from threading import Thread
    from guardrails import Guard
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult

    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _, exporter = native(hide_inputs=True, hide_outputs=True)
    otlp = OTLPSpanExporter(endpoint=f"http://127.0.0.1:{server.server_port}/v1/traces")
    try:
        Guard().use(validator()).validate("PRIVATE wire")
        assert otlp.export(exporter.get_finished_spans()) is SpanExportResult.SUCCESS
        assert len(received) == 1
        request = ExportTraceServiceRequest.FromString(received[0])
        assert (
            sum(
                len(scope.spans)
                for resource in request.resource_spans
                for scope in resource.scope_spans
            )
            == 4
        )
        assert b"PRIVATE" not in received[0]
    finally:
        otlp.shutdown()
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("disabled", ["upstream", "suppression"])
def test_respects_tracing_opt_out(native, monkeypatch, disabled):
    from guardrails import Guard
    from guardrails.settings import settings

    _, exporter = native()
    token = None
    if disabled == "upstream":
        monkeypatch.setattr(settings, "disable_tracing", True)
    else:
        token = context.attach(
            context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True)
        )
    try:
        assert Guard().validate("hello").validated_output == "hello"
    finally:
        if token is not None:
            context.detach(token)
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_validator_failure_wire_privacy(native, asynchronous):
    from guardrails import Guard, AsyncGuard
    from guardrails.validators import Validator, register_validator

    @register_validator(name="offline-failure", data_type="string")
    class Broken(Validator):
        def _validate(self, value, metadata):
            raise ValueError("PRIVATE error")

    _, exporter = native(hide_inputs=True, hide_outputs=True)
    guard = (AsyncGuard() if asynchronous else Guard()).use(Broken(on_fail="exception"))
    original_context = context.get_current()
    with pytest.raises(ValueError, match="PRIVATE error"):
        result = guard.validate("PRIVATE payload")
        if asynchronous:
            asyncio.run(result)
    assert context.get_current() is original_context
    spans = exporter.get_finished_spans()
    assert any(span.status.status_code is trace.StatusCode.ERROR for span in spans)
    assert b"PRIVATE" not in wire(spans)
    assert any(
        event.attributes.get("exception.type", "").endswith("ValueError")
        for span in spans
        for event in span.events
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_stream_call_never_consumes_response(native, asynchronous):
    from guardrails.telemetry import runner_tracing
    from guardrails.classes.llm.llm_response import LLMResponse

    _, exporter = native(hide_inputs=True, hide_outputs=True)
    consumed = []

    def chunks():
        for value in ("PRIVATE one", "PRIVATE two"):
            consumed.append(value)
            yield value

    async def async_chunks():
        for value in ("PRIVATE one", "PRIVATE two"):
            consumed.append(value)
            yield value

    response = LLMResponse(
        output="",
        **(
            {"async_stream_output": async_chunks()}
            if asynchronous
            else {"stream_output": chunks()}
        ),
    )

    @runner_tracing.trace_call
    def sync_call():
        return response

    @runner_tracing.trace_async_call
    async def async_call():
        return response

    result = asyncio.run(async_call()) if asynchronous else sync_call()
    assert result is response
    assert not consumed

    async def drain():
        return [value async for value in result.async_stream_output]

    assert (asyncio.run(drain()) if asynchronous else list(result.stream_output)) == [
        "PRIVATE one",
        "PRIVATE two",
    ]
    assert len(exporter.get_finished_spans()) == 1
    assert b"PRIVATE" not in wire(exporter.get_finished_spans())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_stream_step_completion_and_failure(native, asynchronous):
    from guardrails.telemetry import runner_tracing

    _, exporter = native(hide_inputs=True, hide_outputs=True)
    consumed = []

    @runner_tracing.trace_stream_step
    def sync_step():
        consumed.append(1)
        yield "PRIVATE chunk"
        raise ValueError("PRIVATE failure")

    @runner_tracing.trace_async_stream_step
    async def async_step():
        consumed.append(1)
        yield "PRIVATE chunk"
        raise ValueError("PRIVATE failure")

    async def run_async():
        stream = await async_step()
        assert not consumed
        assert await anext(stream) == "PRIVATE chunk"
        with pytest.raises(ValueError, match="PRIVATE failure"):
            await anext(stream)

    if asynchronous:
        asyncio.run(run_async())
    else:
        stream = sync_step()
        assert not consumed
        assert next(stream) == "PRIVATE chunk"
        with pytest.raises(ValueError, match="PRIVATE failure"):
            next(stream)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code is trace.StatusCode.ERROR
    assert b"PRIVATE" not in wire(spans)


def test_cancelled_native_call_ends_span_and_restores_context(native):
    from guardrails.telemetry import runner_tracing

    _, exporter = native(hide_inputs=True, hide_outputs=True)

    @runner_tracing.trace_async_call
    async def cancelled():
        raise asyncio.CancelledError("PRIVATE cancellation")

    async def run():
        original = context.get_current()
        with pytest.raises(asyncio.CancelledError, match="PRIVATE cancellation"):
            await cancelled()
        assert context.get_current() is original

    asyncio.run(run())
    assert len(exporter.get_finished_spans()) == 1
    assert b"PRIVATE" not in wire(exporter.get_finished_spans())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_complete_guard_stream(native, asynchronous):
    from guardrails import Guard, AsyncGuard

    _, exporter = native(hide_inputs=True, hide_outputs=True)
    consumed = []

    def llm(*args, **kwargs):
        for chunk in ("PRIVATE ", "answer."):
            consumed.append(chunk)
            yield chunk

    async def async_llm(*args, **kwargs):
        async def chunks():
            for chunk in ("PRIVATE ", "answer."):
                consumed.append(chunk)
                yield chunk

        from types import SimpleNamespace

        return SimpleNamespace(completion_stream=chunks())

    async def run_async():
        stream = await AsyncGuard()(
            llm_api=async_llm,
            stream=True,
            messages=[{"role": "user", "content": "PRIVATE prompt"}],
        )
        assert not consumed
        return [item async for item in stream]

    if asynchronous:
        results = asyncio.run(run_async())
    else:
        stream = Guard()(
            llm_api=llm,
            stream=True,
            messages=[{"role": "user", "content": "PRIVATE prompt"}],
        )
        assert not consumed
        results = list(stream)
    assert consumed == ["PRIVATE ", "answer."]
    assert results
    spans = exporter.get_finished_spans()
    assert any(
        span.name in ("stream_guard_span", "async_stream_span") for span in spans
    )
    assert b"PRIVATE" not in wire(spans)


@pytest.mark.parametrize("hide", [False, True])
def test_semantic_mapping_events_links_and_unrelated_spans(hide):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = NativeTracer(
        provider.get_tracer("guardrails-ai"),
        TraceConfig(hide_inputs=hide, hide_outputs=hide),
    )
    with provider.get_tracer("application").start_as_current_span("parent") as parent:
        parent.set_attribute("llm.invocation_parameters", "application owned")
        with tracer.start_as_current_span(
            "guard",
            attributes={
                "input.value": "PRIVATE prompt",
                "unknown": "PRIVATE diagnostic",
            },
            links=[
                trace.Link(parent.get_span_context(), {"input.value": "PRIVATE link"})
            ],
        ) as span:
            span.set_attributes(
                {
                    "openinference.span.kind": "GUARDRAIL",
                    "llm.model_name": "synthetic",
                    "llm.token_count.prompt": 0,
                    "llm.token_count.completion": 3,
                    "llm.input_messages.0.message.content": "PRIVATE prompt",
                    "llm.output_messages.0.message.content": "PRIVATE output",
                    "llm.invocation_parameters": "PRIVATE parameters",
                    "validator.init.secret": "PRIVATE init",
                    "client.ip": "PRIVATE baggage",
                }
            )
            span.add_event("PRIVATE event", {"input.value": "PRIVATE prompt"})
            span.add_event(
                "exception",
                {"exception.type": "ValueError", "exception.message": "PRIVATE error"},
            )
    spans = exporter.get_finished_spans()
    attrs = spans[0].attributes
    assert attrs["gen_ai.request.model"] == "synthetic"
    assert attrs["gen_ai.usage.input_tokens"] == 0
    assert attrs["gen_ai.usage.output_tokens"] == 3
    assert not any(key.startswith("llm.") for key in attrs)
    encoded = wire(spans)
    for forbidden in (
        b"PRIVATE diagnostic",
        b"PRIVATE parameters",
        b"PRIVATE init",
        b"PRIVATE baggage",
        b"PRIVATE event",
    ):
        assert forbidden not in encoded
    if hide:
        assert b"PRIVATE" not in encoded
    assert spans[1].attributes["llm.invocation_parameters"] == "application owned"
    provider.shutdown()
