import os
from dotenv import load_dotenv
load_dotenv()

from fi_instrumentation import register, FITracer
from fi_instrumentation.otel import Transport
from fi_instrumentation.fi_types import ProjectType

# Setup OTel via our register function
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name=f"Hack -2",
    transport=Transport.GRPC,
    batch=True
)

tracer = FITracer(trace_provider.get_tracer(__name__))
from opentelemetry.trace.status import Status, StatusCode

for i in range(2):
    with tracer.start_as_current_span(
        f"HTTP-{i}",
        fi_span_kind="llm",
    ) as span:
        span.set_input(f"HTTP batch {i}")

print("done")