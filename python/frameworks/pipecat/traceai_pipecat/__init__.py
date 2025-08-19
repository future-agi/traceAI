"""
TraceAI Pipecat integration.

This package provides integration between Pipecat and Future AGI's tracing system,
allowing Pipecat applications to send telemetry data with proper attribute mapping.
"""

from .integration import (
    install_fi_attribute_mapping,
    install_http_attribute_mapping,
    install_grpc_attribute_mapping,
    create_mapped_http_exporter,
    create_mapped_grpc_exporter,
)
from .exporters import (
    MappedHTTPSpanExporter,
    MappedGRPCSpanExporter,
    BaseMappedSpanExporter,
)

__version__ = "0.1.0"

__all__ = [
    "install_fi_attribute_mapping",
    "install_http_attribute_mapping", 
    "install_grpc_attribute_mapping",
    "create_mapped_http_exporter",
    "create_mapped_grpc_exporter",
    # Exporters
    "MappedHTTPSpanExporter",
    "MappedGRPCSpanExporter",
    "BaseMappedSpanExporter",
]
