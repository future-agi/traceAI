"""Call-root transcripts through the real LiveKit filter and FI mapping; no network."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

agents = pytest.importorskip("livekit.agents", minversion="1.8")
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from fi_instrumentation.otel import HTTPSpanExporter
from livekit.agents.telemetry import set_tracer_provider
from traceai_livekit import enable_http_attribute_mapping
from traceai_livekit import _transcript

KEY = "fi.conversation.transcript"
ATTRS = {"lk.job_id": "job-a", "lk.pii.room_name": "room-a", "ordinary": "kept"}
FLAGS = ("hide_inputs", "hide_input_messages", "hide_input_text",
         "hide_outputs", "hide_output_messages", "hide_output_text")


@pytest.fixture
def call(monkeypatch, make_provider):
    for flag in FLAGS:
        monkeypatch.delenv(f"FI_{flag.upper()}", raising=False)
    monkeypatch.delenv("LIVEKIT_TELEMETRY_ALLOW_PII", raising=False)
    history = agents.llm.ChatContext()
    for role, text in [("system", "instructions"), ("user", "Hello"),
                       ("assistant", ""), ("assistant", "One moment"), ("user", "Thanks")]:
        history.add_message(role=role, content=text, interrupted=role == "assistant")
    messages = Mock(wraps=history.messages)
    session = SimpleNamespace(history=SimpleNamespace(messages=messages),
                              options=SimpleNamespace(recording_options={"redaction": False}))
    ctx = SimpleNamespace(job=SimpleNamespace(id="job-a", enable_redaction=False),
                          primary_session=session, _primary_agent_session=session)
    received = []

    def collect(self, spans):
        received.extend(spans)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(HTTPSpanExporter, "export", collect)

    def setup(*, batch=False, provider_opt_out=False):
        provider = make_provider(batch=batch)
        if provider_opt_out:
            set_tracer_provider(provider, allow_pii=False)
        assert enable_http_attribute_mapping()
        monkeypatch.setattr(agents, "get_job_context", lambda *, required: ctx)
        other = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(other))
        return provider, other

    return SimpleNamespace(setup=setup, ctx=ctx, session=session, received=received, messages=messages)


@pytest.mark.parametrize("root_name", ["job_entrypoint", "agent_session"])
@pytest.mark.parametrize("batch", [False, True])
def test_only_fi_root_copy_has_ordered_committed_turns(call, monkeypatch, root_name, batch):
    provider, other = call.setup(batch=batch)
    tracer = provider.get_tracer("livekit-agents")
    root = tracer.start_span(root_name, attributes=ATTRS)
    call.session._root_span_context = trace.set_span_in_context(root)
    child = tracer.start_span("agent_session", context=call.session._root_span_context, attributes=ATTRS)
    child.end()
    root.end()
    # A batch thread has no active job. There must be no deferred history lookup.
    monkeypatch.setattr(agents, "get_job_context", lambda *, required: None)
    provider.force_flush()

    fi_child, fi_root = call.received
    shared_child, shared_root = other.get_finished_spans()
    assert call.messages.call_count == 1
    assert fi_root.attributes[KEY] == json.dumps([
        {"role": "user", "content": "Hello"}, {"role": "assistant", "content": "One moment"},
        {"role": "user", "content": "Thanks"},
    ])
    assert fi_root is not shared_root and fi_root.context == shared_root.context
    assert fi_root.status.status_code == shared_root.status.status_code
    assert dict(shared_root.attributes) == dict(shared_child.attributes) == ATTRS
    assert KEY not in fi_child.attributes and fi_child.parent == root.get_span_context()


@pytest.mark.parametrize("flag", FLAGS)
def test_any_hide_flag_skips_history_and_preserves_root(call, monkeypatch, flag):
    monkeypatch.setenv(f"FI_{flag.upper()}", "true")
    provider, _ = call.setup()
    provider.get_tracer("livekit-agents").start_span("job_entrypoint", attributes=ATTRS).end()
    call.messages.assert_not_called()
    root, = call.received
    assert root.attributes["ordinary"] == "kept" and KEY not in root.attributes


@pytest.mark.parametrize("allowed", [False, True])
def test_policy_precedes_history_and_empty_history_writes_nothing(call, monkeypatch, allowed):
    policy = Mock(return_value=allowed)
    monkeypatch.setattr(_transcript, "allows_conversation_transcript", policy)
    call.messages.return_value = []
    provider, _ = call.setup()
    provider.get_tracer("livekit-agents").start_span("job_entrypoint", attributes=ATTRS).end()
    policy.assert_called_once()
    assert isinstance(policy.call_args.args[0], TraceConfig)
    assert call.messages.call_count == int(allowed)
    root, = call.received
    assert KEY not in root.attributes


@pytest.mark.parametrize("failure", [None, "raises", "empty"])
def test_role_masks_are_applied_before_encoding_or_the_whole_key_is_omitted(call, monkeypatch, failure):
    class Policy(TraceConfig):
        def mask(self, key, value):
            if key == SpanAttributes.INPUT_VALUE:
                return "masked input"
            assert key == SpanAttributes.OUTPUT_VALUE
            if failure == "raises":
                raise ValueError("mask failed")
            return "" if failure == "empty" else "masked output"

    monkeypatch.setattr(_transcript, "TraceConfig", Policy)
    provider, _ = call.setup()
    provider.get_tracer("livekit-agents").start_span("job_entrypoint", attributes=ATTRS).end()
    root, = call.received
    if failure:
        assert KEY not in root.attributes
    else:
        assert json.loads(root.attributes[KEY]) == [
            {"role": "user", "content": "masked input"},
            {"role": "assistant", "content": "masked output"},
            {"role": "user", "content": "masked input"},
        ]


@pytest.mark.parametrize("reason", ["provider-opt-out", "environment-opt-out", "project-redacted",
                                    "session-redacted", "no-job", "no-session", "other-job",
                                    "secondary-session", "other-scope", "shutdown"])
def test_ineligible_roots_never_read_history(call, monkeypatch, reason):
    if reason == "environment-opt-out":
        monkeypatch.setenv("LIVEKIT_TELEMETRY_ALLOW_PII", "false")
    provider, _ = call.setup(provider_opt_out=reason == "provider-opt-out")
    scope = "app" if reason == "other-scope" else "livekit-agents"
    name = "job_shutdown" if reason == "shutdown" else "job_entrypoint"
    if reason == "project-redacted":
        call.ctx.job.enable_redaction = True
    elif reason == "session-redacted":
        call.session.options.recording_options["redaction"] = True
    elif reason == "no-job":
        monkeypatch.setattr(agents, "get_job_context", lambda *, required: None)
    elif reason == "no-session":
        call.ctx.primary_session = None
    elif reason == "other-job":
        call.ctx.job.id = "job-b"
    elif reason == "secondary-session":
        name = "agent_session"
        call.session._root_span_context = trace.set_span_in_context(trace.INVALID_SPAN)
    provider.get_tracer(scope).start_span(name, attributes=ATTRS).end()
    root, = call.received
    assert root.attributes["ordinary"] == "kept" and KEY not in root.attributes
    call.messages.assert_not_called()
