"""FI privacy must run before OI exposes context attributes to the SDK."""

import inspect
import json

import pytest
from agno.agent import Agent
from agno.knowledge.knowledge import Knowledge
from agno.workflow import Workflow
from agno.workflow.step import Step
from agno.workflow.types import StepInput, StepOutput
from fi_instrumentation.instrumentation import TraceConfig as FITraceConfig
from openinference.instrumentation import (
    OITracer,
    TraceConfig,
    get_attributes_from_context,
    using_attributes,
)
from opentelemetry import trace
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Sampler
from traceai_agno import configure_agno_tracing
from traceai_agno._wrappers._privacy import AgnoTracer, privacy_configs

from . import test_release_privacy_workflows as loopback
from .test_knowledge import VectorStore

model = loopback.model
server = loopback.server

TEMPLATE = "SYNTHETIC-TEMPLATE-PRIVATE-572"
CONTACT = "synthetic-context@example.invalid"


class CaptureSampler(Sampler):
    def __init__(self):
        self.attributes = []

    def should_sample(
        self,
        parent_context,
        trace_id,
        name,
        kind=None,
        attributes=None,
        links=None,
        trace_state=None,
    ):
        self.attributes.append(dict(attributes or {}))
        return ALWAYS_ON.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self):
        return "capture-context-attributes"


class CaptureStarts(SpanProcessor):
    def __init__(self):
        self.attributes = []

    def on_start(self, span, parent_context=None):
        # Copy now: inspecting the span after end would miss transient leaks.
        self.attributes.append(dict(span.attributes))


def ids_exempt_from_pii():
    """Cores with the issue #195 scope keep identifier fields under PII redaction;
    older published cores redact them. Metadata is redacted by both."""
    return FITraceConfig(pii_redaction=True).mask("user.id", CONTACT) == CONTACT


def context_attributes():
    return using_attributes(
        prompt_template_variables={"customer": TEMPLATE},
        user_id=CONTACT,
        metadata={"contact": CONTACT, "safe": "retained"},
    )


def assert_policy(attributes, expected):
    for key, value in expected.items():
        assert attributes.get(key) == value, (key, attributes)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("endpoint_created", [False, True])
@pytest.mark.parametrize("operation", ["knowledge", "agent", "workflow"])
@pytest.mark.parametrize("privacy", ["default", "hide_inputs", "pii"])
async def test_context_privacy_at_every_boundary(
    monkeypatch, model, receiver, asynchronous, endpoint_created, operation, privacy
):
    if privacy == "hide_inputs":
        monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    elif privacy == "pii":
        monkeypatch.setenv("FI_PII_REDACTION", "true")
    sampler = CaptureSampler()
    starts = CaptureStarts()
    exporter = InMemorySpanExporter()
    if endpoint_created:
        instrumentation = configure_agno_tracing(otlp_endpoint=receiver[0])
        provider = instrumentation.tracer_provider
    else:
        provider = TracerProvider(sampler=sampler)
        instrumentation = configure_agno_tracing(tracer_provider=provider)
    provider.add_span_processor(starts)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    before = trace.get_current_span().get_span_context()
    try:
        with context_attributes():
            raw = dict(get_attributes_from_context())
            expected = {
                key: FITraceConfig().mask(key, value) for key, value in raw.items()
            }
            # Pin the installed FI policy, not just absence of some chosen text.
            if privacy == "hide_inputs":
                assert expected != raw
            elif privacy == "pii":
                # Issue #195: cores with the new scope keep identifier fields such
                # as user.id; free-form metadata stays redacted on every core.
                if ids_exempt_from_pii():
                    assert expected["user.id"] == raw["user.id"]
                else:
                    assert CONTACT not in expected["user.id"]
                assert CONTACT not in expected["metadata"]
            if operation == "knowledge":
                store = VectorStore()
                knowledge = Knowledge(name="context-records", vector_db=store)
                result = (
                    await knowledge.asearch("safe query")
                    if asynchronous
                    else knowledge.search("safe query")
                )
                assert result == [store.document]
                kinds = {"RETRIEVER"}
            elif operation == "agent":
                agent = Agent(model=model(), telemetry=False)
                result = (
                    await agent.arun("safe query")
                    if asynchronous
                    else agent.run("safe query")
                )
                assert result.content == loopback.OUTPUT
                kinds = {"AGENT", "LLM"}
            else:

                def step(step_input: StepInput) -> StepOutput:
                    return StepOutput(content="safe result")

                workflow = Workflow(
                    name="Context",
                    steps=[Step(name="step", executor=step)],
                    telemetry=False,
                )
                if asynchronous:
                    events = [
                        event
                        async for event in workflow.arun("safe query", stream=True)
                    ]
                else:
                    events = list(workflow.run("safe query", stream=True))
                assert any(str(event.event) == "WorkflowCompleted" for event in events)
                kinds = {"CHAIN"}
            # Do not scrub or replace the application's context as a side effect.
            assert dict(get_attributes_from_context()) == raw
            assert trace.get_current_span().get_span_context() == before
        spans = exporter.get_finished_spans()
        assert len(spans) == (1 if operation == "knowledge" else 2)
        assert {span.attributes["openinference.span.kind"] for span in spans} == kinds
        assert len(starts.attributes) == len(spans)
        snapshots = starts.attributes + [dict(span.attributes) for span in spans]
        if not endpoint_created:
            assert len(sampler.attributes) == len(spans)
            snapshots += sampler.attributes
        for attributes in snapshots:
            assert_policy(attributes, expected)
        assert provider.force_flush()
        if endpoint_created:
            assert receiver[1]
            wire_spans = [
                span
                for _, _, body in receiver[1]
                for resource in ExportTraceServiceRequest.FromString(
                    body
                ).resource_spans
                for scope in resource.scope_spans
                for span in scope.spans
            ]
            assert len(wire_spans) == len(spans)
            for span in wire_spans:
                assert_policy(
                    {a.key: a.value.string_value for a in span.attributes}, expected
                )
            secret = TEMPLATE if privacy == "hide_inputs" else CONTACT
            # Issue #195: PII redaction keeps user.id on cores with the new scope,
            # and user.id is then the only attribute carrying CONTACT here
            # (assert_policy pins every value).
            assert (
                secret.encode() in b"".join(body for _, _, body in receiver[1])
            ) == (privacy == "default" or (privacy == "pii" and ids_exempt_from_pii()))
    finally:
        instrumentation.uninstrument()
        if not endpoint_created:
            provider.shutdown()


def test_composed_policy_keeps_openinference_config_and_provider_isolation(monkeypatch):
    monkeypatch.setenv("FI_PII_REDACTION", "true")
    config = TraceConfig(hide_llm_tools=True)
    original = dict(vars(config))
    oi, fi = privacy_configs(config)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = AgnoTracer(provider.get_tracer("agno"), oi, fi)
    unrelated = OITracer(provider.get_tracer("unrelated"), config)
    attributes = {
        "llm.tools.0.tool.json_schema": "SYNTHETIC-TOOL",
        "user.id": CONTACT,
        "input.value": f"reach {CONTACT}",
    }
    try:
        with context_attributes():
            raw = dict(get_attributes_from_context())
            with tracer.start_span("private", attributes=attributes) as span:
                span.set_attribute("metadata", json.dumps({"contact": CONTACT}))
            with unrelated.start_span("unrelated", attributes=attributes):
                pass
            assert dict(get_attributes_from_context()) == raw
        private, other = exporter.get_finished_spans()
        # FI PII redaction applies to the agno span's free text and metadata;
        # identifier fields such as user.id are exempt (issue #195).
        assert CONTACT not in private.attributes["input.value"]
        assert CONTACT not in private.attributes["metadata"]
        if ids_exempt_from_pii():
            assert private.attributes["user.id"] == CONTACT
        else:
            assert CONTACT not in private.attributes["user.id"]
        assert other.attributes["user.id"] == CONTACT
        assert other.attributes["input.value"] == f"reach {CONTACT}"
        assert "llm.tools.0.tool.json_schema" not in private.attributes
        assert "llm.tools.0.tool.json_schema" not in other.attributes
        assert dict(vars(config)) == original
        assert attributes["user.id"] == CONTACT
    finally:
        provider.shutdown()


def test_openinference_sampling_keeps_externalization_disabled():
    if "externalize" not in inspect.signature(TraceConfig.mask).parameters:
        pytest.skip("This OpenInference version has no blob externalization")
    calls = []

    class CustomConfig(TraceConfig):
        def mask(self, key, value, *, externalize=True):
            calls.append((key, externalize))
            return super().mask(key, value, externalize=externalize)

    oi, fi = privacy_configs(CustomConfig())
    provider = TracerProvider()
    tracer = AgnoTracer(provider.get_tracer("agno"), oi, fi)
    try:
        with tracer.start_span("custom", attributes={"custom.attribute": "retained"}):
            pass
        assert calls == [("custom.attribute", False), ("custom.attribute", True)]
    finally:
        provider.shutdown()


def test_custom_two_argument_mask_remains_supported(tracing):
    class CustomConfig(TraceConfig):
        def mask(self, key, value):
            if key == "custom.private":
                return None
            return super().mask(key, value)

    provider, exporter = tracing
    oi, fi = privacy_configs(CustomConfig())
    tracer = AgnoTracer(provider.get_tracer("agno"), oi, fi)
    with tracer.start_span("custom", attributes={"custom.private": "SYNTHETIC"}):
        pass
    assert "custom.private" not in exporter.get_finished_spans()[0].attributes


@pytest.mark.parametrize("positional", [False, True])
def test_explicit_attributes_are_private_before_sampling(positional, monkeypatch):
    monkeypatch.setenv("FI_PII_REDACTION", "true")
    sampler = CaptureSampler()
    starts = CaptureStarts()
    provider = TracerProvider(sampler=sampler)
    provider.add_span_processor(starts)
    oi, fi = privacy_configs(None)
    tracer = AgnoTracer(provider.get_tracer("agno"), oi, fi)
    attributes = {"user.id": CONTACT}
    try:
        if positional:
            span = tracer.start_span(
                "private", None, trace.SpanKind.INTERNAL, attributes
            )
        else:
            span = tracer.start_span("private", attributes=attributes)
        span.end()
        for snapshot in sampler.attributes + starts.attributes:
            assert snapshot["user.id"] == fi.mask("user.id", CONTACT)
        assert len(sampler.attributes) == len(starts.attributes) == 1
        assert attributes["user.id"] == CONTACT
    finally:
        provider.shutdown()
