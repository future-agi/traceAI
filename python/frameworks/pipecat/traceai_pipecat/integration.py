"""
Integration functions for Pipecat with Future AGI.

This module provides functions to install attribute mapping for Pipecat applications
by updating existing span exporters in the tracer provider.
"""

import logging
from typing import Optional

from fi_instrumentation.otel import Transport
from opentelemetry import trace as trace_api

from .exporters import MappedGRPCSpanExporter, MappedHTTPSpanExporter

logger = logging.getLogger(__name__)


def enable_fi_attribute_mapping(transport: Transport = Transport.HTTP) -> bool:
    """
    Install the mapping exporter by swapping FI's exporter instances.

    This function finds existing span exporters in the global tracer provider
    and replaces them with mapped exporters that convert Pipecat attributes
    to Future AGI conventions.

    Args:
        transport: The transport protocol to match existing exporters (Transport enum)

    Returns:
        True if at least one exporter was swapped; False otherwise.
    """
    provider = trace_api.get_tracer_provider()
    swapped_any = False

    active = getattr(provider, "_active_span_processor", None)
    if not active:
        logger.warning("No active span processor found")
        return False

    processors = getattr(active, "_span_processors", tuple())
    for proc in processors:
        exporter = getattr(proc, "span_exporter", None)
        if exporter is None:
            continue

        try:
            if transport == Transport.HTTP:
                from fi_instrumentation.otel import (
                    HTTPSpanExporter as _FIHTTPSpanExporter,
                )

                if isinstance(exporter, _FIHTTPSpanExporter):
                    endpoint = getattr(exporter, "_endpoint", None)
                    headers = getattr(exporter, "_headers", None)
                    new_exporter = MappedHTTPSpanExporter(
                        endpoint=endpoint, headers=headers
                    )

                    if hasattr(proc, "_batch_processor"):
                        setattr(proc._batch_processor, "_exporter", new_exporter)
                    else:
                        setattr(proc, "span_exporter", new_exporter)

                    swapped_any = True
                    logger.info("Replaced HTTP exporter with mapped HTTP exporter")

            elif transport == Transport.GRPC:
                from fi_instrumentation.otel import (
                    GRPCSpanExporter as _FIGRPCSpanExporter,
                )

                if isinstance(exporter, _FIGRPCSpanExporter):
                    endpoint = getattr(exporter, "_endpoint", None)
                    headers = getattr(exporter, "_headers", None)
                    new_exporter = MappedGRPCSpanExporter(
                        endpoint=endpoint, headers=headers
                    )

                    if hasattr(proc, "_batch_processor"):
                        setattr(proc._batch_processor, "_exporter", new_exporter)
                    else:
                        setattr(proc, "span_exporter", new_exporter)

                    swapped_any = True
                    logger.info("Replaced gRPC exporter with mapped gRPC exporter")

        except ImportError as e:
            logger.warning(f"Failed to import required exporter: {e}")
            continue
        except Exception as e:
            logger.warning(f"Failed to replace exporter: {e}")
            continue

    if not swapped_any:
        logger.warning(f"No {transport.value} exporters found to replace")

    return swapped_any


def enable_http_attribute_mapping() -> bool:
    """
    Install HTTP attribute mapping for Pipecat.

    Convenience function that calls enable_fi_attribute_mapping with HTTP transport.

    Returns:
        True if at least one exporter was swapped; False otherwise.
    """
    return enable_fi_attribute_mapping(transport=Transport.HTTP)


def enable_grpc_attribute_mapping() -> bool:
    """
    Install gRPC attribute mapping for Pipecat.

    Convenience function that calls enable_fi_attribute_mapping with gRPC transport.

    Returns:
        True if at least one exporter was swapped; False otherwise.
    """
    return enable_fi_attribute_mapping(transport=Transport.GRPC)


def create_mapped_http_exporter(
    endpoint: Optional[str] = None, headers: Optional[dict] = None
):
    """
    Create a mapped HTTP exporter for Pipecat.

    This function creates a new HTTP exporter that maps Pipecat attributes
    to Future AGI conventions. It can be used independently without
    modifying existing exporters.

    Args:
        endpoint: The collector endpoint (optional, will use environment defaults)
        headers: Optional headers to include in requests (optional, will use environment defaults)

    Returns:
        A configured MappedHTTPSpanExporter instance
    """
    return MappedHTTPSpanExporter(endpoint=endpoint, headers=headers)


def create_mapped_grpc_exporter(
    endpoint: Optional[str] = None, headers: Optional[dict] = None
):
    """
    Create a mapped gRPC exporter for Pipecat.

    This function creates a new gRPC exporter that maps Pipecat attributes
    to Future AGI conventions. It can be used independently without
    modifying existing exporters.

    Args:
        endpoint: The collector endpoint (optional, will use environment defaults)
        headers: Optional headers to include in requests (optional, will use environment defaults)

    Returns:
        A configured MappedGRPCSpanExporter instance
    """
    return MappedGRPCSpanExporter(endpoint=endpoint, headers=headers)
