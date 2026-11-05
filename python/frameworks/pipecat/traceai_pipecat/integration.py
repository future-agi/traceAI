"""Exporter composition and backward-compatible Pipecat mapping setup."""

import logging
from typing import Any, Optional

from fi_instrumentation.otel import Transport
from opentelemetry import trace as trace_api

from .exporters import (
    BaseMappedSpanExporter,
    MappedGRPCSpanExporter,
    MappedHTTPSpanExporter,
    MappedSpanExporter,
)

logger = logging.getLogger(__name__)


def enable_fi_attribute_mapping(transport: Transport = Transport.HTTP) -> bool:
    """Wrap existing FI exporters without reconstructing their configuration.

    Call once at startup, before producing spans. This compatibility helper
    inspects SDK processor internals; new applications should instead pass a
    ``MappedSpanExporter`` to their span processor during provider setup.

    Args:
        transport: The transport protocol whose FI exporters should be mapped.

    Returns:
        True if at least one matching exporter is mapped (including an already
        mapped exporter); False if none can be mapped.
    """
    provider = trace_api.get_tracer_provider()
    active = getattr(provider, "_active_span_processor", None)
    if not active:
        logger.warning("No active span processor found")
        return False

    try:
        if transport == Transport.HTTP:
            from fi_instrumentation.otel import HTTPSpanExporter as exporter_type
        elif transport == Transport.GRPC:
            from fi_instrumentation.otel import GRPCSpanExporter as exporter_type
        else:
            logger.warning("Unsupported exporter transport")
            return False
    except ImportError:
        logger.warning("Required exporter is not installed")
        return False

    mapped_any = False
    for proc in getattr(active, "_span_processors", ()):
        exporter = getattr(proc, "span_exporter", None)
        original = exporter.exporter if isinstance(exporter, MappedSpanExporter) else exporter
        if not isinstance(original, exporter_type):
            continue
        if isinstance(exporter, BaseMappedSpanExporter):
            mapped_any = True
            continue

        try:
            mapped = MappedSpanExporter(exporter)
            # Older SDK batch processors expose a writable span_exporter; newer
            # ones delegate it to a shared batch processor. Preserve both paths.
            if hasattr(proc, "_batch_processor"):
                proc._batch_processor._exporter = mapped
            else:
                proc.span_exporter = mapped
            mapped_any = True
        except Exception as exc:
            # Custom processors may not support replacement. Do not include
            # exception text, which can contain exporter credentials/options.
            logger.warning("Failed to wrap exporter (%s)", type(exc).__name__)

    if not mapped_any:
        logger.warning("No %s exporters found to map", transport.value)
    return mapped_any


def enable_http_attribute_mapping() -> bool:
    """Map existing HTTP exporters at startup; True also means already mapped."""
    return enable_fi_attribute_mapping(transport=Transport.HTTP)


def enable_grpc_attribute_mapping() -> bool:
    """Map existing gRPC exporters at startup; True also means already mapped."""
    return enable_fi_attribute_mapping(transport=Transport.GRPC)


def create_mapped_http_exporter(
    endpoint: Optional[str] = None, headers: Optional[dict] = None, **kwargs: Any
) -> MappedHTTPSpanExporter:
    """Create a mapped HTTP exporter, forwarding all transport options.

    ``endpoint`` and ``headers`` retain FI's environment defaults when omitted.
    Additional options (for example ``timeout``, ``compression``, ``session``
    and TLS settings) are forwarded unchanged to FI's HTTP exporter.
    """
    return MappedHTTPSpanExporter(endpoint=endpoint, headers=headers, **kwargs)


def create_mapped_grpc_exporter(
    endpoint: Optional[str] = None, headers: Optional[dict] = None, **kwargs: Any
) -> MappedGRPCSpanExporter:
    """Create a mapped gRPC exporter, forwarding all transport options.

    ``endpoint`` and ``headers`` retain FI's environment defaults when omitted.
    Additional options (for example ``timeout``, ``compression``, ``insecure``
    and ``credentials``) are forwarded unchanged to FI's gRPC exporter.
    """
    return MappedGRPCSpanExporter(endpoint=endpoint, headers=headers, **kwargs)
