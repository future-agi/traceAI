"""One-call setup for AG2 1.x tracing to Future AGI.

AG2 has no process-wide middleware registry: ``TelemetryMiddleware`` is
attached per agent (``Agent(middleware=[...])``, ``Agent.add_middleware``) or
per call (``agent.ask(..., middleware=[...])``). ``setup`` therefore attaches
the middleware to the agents you pass it through AG2's public
``add_middleware``. It does not patch or wrap ``Agent``.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import threading
from typing import Any, Dict, Iterable, Optional

from fi_instrumentation.instrumentation.config import TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.sdk.trace import ConcurrentMultiSpanProcessor, TracerProvider

from ._processor import AG2SpanProcessor

logger = logging.getLogger(__name__)

# Set on the wrapper ``_keep_processor_first`` installs, so it wraps once.
_KEEP_FIRST_MARKER = "_traceai_ag2_keeps_processor_first"


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
    processor chain or already has an ``AG2SpanProcessor``. In the latter case
    an explicit ``config`` replaces the installed processor's ``TraceConfig``
    (the most recent call wins) and a warning is logged when it changes;
    ``config=None`` leaves the installed config as it is.

    The processor is prepended to the active multi-processor rather than added
    with ``add_span_processor``: the provider returned by
    ``fi_instrumentation.register()`` treats its exporter as a replaceable
    default and shuts it down on the first ``add_span_processor`` call, and the
    processor must run before the exporter reads the span.

    That same ``add_span_processor`` call, made later by the application,
    would also shut down and drop this processor. So the provider instance's
    ``add_span_processor`` is wrapped once: the processor is taken out of the
    chain for the call (fi's reset never shuts it down) and put back first
    afterwards. Spans ending during that call are not normalized.

    A provider whose active processor is a ``ConcurrentMultiSpanProcessor``
    runs every processor's ``on_end`` in parallel, so an exporter can read a
    span before it is normalized; a warning is logged for it.
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
    with _chain_lock(active):
        existing = tuple(active._span_processors)
        installed = next((p for p in existing if isinstance(p, AG2SpanProcessor)), None)
        if installed is None:
            active._span_processors = (processor,) + existing
    _keep_processor_first(provider, active)
    if installed is None:
        if isinstance(active, ConcurrentMultiSpanProcessor):
            logger.warning(
                "traceai-ag2: the tracer provider runs span processors concurrently "
                "(ConcurrentMultiSpanProcessor), so exporters may read AG2 spans before "
                "AG2SpanProcessor normalizes them. Use the default synchronous processor."
            )
        return True
    if config is not None and installed.update_config(config):
        logger.warning(
            "traceai-ag2: an AG2SpanProcessor was already installed on this provider; "
            "its TraceConfig is now replaced by the one from the latest call: %r",
            config,
        )
    return False


def _chain_lock(active: Any) -> Any:
    lock = getattr(active, "_lock", None)
    # SDK multi-processors always carry a lock.
    return lock if lock is not None else contextlib.nullcontext()  # pragma: no branch


def _keep_processor_first(provider: Any, active: Any) -> None:
    """Wrap ``provider.add_span_processor`` so :class:`AG2SpanProcessor` stays first.

    fi's ``TracerProvider.add_span_processor`` shuts down and clears the whole
    chain on its first call after ``register()`` (``fi_instrumentation/otel.py``
    ``add_span_processor``: ``_default_processor``). The wrapper lifts the AG2
    processor out of the chain for the call, so it is neither shut down nor
    dropped, then puts it back in front of whatever the call left.
    """
    original = getattr(provider, "add_span_processor", None)
    if original is None or getattr(original, _KEEP_FIRST_MARKER, False):
        return

    @functools.wraps(original)
    def add_span_processor(*args: Any, **kwargs: Any) -> Any:
        with _chain_lock(active):
            processors = tuple(active._span_processors)
            ours = tuple(p for p in processors if isinstance(p, AG2SpanProcessor))
            if ours:
                active._span_processors = tuple(p for p in processors if not isinstance(p, AG2SpanProcessor))
        try:
            return original(*args, **kwargs)
        finally:
            if ours:
                with _chain_lock(active):
                    rest = tuple(p for p in active._span_processors if not isinstance(p, AG2SpanProcessor))
                    active._span_processors = ours[:1] + rest

    setattr(add_span_processor, _KEEP_FIRST_MARKER, True)
    try:
        provider.add_span_processor = add_span_processor
    except (AttributeError, TypeError):  # pragma: no cover - providers with __slots__
        logger.warning(
            "traceai-ag2: cannot guard %s.add_span_processor; add span processors "
            "before calling setup(), or AG2 spans may reach exporters un-normalized.",
            type(provider).__name__,
        )


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


# The provider ``setup()`` registered itself (no ``tracer_provider`` given).
# Later calls without one reuse it: a second ``register()`` would give the
# later agents their own provider and ``AG2SpanProcessor``, which never sees
# the earlier agents' spans, so a sub-task rollup would be counted twice.
_registered_provider: Optional[TracerProvider] = None
_register_lock = threading.Lock()


def _is_shut_down(provider: Any) -> bool:
    """``True`` once ``provider.shutdown()`` reached its ``AG2SpanProcessor``."""
    active = getattr(provider, "_active_span_processor", None)
    processors = getattr(active, "_span_processors", ())
    return any(isinstance(p, AG2SpanProcessor) and p._shutdown for p in processors)


def _registered_or_new_provider(project_name: Optional[str]) -> TracerProvider:
    global _registered_provider
    with _register_lock:
        provider = _registered_provider
        if provider is not None and not _is_shut_down(provider):
            registered_name = getattr(getattr(provider, "resource", None), "attributes", {}).get("project_name")
            if project_name is not None and project_name != registered_name:
                logger.warning(
                    "traceai-ag2: project_name %r is ignored; setup() reuses the provider it "
                    "registered earlier for project %r. Pass tracer_provider= to send these "
                    "agents elsewhere.",
                    project_name,
                    registered_name,
                )
            return provider

        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType

        provider = register(project_type=ProjectType.OBSERVE, project_name=project_name)
        _registered_provider = provider
        return provider


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
            When omitted, the first such ``setup`` call runs
            ``register(project_type=OBSERVE, project_name=project_name)``
            (``FI_PROJECT_NAME``, ``FI_API_KEY``, ``FI_SECRET_KEY`` and
            ``FI_BASE_URL`` apply) and later calls without one reuse that
            provider until it is shut down, so every agent shares one
            provider and one ``AG2SpanProcessor``.
        project_name: Used only when ``setup`` registers a provider; ignored,
            with a warning, when it differs from the one already registered.
        capture_content: Forwarded to ``TelemetryMiddleware``. Defaults to
            ``False``, overriding AG2's upstream default of ``True``.
        provider_name, model_name, span_attributes, max_tool_result_chars:
            Forwarded to ``TelemetryMiddleware``. ``span_attributes`` is one
            way to stamp ``session.id`` on every span (AG2 emits no session
            id); traceAI's ``using_session`` context takes precedence.
        config: ``TraceConfig`` applied by the span processor as a second
            content gate. Defaults to ``TraceConfig()`` (reads ``FI_HIDE_*``).
            One processor serves the whole provider: when it is already
            installed, an explicit ``config`` replaces its config (the most
            recent call wins, with a warning when it changes) and
            ``config=None`` keeps the installed one.

    Returns:
        The tracer provider in use. Short scripts call ``force_flush()`` on it
        before exiting.
    """
    if tracer_provider is None:
        tracer_provider = _registered_or_new_provider(project_name)
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
