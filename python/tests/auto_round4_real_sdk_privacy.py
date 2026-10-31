"""Installed-wheel smoke probe; run with an environment containing Anthropic 2.x."""

import json

import anthropic
import httpx2
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import _manager


def main():
    app, fi = InMemorySpanExporter(), InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(app))
    trace.set_tracer_provider(provider)
    _manager.init(tracer_provider=provider, span_exporter=fi, batch=False, instrumentations=["wire"])
    tracer = FITracer(provider.get_tracer("library"), TraceConfig())
    secret = "REAL_ANTHROPIC_REQUEST_KEY_123456"
    payload = {"id": "msg-test", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
               "content": [{"type": "text", "text": secret}], "stop_reason": "end_turn",
               "usage": {"input_tokens": 1, "output_tokens": 1}}
    received = []

    def handle(request):
        received.append(request.headers.get("x-api-key") or request.headers["Authorization"].split(" ", 1)[-1])
        return httpx2.Response(200, json=payload)

    try:
        with anthropic.Anthropic(api_key=secret, base_url="https://api.anthropic.com", http_client=httpx2.Client(
                transport=httpx2.MockTransport(handle))) as client:
            for outer_kind in ("CHAIN", "AGENT"):
                for model_kind in ("LLM", "EMBEDDING", "RERANKER", "GUARDRAIL"):
                    with tracer.start_as_current_span("outer-" + outer_kind,
                            attributes={"gen_ai.span.kind": outer_kind}) as outer:
                        with tracer.start_as_current_span("model-" + model_kind,
                                attributes={"gen_ai.span.kind": model_kind}) as model:
                            response = client.messages.create(model="claude-haiku-4-5", max_tokens=8,
                                                              messages=[{"role": "user", "content": "hi"}])
                            assert response.content[0].text == secret
                            model.set_attribute("output.value", response.content[0].text)
                        outer.set_attribute("output.value", response.content[0].text)
                        outer.add_event("echo", {"exception.message": secret})
            # The context marker must be gone; without a model owner the wire span
            # is emitted as the real SDK-native span's child.
            assert client.messages.create(model="claude-haiku-4-5", max_tokens=8,
                                          messages=[{"role": "user", "content": "hi"}]).content[0].text == secret
        assert _manager.flush(3000)
        assert received == [secret] * 9
        for exporter in (app, fi):
            spans = exporter.get_finished_spans()
            assert len(spans) == 26, [s.name for s in spans]
            assert len([s for s in spans if s.attributes.get("traceai.capture.layer") == "wire"]) == 1
            serialized = json.dumps([{"name": s.name, "attributes": dict(s.attributes),
                                      "status": s.status.description, "resource": dict(s.resource.attributes),
                                      "events": [(e.name, dict(e.attributes)) for e in s.events]} for s in spans])
            assert secret not in serialized, serialized
            wire = next(s for s in spans if s.attributes.get("traceai.capture.layer") == "wire")
            assert next(s.name for s in spans if s.context.span_id == wire.parent.span_id) == "anthropic.messages.create"
        print("Anthropic", anthropic.__version__, "privacy/dedup: 9 unchanged responses; 26 spans per destination; no credential echoes")
    finally:
        _manager.shutdown()
        provider.shutdown()


if __name__ == "__main__":
    main()
