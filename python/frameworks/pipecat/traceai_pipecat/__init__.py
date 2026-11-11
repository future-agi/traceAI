"""
TraceAI Pipecat integration.

This package provides integration between Pipecat and Future AGI's tracing system,
allowing Pipecat applications to send telemetry data with proper attribute mapping.
"""

from .exporters import (
    BaseMappedSpanExporter,
    MappedGRPCSpanExporter,
    MappedHTTPSpanExporter,
)
from .integration import (
    create_mapped_grpc_exporter,
    create_mapped_http_exporter,
    enable_fi_attribute_mapping,
    enable_grpc_attribute_mapping,
    enable_http_attribute_mapping,
)

def bind_audio_processor(processor, session_context, *, manager):
    """Check an existing processor without starting recording or retaining PCM."""
    from .attachments import bind_audio_processor as bind
    return bind(processor, session_context, manager=manager)


def create_audio_capture(session_context, *, manager):
    """Check separate recording consent; no recorder until stream support exists."""
    from .attachments import create_audio_capture as create
    return create(session_context, manager=manager)


__version__ = "0.1.2"  # x-release-please-version

__all__ = [
    "bind_audio_processor",
    "create_audio_capture",
    "enable_fi_attribute_mapping",
    "enable_http_attribute_mapping",
    "enable_grpc_attribute_mapping",
    "create_mapped_http_exporter",
    "create_mapped_grpc_exporter",
    # Exporters
    "MappedHTTPSpanExporter",
    "MappedGRPCSpanExporter",
    "BaseMappedSpanExporter",
]
