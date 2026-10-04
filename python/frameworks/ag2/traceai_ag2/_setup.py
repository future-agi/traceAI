"""One-call setup for AG2 1.x tracing to Future AGI.

AG2 has no process-wide middleware registry: ``TelemetryMiddleware`` is
attached per agent (``Agent(middleware=[...])``, ``Agent.add_middleware``) or
per call (``agent.ask(..., middleware=[...])``). ``setup`` therefore attaches
the middleware to the agents you pass it through AG2's public
``add_middleware``. It does not patch or wrap ``Agent``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import TracerProvider

from ._processor import AG2SpanProcessor

logger = logging.getLogger(__name__)


class _Unset:
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "<unset>"


_UNSET: Any = _Unset()


def create_telemetry_middleware(
    *,
    tracer_provider: Optional[TracerProvider] = None,
    capture_content: bool = False,
    agent_name: Optional[str] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    span_attributes: Optional[Dict[str, str]] = None,
    max_tool_result_chars: Any = _UNSET,
) -> Any:
    """Build AG2's own ``TelemetryMiddleware`` with content capture off.

    Upstream ``TelemetryMiddleware`` defaults ``capture_content=True``; this
    factory defaults it to ``False``. Pass ``capture_content=True`` to store
    prompts, completions, tool arguments and tool results.

    ``provider_name`` is never invented. Leave it ``None`` and AG2 fills it
    from the model response when the client reports one.

    ``max_tool_result_chars`` is forwarded only when given; the keyword exists
    from ag2 1.0.4.

    Use this for agents created after ``setup`` or for per-call middleware::

        agent = Agent("bot", config=cfg, middleware=[create_telemetry_middleware(
            tracer_provider=trace_provider, agent_name="bot")])
    """
    from ag2.middleware.builtin.telemetry import TelemetryMiddleware

    kwargs: Dict[str, Any] = {
        "tracer_provider": tracer_provider,
        "capture_content": bool(capture_content),
        "agent_name": agent_name,
        "provider_name": provider_name,
        "model_name": model_name,
        "span_attributes": dict(span_attributes) if span_attributes else None,
    }
    if max_tool_result_chars is not _UNSET:
        kwargs["max_tool_result_chars"] = max_tool_result_chars
    return TelemetryMiddleware(**kwargs)


def install_span_processor(
    tracer_provider: Optional[TracerProvider] = None,
    *,
    config: Optional[TraceConfig] = None,
) -> bool:
    """Install :class:`AG2SpanProcessor` ahead of the provider's exporters.

    Returns ``True`` when installed, ``False`` when the provider has no span
    processor chain or already has an ``AG2SpanProcessor``.

    The processor is prepended to the active multi-processor rather than added
    with ``add_span_processor``: the provider returned by
    ``fi_instrumentation.register()`` treats its exporter as a replaceable
    default and shuts it down on the first ``add_span_processor`` call, and the
    processor must run before the exporter reads the span.
    """
    provider = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()
    active = getattr(provider, "_active_span_processor", None)
    if active is None or not hasattr(active, "_span_processors"):
        logger.warning(
            "traceai-ag2: tracer provider %s has no span processor chain; pass the "
            "provider returned by fi_instrumentation.register() as tracer_provider=.",
            type(provider).__name__,
        )
        return False

    processor = AG2SpanProcessor(config=config)
    lock = getattr(active, "_lock", None)
    if lock is not None:
        with lock:
            existing = tuple(active._span_processors)
            if any(isinstance(p, AG2SpanProcessor) for p in existing):
                return False
            active._span_processors = (processor,) + existing
    else:  # pragma: no cover - SDK multi-processors always carry a lock
        existing = tuple(active._span_processors)
        if any(isinstance(p, AG2SpanProcessor) for p in existing):
            return False
        active._span_processors = (processor,) + existing
    return True


def _has_telemetry_middleware(agent: Any) -> bool:
    from ag2.middleware.builtin.telemetry import TelemetryMiddleware

    # ``Agent.middleware`` (public, ag2 >= 1.0.2) yields entries whose
    # ``.middleware`` is the factory; ag2 1.0.0/1.0.1 only have the
    # ``_middleware`` list declared on ``PluginTarget``.
    entries: Iterable[Any]
    try:
        entries = [getattr(e, "middleware", e) for e in agent.middleware]
    except Exception:
        entries = list(getattr(agent, "_middleware", ()) or ())
    return any(isinstance(m, TelemetryMiddleware) for m in entries)


def setup(
    *agents: Any,
    tracer_provider: Optional[TracerProvider] = None,
    project_name: Optional[str] = None,
    capture_content: bool = False,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    span_attributes: Optional[Dict[str, str]] = None,
    max_tool_result_chars: Any = _UNSET,
    config: Optional[TraceConfig] = None,
) -> TracerProvider:
    """Register the Future AGI exporter and attach AG2's ``TelemetryMiddleware``.

    Args:
        *agents: AG2 ``Agent`` objects to instrument. Each gets its own
            ``TelemetryMiddleware`` named after ``agent.name`` through
            ``agent.add_middleware``. An agent that already has a
            ``TelemetryMiddleware`` is skipped, so calling ``setup`` twice does
            not duplicate spans.
        tracer_provider: Provider from ``fi_instrumentation.register(...)``.
            When omitted, ``setup`` calls ``register(project_type=OBSERVE,
            project_name=project_name)`` itself (``FI_PROJECT_NAME``,
            ``FI_API_KEY``, ``FI_SECRET_KEY`` and ``FI_BASE_URL`` apply).
        project_name: Used only when ``tracer_provider`` is omitted.
        capture_content: Forwarded to ``TelemetryMiddleware``. Defaults to
            ``False``, overriding AG2's upstream default of ``True``.
        provider_name, model_name, span_attributes, max_tool_result_chars:
            Forwarded to ``TelemetryMiddleware``. ``span_attributes`` is how an
            app stamps ``session.id`` on every span; AG2 emits no session id.
        config: ``TraceConfig`` applied by the span processor as a second
            content gate. Defaults to ``TraceConfig()`` (reads ``FI_HIDE_*``).

    Returns:
        The tracer provider in use. Short scripts call ``force_flush()`` on it
        before exiting.
    """
    if tracer_provider is None:
        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType

        tracer_provider = register(project_type=ProjectType.OBSERVE, project_name=project_name)
    elif project_name is not None:
        logger.warning(
            "traceai-ag2: project_name is ignored when tracer_provider is passed; "
            "set it on fi_instrumentation.register()."
        )

    install_span_processor(tracer_provider, config=config)

    for agent in agents:
        if _has_telemetry_middleware(agent):
            logger.info(
                "traceai-ag2: agent %r already has a TelemetryMiddleware; not adding another.",
                getattr(agent, "name", agent),
            )
            continue
        agent.add_middleware(
            create_telemetry_middleware(
                tracer_provider=tracer_provider,
                capture_content=capture_content,
                agent_name=getattr(agent, "name", None),
                provider_name=provider_name,
                model_name=model_name,
                span_attributes=span_attributes,
                max_tool_result_chars=max_tool_result_chars,
            )
        )
    return tracer_provider
