"""Future AGI tracing plugin for Hermes Agent.

Hermes loads ``register(ctx)`` from the ``hermes_agent.plugins`` entry point
``futureagi`` once the plugin is enabled (``hermes plugins enable futureagi``).
It registers observer callbacks on Hermes' documented plugin hooks and exports
one OpenTelemetry trace per Hermes turn to Future AGI.

Configuration comes from the shared version-1 Future AGI profile or scoped
``FI_*`` environment variables. Shared setup captures content by default;
``FI_AGENT_CAPTURE_CONTENT=false`` keeps metadata only. The independent Python
env-only path retains its legacy content-off default. The runtime cap is 20,000
characters per field. See the package README for precedence and backend restarts.

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
import time
import uuid
from typing import Any, Callable, Dict, Optional

from opentelemetry import trace as trace_api

from pathlib import Path

from ._config import resolve, permitted, header_value
from ._recorder import HOOKS, HermesSpanRecorder
from .version import __version__

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
                          project_name: str, endpoint: Optional[str] = None,
                          config: Optional[dict] = None) -> Any:
    """A private TracerProvider exporting to ``$base_url/tracer/v1/traces``.

    Built directly instead of through ``fi_instrumentation.register()``, which
    installs SIGINT/SIGTERM handlers and reads keys from ``os.environ``:
    neither is acceptable inside the Hermes process. Resource attributes match
    what ``register()`` sends for an observe project.
    """
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from ._lifecycle import BoundedTracerProvider, ExportBudget

    # Validate both direct callers and shared/environment configuration before
    # the SDK can construct a request or log a credential-bearing exception.
    for value in (api_key, secret_key, project_name):
        header_value(value)
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
    import requests

    budget = ExportBudget()

    class Session(requests.Session):
        def request(self, *args, **kwargs):
            # The SDK logs transport exceptions and HTTP error bodies. Neither
            # is safe: requests can include header values in exception chains,
            # and a collector can echo them. Return only a constant failure.
            try:
                if config is not None and not permitted(config):
                    raise RuntimeError("TRACING_DISABLED")
                kwargs["timeout"] = budget.timeout(kwargs.get("timeout") or EXPORT_TIMEOUT_SECONDS)
                kwargs["allow_redirects"] = False  # never forward FI keys to a redirect
                response = super().request(*args, **kwargs)
                if 200 <= response.status_code < 300:
                    return response
                response.close()
            except Exception:
                pass
            failure = requests.Response()
            failure.status_code = 400  # bounded best-effort export; do not retry
            failure.reason = "EXPORT_FAILED"
            failure._content = b"EXPORT_FAILED"
            return failure

    session = Session()
    session.trust_env = False  # do not read .netrc or ambient proxy credentials
    exporter = OTLPSpanExporter(
        endpoint=endpoint or base_url.rstrip("/") + "/tracer/v1/traces",
        headers={"X-Api-Key": api_key, "X-Secret-Key": secret_key, "X-Project-Name": project_name},
        timeout=EXPORT_TIMEOUT_SECONDS,
        compression=Compression.NoCompression,
        session=session,
    )
    if config is not None:
        from ._delivery import Delivery
        exporter = Delivery(exporter, config)
    provider = BoundedTracerProvider(resource=resource, budget=budget)
    provider.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=1024,
        max_export_batch_size=128, schedule_delay_millis=1000, export_timeout_millis=FLUSH_TIMEOUT_MILLIS))
    return provider


class FutureAGIHermesPlugin:
    """Owns the tracer provider and the recorder; built lazily on the first turn hook.

    Lazy so that ``hermes plugins list`` and other commands that only import
    plugins never start an exporter, so that a missing key is reported once,
    and so that in a multi-profile gateway the settings are read inside the
    profile's own secret scope. Hermes creates one plugin instance per profile.
    """

    def __init__(self, tracer_provider: Optional[Any] = None, *, project: Optional[Path] = None,
                 register_atexit: bool = True) -> None:
        self._provider = tracer_provider
        self._owns_provider = tracer_provider is None
        self._recorder: Optional[HermesSpanRecorder] = None
        self._disabled = False
        self._lock = threading.Lock()
        self.settings: Dict[str, Any] = {}
        self._config: Optional[dict] = None
        self._project = project
        self._register_atexit = register_atexit

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
                logger.warning("traceAI-hermes-agent: tracing disabled, setup failed")
                self._recorder = None
            if self._recorder is None:
                self._disabled = True
            return self._recorder

    def _build(self) -> Optional[HermesSpanRecorder]:
        capture = _flag(_setting("FI_HERMES_CAPTURE_CONTENT"))
        max_chars = _int(_setting("FI_HERMES_MAX_CHARS"), 8000)
        privacy: Dict[str, Any] = {}
        if self._provider is None:
            config = resolve(_setting, project=self._project)
            if config["issues"]:
                logger.warning("traceAI-hermes-agent: PROJECT_CONFIG_IGNORED")
            if not config["enabled"] or not (config["api_key"] and config["secret_key"]):
                return None
            self._config = config
            capture, max_chars = config["capture_content"], config["max_chars"]
            self.settings = {"endpoint": config["endpoint"], "project_name": config["project_name"]}
            self._provider = build_tracer_provider(api_key=config["api_key"], secret_key=config["secret_key"],
                base_url=DEFAULT_BASE_URL, endpoint=config["endpoint"], project_name=config["project_name"], config=config)
            privacy = dict(hide_inputs=config["hide_inputs"], hide_outputs=config["hide_outputs"],
                           hide_errors=config["hide_errors"], secrets=(config["api_key"], config["secret_key"]),
                           user_id=config["user_id"])
            if self._register_atexit:
                atexit.register(self.shutdown)
        self.settings.update(capture_content=capture, max_chars=max_chars)
        tracer = trace_api.get_tracer(__name__, __version__, tracer_provider=self._provider)
        return HermesSpanRecorder(tracer, capture_content=capture, max_chars=max_chars, **privacy)

    def callback(self, hook: str) -> Callable[..., None]:
        method = "on_" + hook if not hook.startswith("on_") else hook
        build = hook in _BUILD_HOOKS
        # A long-lived gateway exports a session as soon as Hermes tears it down.
        flush_after = hook == "on_session_finalize"

        def _callback(**kwargs: Any) -> None:
            try:
                # Stream observers keep only one timestamp, not text. Avoid
                # filesystem reads per token; the exporter still checks the fence.
                if hook != "on_stream_delta" and self._config is not None and not permitted(self._config):
                    return None
                recorder = self.recorder(build=build)
                if recorder is not None:
                    getattr(recorder, method)(**kwargs)
                    if flush_after:
                        self.flush()
            except Exception:  # never let tracing break Hermes
                logger.debug("traceAI-hermes-agent: %s callback failed", hook)
            return None

        _callback.__name__ = "futureagi_" + hook
        return _callback

    def flush(self) -> None:
        provider = self._provider
        if provider is not None and hasattr(provider, "force_flush"):
            try:
                provider.force_flush(timeout_millis=FLUSH_TIMEOUT_MILLIS)
            except Exception:
                logger.debug("traceAI-hermes-agent: flush failed")

    def shutdown(self, deadline: Optional[float] = None) -> None:
        """Close spans and drain within one wall-clock budget, including all batches."""
        deadline = deadline if deadline is not None else time.monotonic() + EXPORT_TIMEOUT_SECONDS
        if self._recorder is not None:
            self._recorder.end_all()
        if self._owns_provider and self._provider is not None:
            try:
                self._provider.shutdown(timeout_millis=max(0, deadline - time.monotonic()) * 1000)
            except Exception:
                logger.debug("traceAI-hermes-agent: shutdown failed")
        # Injected providers belong to the embedder, not to an evicted route.


def register(ctx: Any, tracer_provider: Optional[Any] = None) -> None:
    """Hermes plugin entry point: register the tracing callbacks on ``ctx``.

    ``tracer_provider`` is for tests and embedders; Hermes calls ``register(ctx)``.
    """
    # Current CLI sessions run in a persistent native gateway whose os.getcwd()
    # is NOT the client workspace. Use the native scoped context API instead.
    # A route keeps its provider/destination for its lifetime; concurrent projects
    # cannot change one another's exporter. No new daemon is introduced here.
    routes: Dict[str, FutureAGIHermesPlugin] = {}
    sessions: Dict[str, str] = {}
    inflight: Dict[str, int] = {}
    lock = threading.RLock()

    def shutdown_routes() -> None:
        # Every provider receives the same deadline, including providers whose
        # budget is already exhausted: their remaining HTTP sends are fenced.
        deadline = time.monotonic() + EXPORT_TIMEOUT_SECONDS
        with lock:
            plugins = list(routes.values())
        for plugin in plugins:
            plugin.shutdown(deadline)
    atexit.register(shutdown_routes)

    def dispatch(hook: str) -> Callable[..., None]:
        def observe(**kwargs: Any) -> None:
            try:
                sid = (kwargs.get("child_session_id") if hook in {"subagent_start", "subagent_stop"}
                       else kwargs.get("session_id") or kwargs.get("old_session_id")) or ""
                parent = kwargs.get("parent_session_id") or ""
                retired = None
                with lock:
                    if sid and sid not in sessions and len(sessions) >= 2048:
                        return None
                    key = sessions.get(sid) or sessions.get(parent)
                    if hook == "on_stream_delta" and key is None:
                        return None  # queued/late text cannot reopen a retired route
                    project = None
                    if key is None:
                        surface = ctx.session_surface() if hasattr(ctx, "session_surface") else kwargs.get("platform")
                        cwd = ctx.session_cwd() if surface == "cli" and hasattr(ctx, "session_cwd") else ""
                        project = Path(cwd).resolve() if cwd and Path(cwd).is_absolute() else None
                        key = str(project) if project else "profile"
                    if key not in routes:
                        if hook not in _BUILD_HOOKS:
                            return None
                        if len(routes) >= 8:
                            # The cap bounds concurrent routes, not all workspaces
                            # ever seen by a long-lived gateway. Never retire a
                            # live parent/child session or an executing callback.
                            active = set(sessions.values())
                            idle = next((k for k in routes if k not in active and not inflight.get(k)), None)
                            if idle is None:
                                return None
                            retired = routes.pop(idle)
                            inflight.pop(idle, None)
                        routes[key] = FutureAGIHermesPlugin(tracer_provider, project=project, register_atexit=False)
                    if sid:
                        sessions[sid] = key
                    plugin = routes[key]
                    inflight[key] = inflight.get(key, 0) + 1
                try:
                    if retired is not None:
                        retired.shutdown()
                    plugin.callback(hook)(**kwargs)
                finally:
                    with lock:
                        inflight[key] -= 1
                        if hook in {"on_session_finalize", "subagent_stop"}:
                            sessions.pop(sid, None)
                            old = kwargs.get("old_session_id")
                            if old and sessions.get(old) == key:
                                sessions.pop(old, None)
            except Exception:
                logger.debug("traceAI-hermes-agent: observer routing failed")
            return None
        observe.__name__ = "futureagi_" + hook
        return observe

    for hook in HOOKS:
        ctx.register_hook(hook, dispatch(hook))
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except ImportError:
        VALID_HOOKS = set()
    for hook in ("post_compaction", "post_approval_response", "on_stream_delta"):
        if hook in VALID_HOOKS:
            ctx.register_hook(hook, dispatch(hook))


__all__ = ["FutureAGIHermesPlugin", "HermesSpanRecorder", "build_tracer_provider", "register",
           "__version__"]
