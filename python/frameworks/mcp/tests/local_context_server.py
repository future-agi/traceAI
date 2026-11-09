"""Real stdio transport consumer used by the context ownership regressions."""
import sys

import anyio
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_mcp import MCPInstrumentor

REMOTE_TRACE = 0x12345678901234567890123456789012
REMOTE_SPAN = 0x1234567812345678


async def main(reason, close):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("context-lifecycle")
    instrumentor = MCPInstrumentor()
    instrumentor.instrument()
    import mcp.server.stdio as stdio

    try:
        with tracer.start_as_current_span("outer") as outer:
            async with stdio.stdio_server() as (reader, writer):
                with anyio.CancelScope() as scope:
                    async for _ in reader:
                        with tracer.start_as_current_span("request") as request:
                            assert request.get_span_context().trace_id == REMOTE_TRACE
                        if reason == "cancel":
                            scope.cancel()
                            await anyio.sleep(0)
                            raise AssertionError("cancellation was not delivered")
                        break
                # Native stdio waits for its output pump on normal exit.
                await writer.aclose()
                if close == "explicit":
                    await reader.aclose()
                    with tracer.start_as_current_span("after_close") as after_close:
                        assert after_close.get_span_context().trace_id == outer.get_span_context().trace_id
            with tracer.start_as_current_span("after_transport") as after:
                assert after.get_span_context().trace_id == outer.get_span_context().trace_id
        spans = {span.name: span for span in exporter.get_finished_spans()}
        assert set(spans) == {"outer", "request", "after_transport"} | (
            {"after_close"} if close == "explicit" else set()
        )  # Propagation must not add spans of its own.
        assert spans["request"].parent.span_id == REMOTE_SPAN
        assert spans["after_transport"].parent.span_id == outer.get_span_context().span_id
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


if __name__ == "__main__":
    anyio.run(main, *sys.argv[1:])
