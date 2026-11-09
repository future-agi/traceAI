"""Real SDK regressions at the supported floor and native telemetry boundary."""

import asyncio
from importlib.util import find_spec

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import context, trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from guardrails import AsyncGuard, Guard
from guardrails.validators import FailResult, PassResult, Validator, register_validator
from traceai_guardrails import GuardrailsInstrumentor


@pytest.fixture
def instrument(monkeypatch, tmp_path):
    # No user's Guardrails config, hub telemetry, or provider credentials.
    (tmp_path / ".guardrailsrc").write_text("no_metrics=true\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GUARDRAILS_NO_METRICS", "true")
    from guardrails.settings import settings

    if hasattr(settings, "rc"):
        monkeypatch.setattr(settings.rc, "enable_metrics", False)
        monkeypatch.setattr(Guard, "_load_rc", lambda self: None)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = GuardrailsInstrumentor()

    def start(**privacy):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**privacy))
        return provider, exporter

    yield start
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()


def make_guard(asynchronous=False, schema=None):
    cls = AsyncGuard if asynchronous else Guard
    if schema is None:
        guard = cls()
    else:
        constructor = getattr(cls, "for_pydantic", None) or cls.from_pydantic
        guard = constructor(schema)
    guard.configure(allow_metrics_collection=False)
    return guard


def wire(exporter):
    return encode_spans(exporter.get_finished_spans()).SerializeToString()


@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_rejected_candidate_is_input_not_output(instrument, monkeypatch, environment, asynchronous):
    if environment:
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
        _, exporter = instrument(hide_outputs=False)
    else:
        _, exporter = instrument(hide_inputs=True, hide_outputs=False)

    class Count(BaseModel):
        count: int

    guard = make_guard(asynchronous, Count)
    candidate = '{"count":"PRIVATE_REJECTED_CANDIDATE"}'
    result = guard.parse(candidate, num_reasks=0)
    if asynchronous:
        result = asyncio.run(result)
    assert result.validation_passed is False
    assert result.validated_output is None
    assert result.raw_llm_output == candidate
    assert exporter.get_finished_spans()
    assert b"PRIVATE_REJECTED_CANDIDATE" not in wire(exporter)
    # Instrumentation does not redact or rewrite the caller's outcome/history.
    assert guard.history.last.iterations.last.raw_output == candidate


@pytest.mark.parametrize("asynchronous", [False, True])
def test_input_privacy_retains_independently_allowed_validated_output(instrument, asynchronous):
    _, exporter = instrument(hide_inputs=True, hide_outputs=False)

    class Count(BaseModel):
        count: int

    result = make_guard(asynchronous, Count).parse('{"count":7}', num_reasks=0)
    if asynchronous:
        result = asyncio.run(result)
    assert result.validated_output == {"count": 7}
    spans = exporter.get_finished_spans()
    assert any('"count": 7' in s.attributes.get("output.value", "") for s in spans)


@pytest.mark.parametrize("passed", [True, False])
@pytest.mark.parametrize("hide_outputs", [False, True])
def test_typed_validator_outcome_survives_serialization(instrument, passed, hide_outputs):
    _, exporter = instrument(hide_inputs=False, hide_outputs=hide_outputs)

    @register_validator(name="release-verdict", data_type="string")
    class Verdict(Validator):
        def _validate(self, value, metadata):
            metadata = {"private": "PRIVATE_VALIDATOR_METADATA"}
            return PassResult(metadata=metadata) if passed else FailResult(
                error_message="synthetic failure", metadata=metadata
            )

    guard = make_guard().use(Verdict(on_fail="noop"))
    guard.validate("synthetic value")
    matches = [
        s for s in exporter.get_finished_spans()
        if s.attributes.get("validator.name", s.attributes.get("validator_name")) == "release-verdict"
    ]
    assert len(matches) == 1
    assert matches[0].attributes["output.value"] == (
        "__REDACTED__" if hide_outputs else "pass" if passed else "fail"
    )
    assert b"PRIVATE_VALIDATOR_METADATA" not in wire(exporter)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("environment", [False, True])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
def test_legacy_validator_event_directions(instrument, monkeypatch, asynchronous, environment, hide_inputs, hide_outputs):
    if find_spec("guardrails.telemetry.guard_tracing") is not None:
        pytest.skip("Legacy validator-result events are emitted by the 0.5 SDK")
    if environment:
        monkeypatch.setenv("FI_HIDE_INPUTS", str(hide_inputs).lower())
        monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hide_outputs).lower())
        provider, exporter = instrument()
    else:
        provider, exporter = instrument(hide_inputs=hide_inputs, hide_outputs=hide_outputs)

    @register_validator(name="release-event", data_type="string")
    class Fix(Validator):
        def _validate(self, value, metadata):
            return FailResult(error_message="synthetic failure", fix_value="PRIVATE_EVENT_OUTPUT")

    with provider.get_tracer("application").start_as_current_span("application") as parent:
        parent.add_event("application-event", {"input": "application input", "output": "application output"})
        result = make_guard(asynchronous).use(Fix(on_fail="fix")).validate("PRIVATE_EVENT_INPUT")
        if asynchronous:
            result = asyncio.run(result)
    assert result.validated_output == "PRIVATE_EVENT_OUTPUT"
    spans = exporter.get_finished_spans()
    events = [e for s in spans if s.name != "application" for e in s.events if e.name == "release-event_result"]
    assert events
    for event in events:
        attrs = dict(event.attributes)
        assert "input" not in attrs and "output" not in attrs
        assert attrs["input.value"] == ("__REDACTED__" if hide_inputs else "PRIVATE_EVENT_INPUT")
        assert attrs["output.value"] == ("__REDACTED__" if hide_outputs else "PRIVATE_EVENT_OUTPUT")
    app, = [s for s in spans if s.name == "application"]
    assert dict(app.events[0].attributes) == {"input": "application input", "output": "application output"}
    if hide_inputs:
        assert b"PRIVATE_EVENT_INPUT" not in wire(exporter)
    if hide_outputs:
        assert b"PRIVATE_EVENT_OUTPUT" not in wire(exporter)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("initial_candidate,reask", [(True, False), (False, False), (False, True), (True, True)])
def test_model_call_count_is_not_iteration_count(instrument, asynchronous, initial_candidate, reask):
    _, exporter = instrument()
    calls = []

    @register_validator(name="release-reask", data_type="string")
    class Accepted(Validator):
        def _validate(self, value, metadata):
            return PassResult() if value == "accepted" else FailResult(error_message="try again")

    def model(*args, **kwargs):
        calls.append(True)
        return "rejected" if reask and not initial_candidate and len(calls) == 1 else "accepted"

    async def async_model(*args, **kwargs):
        return model(*args, **kwargs)

    guard = make_guard(asynchronous).use(Accepted(on_fail="reask"))
    # A real Guard/Runner reask invokes the local callable, not a fabricated history.
    options = {"num_reasks": 1 if reask else 0}
    if not initial_candidate or reask:
        options["llm_api"] = async_model if asynchronous else model
        if find_spec("guardrails.telemetry.guard_tracing") is not None:
            options["messages"] = [{"role": "user", "content": "synthetic request"}]
        else:
            options["prompt"] = "synthetic request"
    result = guard.parse("rejected" if reask else "accepted", **options) if initial_candidate else guard(**options)
    if asynchronous:
        result = asyncio.run(result)
    assert result.validated_output == "accepted"
    expected = (1 if reask else 0) if initial_candidate else (2 if reask else 1)
    assert len(calls) == expected
    spans = exporter.get_finished_spans()
    assert spans
    for span in spans:
        # The native inferred counter is omitted rather than presenting an
        # iteration count as model usage. Actual provider token attrs remain.
        assert span.attributes.get("number_of_llm_calls") in (None, expected)


def test_async_validation_span_parentage_and_restore(instrument):
    from guardrails.run import AsyncRunner

    original = AsyncRunner.async_step
    provider, exporter = instrument(hide_inputs=True, hide_outputs=True)
    before = context.get_current()
    with provider.get_tracer("application").start_as_current_span("parent") as parent:
        result = asyncio.run(make_guard(True).validate("PRIVATE_ASYNC_VALUE"))
        assert trace.get_current_span() is parent
    assert context.get_current() is before
    assert result.validated_output == "PRIVATE_ASYNC_VALUE"
    children = [s for s in exporter.get_finished_spans() if s.name != "parent"]
    assert children and all(s.attributes.get("gen_ai.span.kind") == "GUARDRAIL" for s in children)
    assert all(s.context.trace_id == parent.get_span_context().trace_id for s in children)
    assert b"PRIVATE_ASYNC_VALUE" not in wire(exporter)
    GuardrailsInstrumentor().uninstrument()
    assert AsyncRunner.async_step is original
    exporter.clear()
    asyncio.run(make_guard(True).validate("uninstrumented"))
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("cancelled", [False, True])
def test_async_validation_failure_preserves_context_and_closes_spans(instrument, cancelled):
    _, exporter = instrument(hide_inputs=True, hide_outputs=True)
    failure_type = asyncio.CancelledError if cancelled else ValueError

    @register_validator(name="release-broken", data_type="string")
    class Broken(Validator):
        def _validate(self, value, metadata):
            raise failure_type("PRIVATE_ASYNC_FAILURE")

    guard = make_guard(True).use(Broken(on_fail="exception"))
    before = context.get_current()
    with pytest.raises(failure_type, match="PRIVATE_ASYNC_FAILURE"):
        asyncio.run(guard.validate("PRIVATE_ASYNC_INPUT"))
    assert context.get_current() is before
    spans = exporter.get_finished_spans()
    assert spans and all(s.end_time is not None for s in spans)
    assert b"PRIVATE_ASYNC" not in wire(exporter)


def test_async_validation_respects_suppression(instrument):
    _, exporter = instrument()
    token = context.attach(context.set_value(context._SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert asyncio.run(make_guard(True).validate("synthetic")).validation_passed
    finally:
        context.detach(token)
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_call_keyword_candidate_provenance(instrument, asynchronous):
    if find_spec("guardrails.telemetry.guard_tracing") is None:
        pytest.skip("The native telemetry boundary is not used by the 0.5 SDK")
    from guardrails.run import AsyncRunner, Runner

    _, exporter = instrument(hide_inputs=True, hide_outputs=False)
    # Invoke the actual installed Runner method using keywords, as opposed to
    # the positional form exercised by normal Guard.parse execution.
    runner = object.__new__(AsyncRunner if asynchronous else Runner)
    method = runner.async_call if asynchronous else runner.call
    result = method(messages=None, api=None, output="PRIVATE_KEYWORD_CANDIDATE")
    if asynchronous:
        result = asyncio.run(result)
    assert result.output == "PRIVATE_KEYWORD_CANDIDATE"
    span, = exporter.get_finished_spans()
    assert span.attributes["input.value"] == "__REDACTED__"
    assert b"PRIVATE_KEYWORD_CANDIDATE" not in wire(exporter)
