"""Real OpenInference instrumentors -> Future AGI (OTLP/HTTP, uncompressed). Not run in CI.
usage: python live_e2e.py PROJECT MARKER  (hide flags: OPENINFERENCE_HIDE_* env)"""
import json, os, sys
from openai import OpenAI
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter, Compression
from openinference.instrumentation import OITracer, TraceConfig
from openinference.instrumentation.openai import OpenAIInstrumentor

project, marker = sys.argv[1], sys.argv[2]
tp = TracerProvider(resource=Resource.create({"service.name": "th8336-live", "project_name": project, "project_type": "observe"}))
tp.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
    endpoint=os.environ["FI_BASE_URL"].rstrip("/") + "/v1/traces",
    headers={"X-Api-Key": os.environ["FI_API_KEY"], "X-Secret-Key": os.environ["FI_SECRET_KEY"]},
    compression=Compression.NoCompression)))
OpenAIInstrumentor().instrument(tracer_provider=tp)
tracer = OITracer(tp.get_tracer("th8336"), TraceConfig())
client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), api_key=os.environ["OPENAI_API_KEY"])
MODEL = os.environ.get("E2E_MODEL", "gpt-4o-mini")

@tracer.tool
def lookup_refund_policy(order: str) -> str:
    """Look up the refund policy for an order."""
    return f"policy for {order}: refunds within 30 days {marker}"

@tracer.tool
def broken_tool(order: str) -> str:
    """Always fails."""
    raise RuntimeError(f"upstream refused {marker}")

tools = [{"type": "function", "function": {"name": "lookup_refund_policy", "description": "refund policy",
          "parameters": {"type": "object", "properties": {"order": {"type": "string"}}, "required": ["order"]}}}]
q = f"What is the refund policy for order A-17? {marker}"
with tracer.start_as_current_span("support-agent", openinference_span_kind="agent") as root:
    root.set_input(q)
    with tracer.start_as_current_span("retrieve-docs", openinference_span_kind="retriever") as r:
        r.set_input(q)
        r.set_attribute("retrieval.documents.0.document.content", f"Refunds within 30 days {marker}")
    msgs = [{"role": "user", "content": q}]
    resp = client.chat.completions.create(model=MODEL, messages=msgs, tools=tools,
                                          tool_choice={"type": "function", "function": {"name": "lookup_refund_policy"}})
    m = resp.choices[0].message
    msgs.append(m.model_dump(exclude_none=True))
    for tc in m.tool_calls or []:
        out = lookup_refund_policy(**json.loads(tc.function.arguments))
        msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    final = client.chat.completions.create(model=MODEL, messages=msgs, tools=tools)
    root.set_output(final.choices[0].message.content or "")
# failure cases (separate trace): tool exception + upstream 4xx
with tracer.start_as_current_span("failing-agent", openinference_span_kind="agent") as fr:
    try:
        broken_tool("A-17")
    except RuntimeError:
        pass
    try:
        client.chat.completions.create(model="no-such-model-" + marker[:6], messages=[{"role": "user", "content": marker}])
    except Exception:
        pass
tp.force_flush(); tp.shutdown()
print("done", trace.format_trace_id(root.get_span_context().trace_id))
