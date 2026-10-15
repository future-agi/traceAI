"""Future AGI tracing plugin for Hermes Agent.

Hermes loads ``register(ctx)`` from the ``hermes_agent.plugins`` entry point
``futureagi`` once the plugin is enabled (``hermes plugins enable futureagi``).
It registers observer callbacks on Hermes' documented plugin hooks and exports
one OpenTelemetry trace per Hermes turn to Future AGI.

Configuration is read from the environment (``$HERMES_HOME/.env`` works):

- ``FI_API_KEY`` / ``FI_SECRET_KEY``: required. Without them the plugin stays a no-op.
- ``FI_BASE_URL``: collector base URL (default ``https://api.futureagi.com``).
- ``FI_PROJECT_NAME``: Future AGI project (default ``hermes-agent``).
- ``FI_HERMES_CAPTURE_CONTENT``: ``true`` to also export prompts, responses,
  tool arguments and tool results. Off by default.
- ``FI_HERMES_MAX_CHARS``: per-attribute cap on captured text (default 8000).

The plugin never reads Hermes' session files, config or memory: everything it
exports comes from the hook payloads.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import Any, Callable, Optional

from opentelemetry import trace as trace_api

from traceai_hermes_agent._recorder import HOOKS, HermesSpanRecorder
from traceai_hermes_agent.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_PROJECT_NAME = "hermes-agent"
_TRUE = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


class FutureAGIHermesPlugin:
    """Owns the tracer provider and the recorder; built lazily on the first hook.

    Lazy so that ``hermes plugins list`` and other commands that only import
    plugins never start an exporter, and so that a missing key is reported
    once, at the first turn, instead of at import.
    """

    def __init__(self, tracer_provider: Optional[Any] = None) -> None:
        self._provider = tracer_provider
        self._owns_provider = tracer_provider is None
        self._recorder: Optional[HermesSpanRecorder] = None
        self._disabled = False
        self._lock = threading.Lock()

    def recorder(self) -> Optional[HermesSpanRecorder]:
        if self._recorder is not None or self._disabled:
            return self._recorder
        with self._lock:
            if self._recorder is not None or self._disabled:
                return self._recorder
            try:
                self._recorder = self._build()
            except Exception:
                logger.warning("traceAI-hermes-agent: tracing disabled, setup failed", exc_info=True)
                self._recorder = None
            if self._recorder is None:
                self._disabled = True
            return self._recorder

    def _build(self) -> Optional[HermesSpanRecorder]:
        if self._provider is None:
            if not (os.environ.get("FI_API_KEY") and os.environ.get("FI_SECRET_KEY")):
                logger.warning(
                    "traceAI-hermes-agent: FI_API_KEY and FI_SECRET_KEY are not set; "
                    "Hermes turns are not exported"
                )
                return None
            from fi_instrumentation import register
            from fi_instrumentation.fi_types import ProjectType

            self._provider = register(
                project_name=os.environ.get("FI_PROJECT_NAME") or DEFAULT_PROJECT_NAME,
                project_type=ProjectType.OBSERVE,
                # Hermes and other plugins may own the global provider.
                set_global_tracer_provider=False,
                verbose=False,
            )
            atexit.register(self.shutdown)
        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider=self._provider)
        return HermesSpanRecorder(
            tracer,
            capture_content=_env_flag("FI_HERMES_CAPTURE_CONTENT"),
            max_chars=_env_int("FI_HERMES_MAX_CHARS", 8000),
        )

    def callback(self, hook: str) -> Callable[..., None]:
        method = "on_" + hook if not hook.startswith("on_") else hook

        # A long-lived gateway exports a session as soon as Hermes tears it down.
        flush_after = hook == "on_session_finalize"

        def _callback(**kwargs: Any) -> None:
            recorder = self.recorder()
            if recorder is not None:
                getattr(recorder, method)(**kwargs)
                if flush_after:
                    self.flush()
            return None

        _callback.__name__ = "futureagi_" + hook
        return _callback

    def flush(self) -> None:
        provider = self._provider
        if provider is not None and hasattr(provider, "force_flush"):
            try:
                provider.force_flush()
            except Exception:
                logger.debug("traceAI-hermes-agent: flush failed", exc_info=True)

    def shutdown(self) -> None:
        """Close open spans (marked unfinished) and flush; runs at process exit."""
        if self._recorder is not None:
            self._recorder.end_all()
        self.flush()
        if self._owns_provider and self._provider is not None and hasattr(self._provider, "shutdown"):
            try:
                self._provider.shutdown()
            except Exception:
                logger.debug("traceAI-hermes-agent: shutdown failed", exc_info=True)


def register(ctx: Any, tracer_provider: Optional[Any] = None) -> None:
    """Hermes plugin entry point: register the tracing callbacks on ``ctx``.

    ``tracer_provider`` is for tests and embedders; Hermes calls ``register(ctx)``.
    """
    plugin = FutureAGIHermesPlugin(tracer_provider)
    for hook in HOOKS:
        ctx.register_hook(hook, plugin.callback(hook))


__all__ = ["FutureAGIHermesPlugin", "HermesSpanRecorder", "register", "__version__"]
