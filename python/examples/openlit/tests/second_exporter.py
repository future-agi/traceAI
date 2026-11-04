"""A test-only fixture: another OTLP exporter in the same process, after ``init_tracing()``.

It runs ``init_tracing()`` from ``src/app.py`` as written, then builds a
plain OpenTelemetry OTLP/HTTP span exporter for ``SECOND_COLLECTOR`` with no
headers, as another library in the app might, and exports one span through
it. Not part of the recipe.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: E402
    OTLPSpanExporter,
)
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402

from app import init_tracing  # noqa: E402

init_tracing()
other = TracerProvider()
other.add_span_processor(
    SimpleSpanProcessor(OTLPSpanExporter(endpoint=os.environ["SECOND_COLLECTOR"]))
)
other.get_tracer("second_exporter").start_span("other_library_span").end()
other.shutdown()
