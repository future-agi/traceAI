"""Adapt Strands' native OpenTelemetry spans for Future AGI.

Wrap the exporter with StrandsSpanExporter and install one global provider before
creating agents. Legacy setup, callback and attribute helpers remain available.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from traceai_strands._exporter import StrandsSpanExporter

from traceai_strands._instrumentor import (
    configure_strands_tracing,
    StrandsInstrumentor,
    create_traced_agent,
)
from traceai_strands._callback import StrandsCallbackHandler
from traceai_strands._attributes import (
    get_agent_attributes,
    get_tool_attributes,
    get_model_attributes,
)

__all__ = [
    "StrandsSpanExporter",
    "create_traced_agent",
    "configure_strands_tracing",
    "StrandsInstrumentor",
    "StrandsCallbackHandler",
    "get_agent_attributes",
    "get_tool_attributes",
    "get_model_attributes",
]

__version__ = "0.1.1"  # x-release-please-version


def __getattr__(name: str) -> Any:
    # Keep the legacy attribute helpers importable without loading the exporter.
    if name == "StrandsSpanExporter":
        from traceai_strands._exporter import StrandsSpanExporter
        return StrandsSpanExporter
    raise AttributeError(name)
