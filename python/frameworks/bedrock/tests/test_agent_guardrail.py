"""Guardrail contracts through real boto3 HTTP and binary AWS EventStream decoding."""

import base64
import json
import os
import struct
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import boto3
import pytest
from botocore.config import Config
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from traceai_bedrock import BedrockInstrumentor


TRACE_ID = "11111111-2222-3333-4444-555555555555-0"


def event_frame(event_type, body):
    """Encode the headers, length and CRCs consumed by botocore's real parser."""
    headers = b""
    for name, value in {
        ":message-type": "event",
        ":event-type": event_type,
        ":content-type": "application/json",
    }.items():
        key, val = name.encode(), value.encode()
        headers += bytes([len(key)]) + key + b"\x07" + struct.pack(">H", len(val)) + val
    payload = json.dumps(body).encode()
    prelude = struct.pack(">II", 16 + len(headers) + len(payload), len(headers))
    frame = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + payload
    return frame + struct.pack(">I", zlib.crc32(frame))


def trace_frame(event_type, data):
    return event_frame(
        "trace",
        {
            "agentId": "AGENT00001",
            "agentAliasId": "ALIAS00001",
            "sessionId": "session001",
            "trace": {event_type: data},
        },
    )


def output_frame():
    return event_frame("chunk", {"bytes": base64.b64encode(b"answer").decode()})


def model_input_frame():
    return trace_frame(
        "orchestrationTrace",
        {
            "modelInvocationInput": {
                "traceId": TRACE_ID,
                "foundationModel": "amazon.nova-lite-v1:0",
                "text": "prompt",
                "type": "ORCHESTRATION",
            }
        },
    )


def model_output_frame():
    return trace_frame(
        "orchestrationTrace",
        {
            "modelInvocationOutput": {
                "traceId": TRACE_ID,
                "rawResponse": {"content": "model answer"},
                "metadata": {"usage": {"inputTokens": 7, "outputTokens": 4}},
            }
        },
    )


def assessment(direction, action):
    # Topic names, matched words, PII and regexes can all contain private text.
    marker = f"PRIVATE-{direction} {direction}@example.com"
    return {
        "topicPolicy": {"topics": [{"name": marker, "type": "DENY", "action": action}]},
        "wordPolicy": {"customWords": [{"match": marker, "action": action}]},
        "sensitiveInformationPolicy": {
            "piiEntities": [{"match": marker, "type": "EMAIL", "action": action}],
            "regexes": [
                {"name": marker, "regex": marker, "match": marker, "action": action}
            ],
        },
    }


class TrackingProcessor(SimpleSpanProcessor):
    def __init__(self, exporter):
        super().__init__(exporter)
        self.started = []
        self.ended = []

    def on_start(self, span, parent_context=None):
        self.started.append(span.context.span_id)

    def on_end(self, span):
        self.ended.append(span.context.span_id)
        super().on_end(span)


@pytest.fixture
def wire_client(monkeypatch):
    # Synthetic credentials only; never consult AWS files or the metadata service.
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    for name in list(os.environ):
        if name.startswith("FI_HIDE_") or name == "FI_PII_REDACTION":
            monkeypatch.delenv(name)
    state = {"wire": b"", "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.amazon.eventstream")
            self.send_header("Content-Length", str(len(state["wire"])))
            self.end_headers()
            self.wfile.write(state["wire"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    tracker = TrackingProcessor(exporter)
    provider.add_span_processor(tracker)
    clients = []

    def invoke(frames, *, consumed=None, privacy="visible"):
        config = TraceConfig()
        if privacy == "env_hidden":
            monkeypatch.setenv("FI_HIDE_INPUTS", "true")
            monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
            config = TraceConfig()
        elif privacy == "env_pii":
            monkeypatch.setenv("FI_PII_REDACTION", "true")
            config = TraceConfig()
        elif privacy == "pii":
            config = TraceConfig(pii_redaction=True)
        elif privacy != "visible":
            config = TraceConfig(
                hide_inputs=privacy in ("inputs", "both"),
                hide_outputs=privacy in ("outputs", "both"),
            )
        state["wire"] = b"".join(frames)
        BedrockInstrumentor().instrument(tracer_provider=provider, config=config)
        client = boto3.session.Session().client(
            "bedrock-agent-runtime",
            region_name="us-east-1",
            aws_access_key_id="synthetic",
            aws_secret_access_key="synthetic",
            endpoint_url=f"http://127.0.0.1:{server.server_port}",
            config=Config(retries={"max_attempts": 0}),
        )
        clients.append(client)
        with provider.get_tracer("test").start_as_current_span("caller") as caller:
            stream = client.invoke_agent(
                agentId="AGENT00001",
                agentAliasId="ALIAS00001",
                sessionId="session001",
                inputText="prompt",
                enableTrace=True,
            )["completion"]
            assert not exporter.get_finished_spans()
            iterator = iter(stream)
            if consumed is None:
                received = list(iterator)
            else:
                received = [next(iterator) for _ in range(consumed)]
                assert not exporter.get_finished_spans()
            stream.close()
            spans = exporter.get_finished_spans()
            stream.close()
            assert exporter.get_finished_spans() == spans
            assert (
                trace.get_current_span().get_span_context() == caller.get_span_context()
            )
        assert sorted(tracker.started) == sorted(tracker.ended)
        assert len(tracker.ended) == len(set(tracker.ended))
        assert not trace.get_current_span().get_span_context().is_valid
        assert state["requests"] == [{"inputText": "prompt", "enableTrace": True}]
        return exporter.get_finished_spans(), received

    yield invoke
    BedrockInstrumentor().uninstrument()
    for client in clients:
        client.close()
    provider.shutdown()
    server.shutdown()
    thread.join(timeout=3)
    server.server_close()


@pytest.mark.parametrize("action", ["INTERVENED", "NONE"])
@pytest.mark.parametrize("early_close", [False, True])
@pytest.mark.parametrize(
    "privacy", ["visible", "inputs", "outputs", "both", "pii", "env_hidden", "env_pii"]
)
def test_guardrail_outcome_assessments_and_privacy(
    wire_client, action, early_close, privacy
):
    guardrail = {
        "traceId": TRACE_ID,
        "action": action,
        "inputAssessments": [
            assessment("input", "BLOCKED" if action == "INTERVENED" else "NONE")
        ],
        "outputAssessments": [
            assessment("output", "BLOCKED" if action == "INTERVENED" else "NONE")
        ],
    }
    spans, received = wire_client(
        [
            model_input_frame(),
            trace_frame("guardrailTrace", guardrail),
            model_output_frame(),
            output_frame(),
        ],
        consumed=2 if early_close else None,
        privacy=privacy,
    )
    # Instrumentation must neither redact nor otherwise alter the application's event.
    assert received[1]["trace"]["trace"]["guardrailTrace"] == guardrail
    assert len(received) == (2 if early_close else 4)
    (guard,) = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.GEN_AI_SPAN_KIND) == "GUARDRAIL"
    ]
    assert guard.name == "Guardrails"
    assert guard.attributes[SpanAttributes.GEN_AI_PROVIDER_NAME] == "aws"
    assert guard.attributes["aws.bedrock.guardrail.action"] == action
    assert guard.status.status_code == (
        StatusCode.ERROR if action == "INTERVENED" else StatusCode.OK
    )
    assert len(spans) == 5  # caller, agent, orchestration, LLM, one guardrail
    agent = next(s for s in spans if s.name == "bedrock_agent.invoke_agent")
    chain = next(s for s in spans if s.name == "orchestrationTrace")
    llm = next(s for s in spans if s.name == "LLM")
    assert guard.parent.span_id == chain.context.span_id
    assert llm.parent.span_id == chain.context.span_id
    assert chain.parent.span_id == agent.context.span_id
    assert all(s.context.trace_id == agent.context.trace_id for s in spans)
    # A guardrail intervention is not a transport failure of invoke_agent.
    assert agent.status.status_code == StatusCode.OK
    if early_close:
        assert SpanAttributes.OUTPUT_VALUE not in llm.attributes
        assert SpanAttributes.OUTPUT_VALUE not in agent.attributes
    else:
        assert llm.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 11
        assert (
            llm.attributes[SpanAttributes.GEN_AI_REQUEST_MODEL]
            == "amazon.nova-lite-v1:0"
        )
        if privacy not in ("outputs", "both", "env_hidden"):
            assert llm.attributes[SpanAttributes.OUTPUT_VALUE] == "model answer"
            assert agent.attributes[SpanAttributes.OUTPUT_VALUE] == "answer"
    serialized = "".join(s.to_json() for s in spans)
    assert "synthetic" not in serialized
    for direction, key in (
        ("input", SpanAttributes.INPUT_VALUE),
        ("output", SpanAttributes.OUTPUT_VALUE),
    ):
        hidden = privacy in (direction + "s", "both", "env_hidden")
        if hidden:
            assert f"PRIVATE-{direction}" not in serialized
            assert guard.attributes[key] == "__REDACTED__"
        else:
            assert f"PRIVATE-{direction}" in guard.attributes[key]
            parsed = json.loads(guard.attributes[key])
            if privacy not in ("pii", "env_pii"):
                assert parsed == {
                    direction + "Assessments": guardrail[direction + "Assessments"]
                }
        if hidden or privacy in ("pii", "env_pii"):
            assert f"{direction}@example.com" not in serialized
        if privacy in ("pii", "env_pii"):
            assert "<EMAIL_ADDRESS>" in guard.attributes[key]


def test_repeated_guardrails_do_not_consume_model_output_or_own_later_spans(
    wire_client,
):
    allowed = trace_frame("guardrailTrace", {"traceId": TRACE_ID, "action": "NONE"})
    blocked = trace_frame(
        "guardrailTrace", {"traceId": TRACE_ID, "action": "INTERVENED"}
    )
    spans, received = wire_client(
        [
            allowed,
            model_input_frame(),
            blocked,
            model_output_frame(),
            allowed,
            output_frame(),
        ]
    )
    guards = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.GEN_AI_SPAN_KIND) == "GUARDRAIL"
    ]
    assert len(guards) == 3
    assert len(spans) == 7
    assert len(received) == 6
    assert sorted(s.status.status_code.name for s in guards) == ["ERROR", "OK", "OK"]
    guard_ids = {s.context.span_id for s in guards}
    assert all(s.parent is None or s.parent.span_id not in guard_ids for s in spans)
    llm = next(s for s in spans if s.name == "LLM")
    assert llm.attributes[SpanAttributes.OUTPUT_VALUE] == "model answer"
    assert llm.attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] == 11


@pytest.mark.parametrize("consumed", [0, 1])
def test_guardrail_only_stream_close_exports_only_consumed_events(
    wire_client, consumed
):
    spans, received = wire_client(
        [
            trace_frame(
                "guardrailTrace", {"traceId": TRACE_ID, "action": "INTERVENED"}
            ),
            output_frame(),
        ],
        consumed=consumed,
    )
    assert len(received) == consumed
    guards = [
        s
        for s in spans
        if s.attributes.get(SpanAttributes.GEN_AI_SPAN_KIND) == "GUARDRAIL"
    ]
    assert len(guards) == consumed
    assert len(spans) == 2 + consumed
    if consumed:
        agent = next(s for s in spans if s.name == "bedrock_agent.invoke_agent")
        assert guards[0].parent.span_id == agent.context.span_id
        assert guards[0].status.status_code == StatusCode.ERROR
