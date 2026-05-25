"""
Integration functions for Microsoft Agent Framework with Future AGI.

This module provides the public entry point used to wire up FI conventions
on Agent Framework's native OpenTelemetry spans. We register an
:class:`AgentFrameworkSpanProcessor` on a ``TracerProvider`` so that every
Agent Framework span gets the FI-specific attributes added before reaching
any exporter.
"""

import logging
from typing import Optional

from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from .processor import AgentFrameworkSpanProcessor

logger = logging.getLogger(__name__)


def _ensure_native_instrumentation_enabled() -> None:
    """Turn on agent_framework's native OTel emission, only if not already on.

    The framework's ``enable_instrumentation()`` re-reads ``ENABLE_SENSITIVE_DATA``
    from the environment when called with no kwargs, which would override any
    explicit ``enable_sensitive_data=True`` the user set. Guarding on the
    existing flag preserves that user choice.
    """
    try:
        from agent_framework.observability import (
            OBSERVABILITY_SETTINGS,
            enable_instrumentation as _af_enable_instrumentation,
        )
    except ImportError:
        logger.debug(
            "agent-framework is not installed; skipping native instrumentation enable. "
            "Install it with: pip install agent-framework"
        )
        return
    try:
        if not OBSERVABILITY_SETTINGS.enable_instrumentation:
            _af_enable_instrumentation()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(
            "Could not enable agent_framework native instrumentation: %s", e
        )


def enable_fi_attribute_mapping(
    tracer_provider: Optional[TracerProvider] = None,
) -> bool:
    """Install the FI attribute mapping on a tracer provider.

    Adds an :class:`AgentFrameworkSpanProcessor` to ``tracer_provider`` (or to
    the global OTel tracer provider if none is passed). Idempotent: calling
    twice on the same provider only installs one processor.

    Args:
        tracer_provider: The provider to install on. If omitted, the global
            tracer provider is used. Pass the provider returned by
            ``fi_instrumentation.register(...)`` when you kept the FI default
            ``set_global_tracer_provider=False``.

    Returns:
        True if the processor was installed; False if there was no usable
        tracer provider, or if our processor was already installed.
    """
    _ensure_native_instrumentation_enabled()

    provider = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()

    active = getattr(provider, "_active_span_processor", None)
    if active is None:
        logger.warning(
            "Tracer provider %s has no active span processor. "
            "Did you forget to call fi_instrumentation.register(...), or call "
            "register(set_global_tracer_provider=True), or pass the provider "
            "directly to enable_fi_attribute_mapping(tracer_provider=...)?",
            type(provider).__name__,
        )
        return False

    existing = tuple(getattr(active, "_span_processors", ()))
    if any(isinstance(p, AgentFrameworkSpanProcessor) for p in existing):
        return False

    # Prepend to the multi-processor's tuple directly rather than calling
    # ``provider.add_span_processor``: FI's TracerProvider drops its default
    # exporter on the first ``add_span_processor`` call, so the public path
    # would silently lose spans. Prepending also ensures our mutations land
    # before any synchronous downstream processor reads attrs.
    new_processor = AgentFrameworkSpanProcessor()
    try:
        active._span_processors = (new_processor,) + existing
    except AttributeError:
        provider.add_span_processor(new_processor)

    logger.info(
        "Installed AgentFrameworkSpanProcessor on %s alongside %d existing processor(s)",
        type(provider).__name__, len(existing),
    )
    return True
