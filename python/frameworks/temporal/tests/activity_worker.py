"""Child-runtime test helper; invoked under the shared heavy-slot helper."""

import asyncio
import json
import signal
import sys
import threading
from pathlib import Path

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.auto import wire
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult
from temporalio.client import Client
from temporalio.worker import Worker

from traceai_temporal import TemporalInterceptor
from workflows import ChildWorkflow, llm_activity


class FileExporter(SpanExporter):
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def export(self, spans):
        with self.lock, open(self.path, "a") as stream:
            for span in spans:
                stream.write(json.dumps({
                    "name": span.name,
                    "trace_id": span.context.trace_id,
                    "span_id": span.context.span_id,
                    "parent": span.parent.span_id if span.parent else None,
                    "attributes": dict(span.attributes),
                }) + "\n")
        return SpanExportResult.SUCCESS


async def main():
    address, queue, output, ready = sys.argv[1:]
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(FileExporter(output)))
    wire.enable(FITracer(provider.get_tracer("wire.child"), config=TraceConfig()), transports=["httpx"])
    client = await Client.connect(address)
    worker = Worker(client, task_queue=queue, workflows=[ChildWorkflow], activities=[llm_activity],
                    interceptors=[TemporalInterceptor(provider)], max_concurrent_activities=2)
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    try:
        async with worker:
            Path(ready).touch()
            await stop.wait()
    finally:
        wire.disable()
        provider.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
