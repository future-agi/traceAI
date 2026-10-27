"""README Option 1, run for real: register(), then configure_strands_tracing(tracer_provider=...).

Run by test_real_export.py in a fresh interpreter, because conftest.py replaces opentelemetry,
fi_instrumentation and strands with mocks for the rest of the test session. Two OTLP/HTTP
collectors on 127.0.0.1 record the spans they receive, with the X-Api-Key each request carried:
one is Future AGI (FI_BASE_URL), the other a second backend.

argv[1] says where Strands' own OTLP exporter is pointed:
    "future-agi"       OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, at register()'s Future AGI traces URL
    "future-agi-base"  OTEL_EXPORTER_OTLP_ENDPOINT, a base URL that resolves to that same URL
    "other"            OTEL_EXPORTER_OTLP_TRACES_ENDPOINT, at the second collector
Options:
    --keys {fi,both,strands}  who carries the Future AGI keys: register() only, through
                              FI_API_KEY/FI_SECRET_KEY (default); register() and Strands' exporter,
                              the same keys also in OTEL_EXPORTER_OTLP_HEADERS; or Strands' exporter
                              only, through configure_strands_tracing(otlp_headers=...)
    --console                 configure_strands_tracing(enable_console_exporter=True)
    --processor-after         the application adds an in-memory span processor afterwards

Prints one line, "SCENARIO_RESULT <json>", with what each collector received.

strands-agents is not a dev dependency. Without it, `strands.telemetry` is a copy of the part of
strands-agents' StrandsTelemetry this package calls: setup_otlp_exporter() wraps the upstream
OTLP/HTTP span exporter in a BatchSpanProcessor and adds it to the provider, and
setup_console_exporter() adds a SimpleSpanProcessor with a ConsoleSpanExporter. With strands-agents
installed, the real StrandsTelemetry is used.
"""

import argparse
import gzip
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, List, Tuple

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

RESULT_PREFIX = "SCENARIO_RESULT "
API_KEY = "test-api-key"
SECRET_KEY = "test-secret-key"


def _collector() -> Tuple[ThreadingHTTPServer, List[Tuple[str, str]]]:
    """An OTLP/HTTP collector recording (span name, X-Api-Key or "") for every span it receives."""
    received: List[Tuple[str, str]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            api_key = self.headers.get("X-Api-Key", "")
            received.extend(
                (span.name, api_key)
                for resource_spans in request.resource_spans
                for scope_spans in resource_spans.scope_spans
                for span in scope_spans.spans
            )
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, received


def _install_strands_telemetry_stand_in() -> bool:
    """Use strands-agents when installed; otherwise install the copy described above."""
    try:
        import strands.telemetry  # noqa: F401

        return True
    except ImportError:
        pass

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    class StrandsTelemetry:
        def __init__(self, tracer_provider: Any = None) -> None:
            self.tracer_provider = tracer_provider

        def setup_otlp_exporter(self, **kwargs: Any) -> "StrandsTelemetry":
            self.tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**kwargs)))
            return self

        def setup_console_exporter(self, **kwargs: Any) -> "StrandsTelemetry":
            self.tracer_provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter(**kwargs))
            )
            return self

    strands = types.ModuleType("strands")
    telemetry = types.ModuleType("strands.telemetry")
    telemetry.StrandsTelemetry = StrandsTelemetry
    strands.telemetry = telemetry
    sys.modules["strands"] = strands
    sys.modules["strands.telemetry"] = telemetry
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["future-agi", "future-agi-base", "other"])
    parser.add_argument("--keys", choices=["fi", "both", "strands"], default="fi")
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--processor-after", action="store_true")
    args = parser.parse_args()

    future_agi, future_agi_received = _collector()
    other, other_received = _collector()
    future_agi_url = f"http://127.0.0.1:{future_agi.server_address[1]}"

    for name in list(os.environ):
        if name.startswith(("OTEL_", "_OTEL_", "FI_")):
            del os.environ[name]
    os.environ["FI_BASE_URL"] = future_agi_url
    if args.keys in ("fi", "both"):
        os.environ["FI_API_KEY"] = API_KEY
        os.environ["FI_SECRET_KEY"] = SECRET_KEY
    if args.keys == "both":
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"X-Api-Key={API_KEY},X-Secret-Key={SECRET_KEY}"
    if args.mode == "future-agi":
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = f"{future_agi_url}/tracer/v1/traces"
    elif args.mode == "future-agi-base":
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{future_agi_url}/tracer/"
    else:
        os.environ["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = (
            f"http://127.0.0.1:{other.server_address[1]}/v1/traces"
        )

    real_strands = _install_strands_telemetry_stand_in()

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from traceai_strands import configure_strands_tracing

    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="traceai-strands-test",
        set_global_tracer_provider=False,
        verbose=False,
    )
    # fi-instrumentation-otel 1.1.0 and earlier mark register()'s exporter as a default that the
    # next add_span_processor() call replaces (fixed by TH-8727).
    register_marks_its_exporter = bool(getattr(provider, "_default_processor", False))

    configure_strands_tracing(
        tracer_provider=provider,
        otlp_headers=(
            {"X-Api-Key": API_KEY, "X-Secret-Key": SECRET_KEY} if args.keys == "strands" else None
        ),
        enable_console_exporter=args.console,
        enable_metrics=False,
    )
    span_processors = len(provider._active_span_processor._span_processors)

    added_after = InMemorySpanExporter()
    if args.processor_after:
        provider.add_span_processor(SimpleSpanProcessor(added_after))

    tracer = provider.get_tracer("traceai-strands-test")
    for i in range(3):
        with tracer.start_as_current_span(f"span-{i}"):
            pass
    provider.force_flush(timeout_millis=10_000)

    print(
        RESULT_PREFIX
        + json.dumps(
            {
                "real_strands": real_strands,
                "register_marks_its_exporter": register_marks_its_exporter,
                "span_processors": span_processors,
                "future_agi": sorted(name for name, _ in future_agi_received),
                "future_agi_with_keys": sorted(name for name, key in future_agi_received if key),
                "other": sorted(name for name, _ in other_received),
                "added_after": sorted(s.name for s in added_after.get_finished_spans()),
            }
        ),
        flush=True,
    )
    # register() installed signal handlers and an exit-time shutdown; neither is needed here.
    os._exit(0)


if __name__ == "__main__":
    main()
