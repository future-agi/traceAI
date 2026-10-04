"""One-call enabler for AG2 Classic's built-in ``autogen.opentelemetry``.

``setup()`` does four things, in order:

1. Checks that the importable ``autogen`` is AG2 Classic 0.14.x (``_guard``).
2. Prepends :class:`AG2ClassicSpanProcessor` to the provider you pass, ahead
   of the exporting processor ``fi_instrumentation.register()`` installed.
3. Calls upstream ``instrument_llm_wrapper`` (global, patches
   ``OpenAIWrapper.create``) with ``capture_messages=capture_content``.
4. Calls upstream ``instrument_agent`` / ``instrument_pattern`` for the agents
   and patterns you pass.

It never creates a ``TracerProvider``, never sets the global provider, and
never constructs an exporter (in particular not the gRPC exporter the
``autogen[tracing]`` extra installs). One provider, the one you pass.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from opentelemetry import trace as trace_api

from ._guard import check_autogen_classic
from ._processor import AG2ClassicSpanProcessor

logger = logging.getLogger(__name__)


def _active_multi_processor(provider: Any) -> Any:
    active = getattr(provider, "_active_span_processor", None)
    if active is None or not hasattr(active, "_span_processors"):
        raise TypeError(
            "traceai_ag2_classic.setup() needs an OpenTelemetry SDK TracerProvider, for example "
            "the one returned by fi_instrumentation.register(project_type=ProjectType.OBSERVE, "
            "project_name=...). It does not create or register a provider itself. Got {0}.".format(
                type(provider).__name__
            )
        )
    return active


def _install_processor(provider: Any, capture_content: bool) -> AG2ClassicSpanProcessor:
    """Prepend our processor so it runs before the exporting processor.

    ``provider.add_span_processor`` is deliberately not used: Future AGI's
    ``TracerProvider`` drops its default exporting processor on the first
    ``add_span_processor`` call after ``register()``, which would lose spans.
    Prepending also guarantees the exporter sees the mapped attributes, and
    that the ``error.type`` -> ERROR promotion runs before Future AGI's
    exporting processor turns every UNSET status into OK
    (``fi_instrumentation/otel.py`` ``BatchSpanProcessor.on_end``).
    """
    active = _active_multi_processor(provider)
    existing = tuple(active._span_processors)
    for processor in existing:
        if isinstance(processor, AG2ClassicSpanProcessor):
            if processor.capture_content != bool(capture_content):
                logger.warning(
                    "traceai-ag2-classic is already installed on this provider with "
                    "capture_content=%s; keeping that setting.",
                    processor.capture_content,
                )
            return processor
    processor = AG2ClassicSpanProcessor(capture_content=capture_content)
    with getattr(active, "_lock", _NullLock()):
        active._span_processors = (processor,) + tuple(active._span_processors)
    return processor


class _NullLock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        return None


class AG2ClassicTracing:
    """Handle returned by :func:`setup`.

    Use it to instrument agents or patterns created after ``setup()``.
    """

    def __init__(
        self,
        *,
        tracer_provider: Any,
        processor: AG2ClassicSpanProcessor,
        autogen_version: str,
        capture_content: bool,
    ) -> None:
        self.tracer_provider = tracer_provider
        self.processor = processor
        self.autogen_version = autogen_version
        self.capture_content = capture_content
        self._original_create: Any = None
        self.owns_llm_wrapper = False

    # Upstream calls ---------------------------------------------------------

    def _instrument_llm_wrapper(self) -> None:
        from autogen.oai.client import OpenAIWrapper
        from autogen.opentelemetry import instrument_llm_wrapper

        current = OpenAIWrapper.create
        if hasattr(current, "__otel_wrapped__"):
            # Upstream returns early when already wrapped (llm_wrapper.py:64-65),
            # so LLM spans keep going to whichever provider wrapped it first.
            logger.warning(
                "autogen OpenAIWrapper.create is already instrumented; LLM spans keep using the "
                "tracer provider and capture setting from that earlier call."
            )
            return
        self._original_create = current
        instrument_llm_wrapper(
            tracer_provider=self.tracer_provider, capture_messages=self.capture_content
        )
        self.owns_llm_wrapper = True

    def instrument_agent(self, agent: Any) -> Any:
        """Call upstream ``instrument_agent`` on ``agent``.

        For a ``GroupChatManager`` it also instruments every agent in its group
        chat and the group chat itself (speaker-selection spans), mirroring what
        upstream ``instrument_pattern`` does for patterns (pattern.py:93-104).
        """
        from autogen.agentchat.groupchat import GroupChat, GroupChatManager
        from autogen.opentelemetry import instrument_agent
        from autogen.opentelemetry.instrumentators.pattern import instrument_groupchat

        provider = self.tracer_provider
        instrument_agent(agent, tracer_provider=provider)
        if isinstance(agent, GroupChatManager):
            groupchat = agent.groupchat
            for member in list(getattr(groupchat, "agents", []) or []):
                instrument_agent(member, tracer_provider=provider)
            instrument_groupchat(groupchat, tracer_provider=provider)
            # GroupChatManager.__init__ registers a shallow copy of the group chat
            # in _reply_func_list; instrument that copy too (pattern.py:98-104).
            for entry in getattr(agent, "_reply_func_list", []) or []:
                config = entry.get("config") if isinstance(entry, dict) else None
                if isinstance(config, GroupChat) and config is not groupchat:
                    instrument_groupchat(config, tracer_provider=provider)
        return agent

    def instrument_pattern(self, pattern: Any) -> Any:
        """Call upstream ``instrument_pattern`` on ``pattern``."""
        from autogen.opentelemetry import instrument_pattern

        return instrument_pattern(pattern, tracer_provider=self.tracer_provider)

    def instrument_a2a_server(self, server: Any) -> Any:
        """Call upstream ``instrument_a2a_server``. Optional; only if you ask.

        Upstream exports it only when its imports succeed (``autogen[a2a]``).
        """
        try:
            from autogen.opentelemetry import instrument_a2a_server  # type: ignore[attr-defined]
        except ImportError as error:
            raise ImportError(
                "autogen.opentelemetry.instrument_a2a_server is not available; install the "
                "`autogen[a2a]` extra to trace an A2A server."
            ) from error
        return instrument_a2a_server(server, tracer_provider=self.tracer_provider)

    # Teardown ---------------------------------------------------------------

    def uninstrument(self) -> None:
        """Restore ``OpenAIWrapper.create`` if this handle patched it.

        The span processor stays on the provider. Upstream patches agents per
        instance and offers no undo, so agents instrumented earlier keep
        emitting spans on this provider; the processor keeps dropping their
        content and moving their aggregate usage off non-LLM spans. Every
        handle on a provider shares that one processor, so removing it here
        would also unfilter the other handles. It is shut down with the
        provider (``provider.shutdown()``).
        """
        if self.owns_llm_wrapper and self._original_create is not None:
            from autogen.oai import client as oai_client_module
            from autogen.oai.client import OpenAIWrapper

            OpenAIWrapper.create = self._original_create
            oai_client_module.OpenAIWrapper.create = self._original_create
            self.owns_llm_wrapper = False
            self._original_create = None


def setup(
    tracer_provider: Optional[Any] = None,
    *,
    agents: Iterable[Any] = (),
    patterns: Iterable[Any] = (),
    capture_content: bool = False,
    instrument_llm: bool = True,
) -> AG2ClassicTracing:
    """Enable AG2 Classic's native tracing on a Future AGI tracer provider.

    Args:
        tracer_provider: The provider returned by ``fi_instrumentation.register``.
            Defaults to the global provider, which must be an SDK provider.
        agents: Agents to pass to upstream ``instrument_agent``.
        patterns: Group patterns to pass to upstream ``instrument_pattern``.
        capture_content: Keep message bodies, tool arguments/results, human
            input and code output on spans. Off by default.
        instrument_llm: Call upstream ``instrument_llm_wrapper`` (LLM spans).

    Returns:
        An :class:`AG2ClassicTracing` handle for agents created later.

    Raises:
        AG2ClassicCompatibilityError: ``autogen`` is not AG2 Classic 0.14.x.
        TypeError: The provider is not an OpenTelemetry SDK provider.
    """
    version = check_autogen_classic()
    provider = tracer_provider if tracer_provider is not None else trace_api.get_tracer_provider()
    processor = _install_processor(provider, capture_content)
    handle = AG2ClassicTracing(
        tracer_provider=provider,
        processor=processor,
        autogen_version=version,
        capture_content=bool(capture_content),
    )
    if instrument_llm:
        handle._instrument_llm_wrapper()
    for agent in agents:
        handle.instrument_agent(agent)
    for pattern in patterns:
        handle.instrument_pattern(pattern)
    return handle
