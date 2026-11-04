import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_temporal import SafeSpanExporter


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capture_content": "false"},
        {"capture_content": 1},
        {"keep_error_text": "false"},
        {"max_content_length": True},
        {"max_content_length": 1.5},
    ],
)
def test_exporter_requires_explicit_typed_opt_in(kwargs):
    with pytest.raises(TypeError):
        SafeSpanExporter(InMemorySpanExporter(), **kwargs)
