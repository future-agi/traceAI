import os
import tempfile
from pathlib import Path

import pytest
from fi_instrumentation.auto import wire
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
(CACHE / "tmp").mkdir(parents=True, exist_ok=True)
os.environ["TMPDIR"] = str(CACHE / "tmp")
# tempfile may have initialized before conftest import.
tempfile.tempdir = str(CACHE / "tmp")


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    wire.disable()
    provider.shutdown()
