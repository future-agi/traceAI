"""
HTTP exporter for Pipecat integration with Future AGI.

This module provides an HTTP span exporter that maps Pipecat attributes
to Future AGI conventions.
"""

from typing import Any, Dict, Optional

from .base_exporter import BaseMappedSpanExporter

try:
    # Use FI's HTTP exporter as base
    from fi_instrumentation.otel import HTTPSpanExporter as _FIHTTPSpanExporter
except ImportError:
    _FIHTTPSpanExporter = None


class MappedHTTPSpanExporter(BaseMappedSpanExporter, _FIHTTPSpanExporter):
    """HTTP exporter that maps Pipecat attributes to Future AGI conventions."""

    def __init__(self, *args: Any, **kwargs: Any):
        """Initialize the mapped HTTP exporter."""
        if _FIHTTPSpanExporter is None:
            raise ImportError(
                "fi_instrumentation is not installed. "
                'Please install the fi_instrumentation with: pip install "fi-instrumentation-otel"'
            )
        super().__init__(*args, **kwargs)

    def _convert_attributes(self, attributes):
        """Override to use the base class mapping."""
        return super()._convert_attributes(attributes)
