"""Real SDK export-boundary cases that native conventions cannot all emit today."""

import json

from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_strands import StrandsSpanExporter

provider = TracerProvider()
raw = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(raw))
tracer = provider.get_tracer("strands.telemetry.tracer")


def tool_result(status):
    result = {"toolUseId": "call-1", "content": [{"text": "PRIVATE-ERROR"}]}
    if status is not None:
        result["status"] = status
    return {"toolResult": result}


# Missing/invalid status and opaque responses are not evidence of success.
unsafe_messages = [
    {"role": role, "content": [tool_result(status)]}
    for role in ("user", "tool")
    for status in ("error", "", None, False)
] + [
    {"role": "user", "content": [{"toolResult": "PRIVATE-ERROR"}]},
    {"role": "tool", "content": "PRIVATE-ERROR"},
    {"role": "tool", "content": [{"text": "PRIVATE-ERROR"}]},
    {"role": "tool", "parts": [{"type": "text", "content": "PRIVATE-ERROR"}]},
    {
        "role": "user",
        "parts": [
            {"type": "tool_call_response", "id": "call-1", "response": "PRIVATE-ERROR"}
        ],
    },
]
safe_messages = [
    {"role": "user", "content": [{"text": "SAFE-INPUT"}]},
    {"role": "assistant", "parts": [{"type": "text", "content": "SAFE-ANSWER"}]},
    {
        "role": "tool",
        "content": [
            {
                "toolResult": {
                    "toolUseId": "call-2",
                    "status": "success",
                    "content": [{"text": "SAFE-TOOL-RESULT"}],
                }
            }
        ],
    },
]
for direction in ("input", "output"):
    for source in ("attribute", "event", "legacy"):
        for unsafe in unsafe_messages:
            messages = [*safe_messages, unsafe]
            value = json.dumps(messages)
            key = f"gen_ai.{direction}.messages"
            attributes = {"gen_ai.operation.name": "chat"}
            if source == "attribute":
                attributes[key] = value
            elif source == "legacy":
                attributes["gen_ai.prompt" if direction == "input" else "gen_ai.completion"] = value
            with tracer.start_as_current_span("chat", attributes=attributes) as span:
                if source == "event":
                    span.add_event("gen_ai.client.inference.operation.details", {key: value})
            original = raw.get_finished_spans()[-1]
            before = original.to_json()
            for hide in (False, True):
                sink = InMemorySpanExporter()
                adapter = StrandsSpanExporter(
                    sink, capture_content=True, config=TraceConfig(hide_error_details=hide)
                )
                adapter.export([original])
                (projected,) = sink.get_finished_spans()
                payload = projected.to_json()
                assert ("PRIVATE-ERROR" not in payload) == hide, (source, direction, unsafe)
                assert all(text in payload for text in ("SAFE-INPUT", "SAFE-ANSWER", "SAFE-TOOL-RESULT"))
                assert original.to_json() == before
                assert projected.attributes[key] == projected.attributes[f"{direction}.value"]

# Invalid JSON must not become trusted text when history needs redaction. Cover
# both native conventions, legacy callbacks, and an incomplete JSON string.
for direction in ("input", "output"):
    for source in ("attribute", "event", "content", "legacy"):
        for invalid in (
            '[{"toolResult":{"content":"PRIVATE-ERROR',
            '"PRIVATE-ERROR',
            'PRIVATE-ERROR',
        ):
            key = f"gen_ai.{direction}.messages"
            attributes = {"gen_ai.operation.name": "chat"}
            if source == "attribute":
                attributes[key] = invalid
            elif source == "legacy":
                attributes["gen_ai.prompt" if direction == "input" else "gen_ai.completion"] = invalid
            with tracer.start_as_current_span("chat", attributes=attributes) as span:
                if source == "event":
                    span.add_event("gen_ai.client.inference.operation.details", {key: invalid})
                elif source == "content":
                    span.add_event(
                        "gen_ai.user.message" if direction == "input" else "gen_ai.choice",
                        {"content" if direction == "input" else "message": invalid},
                    )
            original = raw.get_finished_spans()[-1]
            before = original.to_json()
            for hide in (False, True):
                sink = InMemorySpanExporter()
                StrandsSpanExporter(
                    sink, capture_content=True, config=TraceConfig(hide_error_details=hide)
                ).export([original])
                (projected,) = sink.get_finished_spans()
                assert ("PRIVATE-ERROR" not in projected.to_json()) == hide, (direction, source)
                assert original.to_json() == before

# Successfully decoded JSON text is not a decode failure, even if the text looks
# like malformed JSON. Do not recursively parse ordinary user/model text.
for content in ('SAFE-TEXT', '[{"toolResult":"SAFE-TEXT', '"SAFE-TEXT'):
    with tracer.start_as_current_span(
        "chat",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.input.messages": json.dumps(content),
        },
    ):
        pass
    sink = InMemorySpanExporter()
    StrandsSpanExporter(
        sink, capture_content=True, config=TraceConfig(hide_error_details=True)
    ).export([raw.get_finished_spans()[-1]])
    assert json.loads(sink.get_finished_spans()[0].attributes["input.value"]) == content

# Tool-span inputs are arguments, not historical tool responses. Successful
# direct outputs carry a reliable native tool status even if their JSON is opaque.
for status_key in ("tool.status", "gen_ai.tool.status"):
    for status in ("success", "error", ""):
        with tracer.start_as_current_span(
            "execute_tool",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                status_key: status,
                "gen_ai.tool.call.result": "DIRECT-RESULT",
            },
        ) as span:
            span.add_event("gen_ai.tool.message", {"content": '{"text":"SAFE-ARGUMENT"}'})
            span.add_event("gen_ai.choice", {"message": "DIRECT-RESULT"})
        sink = InMemorySpanExporter()
        StrandsSpanExporter(
            sink, capture_content=True, config=TraceConfig(hide_error_details=True)
        ).export([raw.get_finished_spans()[-1]])
        (projected,) = sink.get_finished_spans()
        assert "SAFE-ARGUMENT" in projected.to_json()
        assert ("DIRECT-RESULT" in projected.to_json()) == (status == "success")
    if status == "success":
        assert json.loads(projected.attributes["output.value"]) == [
            {"role": "tool", "content": "DIRECT-RESULT"}
        ]

provider.shutdown()
print("PASS tool history privacy")
