"""
TraceAI LiveKit integration.

This package provides integration between LiveKit and Future AGI's tracing system,
allowing LiveKit applications to send telemetry data with proper attribute mapping.
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
    flush_on_job_shutdown,
)

from .recording import import_completed_egress, record_egress, wrap_egress_client


def bind_session(session, job_context, *, manager, owned_recording_files=False,
                 finalization_timeout=30, capture_policy=None):
    """Opt-in existing recording import; requires the attachment-enabled core."""
    from .attachments import bind_session as bind
    return bind(session, job_context, manager=manager, owned_recording_files=owned_recording_files,
                finalization_timeout=finalization_timeout, capture_policy=capture_policy)


__version__ = "0.2.1"  # x-release-please-version

__all__ = [
    "enable_fi_attribute_mapping",
    "enable_http_attribute_mapping",
    "enable_grpc_attribute_mapping",
    "create_mapped_http_exporter",
    "create_mapped_grpc_exporter",
    "flush_on_job_shutdown",
    "bind_session",
    "import_completed_egress",
    "record_egress",
    "wrap_egress_client",
    # Exporters
    "MappedHTTPSpanExporter",
    "MappedGRPCSpanExporter",
    "BaseMappedSpanExporter",
]
