"""Future AGI tracing plugin for Hermes Agent.

Hermes loads ``register(ctx)`` from the ``hermes_agent.plugins`` entry point
``futureagi`` once the plugin is enabled (``hermes plugins enable futureagi``).
It registers observer callbacks on Hermes' documented plugin hooks and exports
one OpenTelemetry trace per Hermes turn to Future AGI.

Configuration (``$HERMES_HOME/.env`` works; in a multi-profile gateway each
profile's own ``.env`` is used):

- ``FI_API_KEY`` / ``FI_SECRET_KEY``: required. Without them the plugin stays a no-op.
- ``FI_BASE_URL``: collector base URL (default ``https://api.futureagi.com``).
- ``FI_PROJECT_NAME``: Future AGI project (default ``hermes-agent``).
- ``FI_HERMES_CAPTURE_CONTENT``: ``true`` to also export prompts, responses,
  tool arguments, tool results and error text. Off by default.
- ``FI_HERMES_MAX_CHARS``: per-attribute cap on captured text (default 8000).

The plugin never reads Hermes' session files, config or memory: everything it
exports comes from the hook payloads. It installs no signal handlers and does
not touch the global OpenTelemetry provider.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import uuid
from typing import Any, Callable, Dict, Optional

from opentelemetry import trace as trace_api

from traceai_hermes_agent._recorder import HOOKS, HermesSpanRecorder
from traceai_hermes_agent.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_PROJECT_NAME = "hermes-agent"
DEFAULT_BASE_URL = "https://api.futureagi.com"
_TRUE = {"1", "true", "yes", "on"}
# Exits and session teardown wait at most this long for the last spans.
EXPORT_TIMEOUT_SECONDS = 3
FLUSH_TIMEOUT_MILLIS = 2000
# Hooks that may create the provider. on_session_finalize only flushes: it can
# run on the main thread at exit, outside any profile's secret scope.
_BUILD_HOOKS = frozenset(HOOKS) - {"on_session_finalize", "subagent_stop"}


class _NotInScope(Exception):
    """A multi-profile gateway asked for a secret outside any profile's scope."""


def _setting(name: str) -> Optional[str]:
    """Read one setting the way Hermes reads profile secrets.

    In a multi-profile gateway every profile's ``.env`` is kept out of
    ``os.environ`` and served through ``agent.secret_scope``; outside Hermes
    (or in a normal single-profile run) this is ``os.environ``.
    """
    try:
        from agent.secret_scope import UnscopedSecretError, get_secret
    except Exception:
        return os.environ.get(name)
    try:
        return get_secret(name)
    except UnscopedSecretError as exc:
        raise _NotInScope(name) from exc


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in _TRUE


def _int(value: Optional[str], default: int) -> int:
    try:
        return int((value or "").strip() or default)
    except ValueError:
        return default


def build_tracer_provider(*, api_key: str, secret_key: str, base_url: str,
                          project_name: str) -> Any:
    """A private TracerProvider exporting to ``$base_url/tracer/v1/traces``.

    Built directly instead of through ``fi_instrumentation.register()``, which
    installs SIGINT/SIGTERM handlers and reads keys from ``os.environ``:
    neither is acceptable inside the Hermes process. Resource attributes match
    what ``register()`` sends for an observe project.
    """
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource(attributes={
        "project_name": project_name,
        "project_type": "observe",
        "project_version_name": "DEFAULT_PROJECT_VERSION_NAME",
        "project_version_id": str(uuid.uuid4()),
        "eval_tags": json.dumps([]),
        "metadata": json.dumps({}),
        "semantic_convention": "fi",
        "service.name": "hermes-agent",
    })
    exporter = OTLPSpanExporter(
        endpoint=base_url.rstrip("/") + "/tracer/v1/traces",
        headers={"X-Api-Key": api_key, "X-Secret-Key": secret_key},
        timeout=EXPORT_TIMEOUT_SECONDS,
        # The Future AGI collector does not read Content-Encoding.
        compression=Compression.NoCompression,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter, export_timeout_millis=FLUSH_TIMEOUT_MILLIS))
    return provider


class FutureAGIHermesPlugin:
    """Owns the tracer provider and the recorder; built lazily on the first turn hook.

    Lazy so that ``hermes plugins list`` and other commands that only import
    plugins never start an exporter, so that a missing key is reported once,
    and so that in a multi-profile gateway the settings are read inside the
    profile's own secret scope. Hermes creates one plugin instance per profile.
    """

    def __init__(self, tracer_provider: Optional[Any] = None) -> None:
        self._provider = tracer_provider
        self._owns_provider = tracer_provider is None
        self._recorder: Optional[HermesSpanRecorder] = None
        self._disabled = False
        self._lock = threading.Lock()
        self.settings: Dict[str, Any] = {}

    def recorder(self, build: bool = True) -> Optional[HermesSpanRecorder]:
        if self._recorder is not None or self._disabled or not build:
            return self._recorder
        with self._lock:
            if self._recorder is not None or self._disabled:
                return self._recorder
            try:
                self._recorder = self._build()
            except _NotInScope:
                return None  # try again on a hook that runs in a profile's scope
            except Exception:
                logger.warning("traceAI-hermes-agent: tracing disabled, setup failed", exc_info=True)
                self._recorder = None
            if self._recorder is None:
                self._disabled = True
            return self._recorder

    def _build(self) -> Optional[HermesSpanRecorder]:
        capture = _flag(_setting("FI_HERMES_CAPTURE_CONTENT"))
        max_chars = _int(_setting("FI_HERMES_MAX_CHARS"), 8000)
        if self._provider is None:
            api_key, secret_key = _setting("FI_API_KEY"), _setting("FI_SECRET_KEY")
            if not (api_key and secret_key):
                logger.warning(
                    "traceAI-hermes-agent: FI_API_KEY and FI_SECRET_KEY are not set; "
                    "Hermes turns are not exported"
                )
                return None
            self.settings = {
                "base_url": _setting("FI_BASE_URL") or DEFAULT_BASE_URL,
                "project_name": _setting("FI_PROJECT_NAME") or DEFAULT_PROJECT_NAME,
            }
            self._provider = build_tracer_provider(api_key=api_key, secret_key=secret_key,
                                                   **self.settings)
            atexit.register(self.shutdown)
        self.settings.update(capture_content=capture, max_chars=max_chars)
        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider=self._provider)
        return HermesSpanRecorder(tracer, capture_content=capture, max_chars=max_chars)

    def callback(self, hook: str) -> Callable[..., None]:
        method = "on_" + hook if not hook.startswith("on_") else hook
        build = hook in _BUILD_HOOKS
        # A long-lived gateway exports a session as soon as Hermes tears it down.
        flush_after = hook == "on_session_finalize"

        def _callback(**kwargs: Any) -> None:
            try:
                recorder = self.recorder(build=build)
                if recorder is not None:
                    getattr(recorder, method)(**kwargs)
                    if flush_after:
                        self.flush()
            except Exception:  # never let tracing break Hermes
                logger.debug("traceAI-hermes-agent: %s callback failed", hook, exc_info=True)
            return None

        _callback.__name__ = "futureagi_" + hook
        return _callback

    def flush(self) -> None:
        provider = self._provider
        if provider is not None and hasattr(provider, "force_flush"):
            try:
                provider.force_flush(timeout_millis=FLUSH_TIMEOUT_MILLIS)
            except Exception:
                logger.debug("traceAI-hermes-agent: flush failed", exc_info=True)

    def shutdown(self) -> None:
        """Close open spans (marked unfinished) and export them; runs at process exit.

        Bounded: an unreachable collector delays exit by at most a few seconds.
        """
        if self._recorder is not None:
            self._recorder.end_all()
        if self._owns_provider and self._provider is not None and hasattr(self._provider, "shutdown"):
            try:
                self._provider.shutdown()
            except Exception:
                logger.debug("traceAI-hermes-agent: shutdown failed", exc_info=True)
        else:
            self.flush()


def register(ctx: Any, tracer_provider: Optional[Any] = None) -> None:
    """Hermes plugin entry point: register the tracing callbacks on ``ctx``.

    ``tracer_provider`` is for tests and embedders; Hermes calls ``register(ctx)``.
    """
    plugin = FutureAGIHermesPlugin(tracer_provider)
    for hook in HOOKS:
        ctx.register_hook(hook, plugin.callback(hook))


__all__ = ["FutureAGIHermesPlugin", "HermesSpanRecorder", "build_tracer_provider", "register",
           "__version__"]
