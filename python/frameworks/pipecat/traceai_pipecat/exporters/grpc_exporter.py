"""
gRPC exporter for Pipecat integration with Future AGI.

This module provides a gRPC span exporter that maps Pipecat attributes
to Future AGI conventions.
"""

from typing import Any, Dict, Optional

from .base_exporter import BaseMappedSpanExporter

try:
    # Use FI's gRPC exporter as base
    from fi_instrumentation.otel import GRPCSpanExporter as _FIGRPCSpanExporter
except ImportError:
    _FIGRPCSpanExporter = None


class MappedGRPCSpanExporter(BaseMappedSpanExporter, _FIGRPCSpanExporter):
    """gRPC exporter that maps Pipecat attributes to Future AGI conventions."""

    def __init__(self, *args: Any, **kwargs: Any):
        """Initialize the mapped gRPC exporter."""
        if _FIGRPCSpanExporter is None:
            raise ImportError(
                "fi_instrumentation is not installed. "
                'Please install the fi_instrumentation with: pip install "fi-instrumentation-otel[grpc]"'
            )
        super().__init__(*args, **kwargs)

    def _convert_attributes(self, attributes):
        """Override to use the base class mapping."""
        return super()._convert_attributes(attributes)
