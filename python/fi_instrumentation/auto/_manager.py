"""Turns on every installed integration and the wire layer, and reports what each one did."""

import atexit
import importlib
import importlib.machinery
import importlib.util
import logging
import os
import sys
import threading
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, Sampler, TraceIdRatioBased

# The one S1 boundary lives in core so manual routes use the same implementation.
from fi_instrumentation._export_privacy import PrivacyExporter as _PrivacyExporter
from fi_instrumentation.auto import _registry
from fi_instrumentation.auto._config import AutoConfig, resolve
from fi_instrumentation.auto._registry import INSTRUMENTOR, MANUAL, PLUGIN, SERVICE, SETUP, Integration

logger = logging.getLogger("fi_instrumentation.auto")

INSTRUMENTED = "instrumented"
PENDING = "pending"
NOT_INSTALLED = "not_installed"
MISSING_INSTRUMENTOR = "missing_instrumentor"
DISABLED = "disabled"
STATUS_MANUAL = "manual"
FAILED = "failed"


@dataclass
class IntegrationStatus:
    name: str
    status: str
    category: str
    package: str
    detail: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"name": self.name, "status": self.status, "category": self.category,
                "package": self.package, "detail": self.detail}


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.init_lock = threading.RLock()
        self.provider: Any = None
        self.trace_config: Any = None
        self.settings: Optional[AutoConfig] = None
        self.statuses: Dict[str, IntegrationStatus] = {}
        self.hooked: set = set()
        self.enabling: set = set()
        self.owned_provider = False
        self.overlays: Dict[Any, Any] = {}
        self.cleanups: Dict[str, Any] = {}
        self.initialized = False
        self.stopped = False


_state = _State()


def module_available(dotted: str) -> bool:
    """True when ``dotted`` can be imported, without importing it or its parents."""
    if dotted in sys.modules:
        return True
    parts = dotted.split(".")
    try:
        spec = importlib.machinery.PathFinder.find_spec(parts[0])
    except (ImportError, ValueError):
        return False
    for part in parts[1:]:
        if spec is None or not spec.submodule_search_locations:
            return False
        try:
            spec = importlib.machinery.PathFinder.find_spec(part, list(spec.submodule_search_locations))
        except (ImportError, ValueError):
            return False
    return spec is not None


def _wire_note(integration: Integration, wire_on: bool) -> str:
    if integration.wire and wire_on:
        return f"; the wire layer still traces its {', '.join(integration.wire)} calls"
    return ""


def _enable(integration: Integration, provider: Any, config: Any) -> Tuple[IntegrationStatus, Any]:
    """Install against a configuration snapshot; retain cleanup only for our own patches."""
    base = IntegrationStatus(integration.name, INSTRUMENTED, integration.category, integration.package)
    cleanup = None
    try:
        module = importlib.import_module(integration.module)
        if integration.kind == INSTRUMENTOR:
            instrumentor = getattr(module, integration.entry)()
            if getattr(instrumentor, "is_instrumented_by_opentelemetry", False):
                base.detail = "already instrumented"
                return base, None
            from fi_instrumentation.auto._sampling import agent_provider

            kwargs: Dict[str, Any] = {"tracer_provider": agent_provider(provider)}
            if integration.module.startswith("traceai_"):
                kwargs["config"] = config
            instrumentor.instrument(**kwargs)
            if not getattr(instrumentor, "is_instrumented_by_opentelemetry", True):
                base.status = FAILED
                base.detail = _dependency_conflict(instrumentor) or "instrument() did not complete"
            else:
                cleanup = instrumentor.uninstrument
        else:
            base.detail, cleanup = _setup_integration(integration, module, provider, config)
    except Exception as error:
        base.status = FAILED
        base.detail = f"{type(error).__name__}: {error}"
        logger.warning("traceAI: could not enable %s: %s", integration.name, error)
    return base, cleanup


def _exporter_slots(processors: Any) -> List[Tuple[Any, str, Any]]:
    slots = []
    for processor in processors:
        processor = getattr(processor, "_delegate", processor)
        batch = getattr(processor, "_batch_processor", None)
        if batch is not None and hasattr(batch, "_exporter"):
            slots.append((batch, "_exporter", batch._exporter))
        elif hasattr(processor, "span_exporter"):
            slots.append((processor, "span_exporter", processor.span_exporter))
    return slots


class _SetupLock:
    """Reentrant setup view of the SDK lock, shared with app processor additions."""

    def __init__(self, lock: Any) -> None:
        self.lock = lock or threading.RLock()
        self.owner: Any = None
        self.depth = 0

    def acquire(self, *args: Any, **kwargs: Any) -> bool:
        owner = threading.get_ident()
        if self.owner != owner and not self.lock.acquire(*args, **kwargs):
            return False
        self.owner, self.depth = owner, self.depth + 1
        return True

    def release(self) -> None:
        if self.owner != threading.get_ident():
            raise RuntimeError("cannot release unowned setup lock")
        self.depth -= 1
        if not self.depth:
            self.owner = None
            self.lock.release()

    def __enter__(self) -> Any:
        self.acquire()
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()


class _SetupChain:
    """Observe explicit setup writes, never infer ownership from global snapshots."""

    def __init__(self, active: Any, added: list) -> None:
        self.active, self.added = active, added
        self.observed = tuple(active._span_processors)
        self._lock = _SetupLock(getattr(active, "_lock", None))

    def __getattr__(self, key: str) -> Any:
        return getattr(self.active, key)

    @property
    def _span_processors(self) -> tuple:
        self.observed = tuple(self.active._span_processors)
        return self.observed

    @_span_processors.setter
    def _span_processors(self, value: tuple) -> None:
        with self._lock:
            before = tuple(self.active._span_processors)
            self.added.extend(p for p in value if not any(p is old for old in before))
            # Legacy adapters prepend using a previously-read tuple without the SDK
            # lock. Preserve concurrent additions they could not have observed.
            unseen = tuple(p for p in before if not any(p is old for old in self.observed)
                           and not any(p is new for new in value))
            self.active._span_processors = tuple(value) + unseen


class _SetupProvider(wrapt.ObjectProxy):
    def __init__(self, provider: Any, chain: Any) -> None:
        super().__init__(provider)
        self._self_chain = chain

    @property
    def _active_span_processor(self) -> Any:
        return self._self_chain

    def get_tracer(self, *args: Any, **kwargs: Any) -> Any:
        from fi_instrumentation.auto._sampling import agent_tracer

        return agent_tracer(self.__wrapped__.get_tracer(*args, **kwargs))


def _setup_integration(integration: Integration, module: Any, provider: Any, config: Any) -> Tuple[str, Any]:
    """Retain setup handles and undo only processor/exporter changes this setup made."""
    active = getattr(provider, "_active_span_processor", None)
    before = tuple(getattr(active, "_span_processors", ()))
    slots = _exporter_slots(before)
    added: list = []
    owner = threading.get_ident()
    observing = True
    original_add = getattr(active, "add_span_processor", None)
    original_slot = vars(active).get("add_span_processor") if active is not None else None

    def add(processor: Any) -> Any:
        result = original_add(processor)
        if observing and threading.get_ident() == owner:
            added.append(processor)
        return result

    # This also observes legacy adapters that get the actual provider globally.
    # Other threads still call the original method, without becoming ours.
    if original_add is not None:
        active.add_span_processor = add
    failure = None
    result = ""
    try:
        setup_provider = _SetupProvider(provider, _SetupChain(active, added)) if active is not None else provider
        result = _registry.SETUPS[integration.entry](module, setup_provider, config)
    except Exception as error:
        failure = error
    finally:
        observing = False
        if original_add is not None and vars(active).get("add_span_processor") is add:
            if original_slot is None:
                del active.add_span_processor
            else:
                active.add_span_processor = original_slot
    # Legacy mapping adapters may replace an exporter. Keep the sanitizer outside
    # the replacement, including when the provider was registered by the application.
    for slot_owner, key, old in slots:
        current = getattr(slot_owner, key)
        if current is not old and not isinstance(current, _PrivacyExporter):
            setattr(slot_owner, key, _PrivacyExporter(current, config))
    replacements = [(owner, key, old, getattr(owner, key)) for owner, key, old in slots
                    if getattr(owner, key) is not old]
    callback = getattr(result, "cleanup", None)

    def cleanup() -> None:
        if callback is not None:
            callback()
        # Upstream has no public remove_span_processor. Preserve app processors
        # installed later and never invoke arbitrary shutdown code under its lock.
        def detach(blocking: bool = False) -> bool:
            lock = getattr(active, "_lock", None)
            if lock is not None and not lock.acquire(blocking=blocking):
                return False
            try:
                current = tuple(getattr(active, "_span_processors", ()))
                removed = [p for p in current if any(p is ours for ours in added)]
                if removed:
                    active._span_processors = tuple(p for p in current if not any(p is r for r in removed))
            finally:
                if lock is not None:
                    lock.release()
            for processor in removed:
                _bounded_call(processor.shutdown, 1, threading.Lock())
            return True

        if added and not detach():
            _bounded_call(lambda: detach(blocking=True), 1, threading.Lock())
        for owner, key, old, replacement in replacements:
            if getattr(owner, key) is replacement:
                setattr(owner, key, old)
                _bounded_call(replacement.shutdown, 1, threading.Lock())

    if failure is not None:
        cleanup()
        raise failure
    return str(result), cleanup


def _dependency_conflict(instrumentor: Any) -> Optional[str]:
    try:
        from opentelemetry.instrumentation.dependencies import get_dependency_conflicts

        conflict = get_dependency_conflicts(instrumentor.instrumentation_dependencies())
        return f"unsupported library version: {conflict}" if conflict else None
    except Exception:
        return None


def _cleanup(callback: Any, name: str) -> None:
    if callback is not None:
        try:
            callback()
        except Exception:
            logger.debug("traceAI: could not disable %s", name, exc_info=True)


def _on_import(integration: Integration) -> None:
    _reconcile(integration)


def _reconcile(integration: Integration) -> None:
    """Install each selected integration once; later calls are strictly additive."""
    state = _state
    with state.lock:
        current = state.statuses.get(integration.name)
        if (state.stopped or current is None or current.status != PENDING
                or integration.name in state.enabling):
            return
        state.enabling.add(integration.name)
        provider, config = state.provider, state.trace_config
    try:
        result, cleanup = _enable(integration, provider, config)
        with state.lock:
            stopped = state.stopped
            if stopped:
                result.status, result.detail = DISABLED, "agent shut down"
            elif cleanup is not None:
                state.cleanups[integration.name] = cleanup
            state.statuses[integration.name] = result
        # Shutdown either owns the published callback or the installer owns it,
        # never both. There is no configuration generation or retry/rollback loop.
        if stopped:
            _cleanup(cleanup, integration.name)
    finally:
        with state.lock:
            state.enabling.discard(integration.name)


def _plan(integration: Integration, settings: AutoConfig, lazy: bool) -> Tuple[IntegrationStatus, Optional[str], List[str]]:
    """Decide what to do with an integration without doing it.

    Returns the status to report, the action (``"now"``, ``"hook"`` or None) and the
    installed import targets.
    """
    status = IntegrationStatus(integration.name, NOT_INSTALLED, integration.category, integration.package)
    installed = [t for t in integration.targets if module_available(t)]
    if not installed:
        return status, None, installed
    if not settings.is_selected(integration.name, integration.default):
        status.status = DISABLED
        status.detail = integration.note or "disabled by configuration"
        return status, None, installed
    if integration.kind in (MANUAL, SERVICE):
        status.status, status.detail = STATUS_MANUAL, integration.note
        return status, None, installed
    if not module_available(integration.module):
        status.status = MISSING_INSTRUMENTOR
        status.detail = f"pip install {integration.package}" + _wire_note(integration, settings.wire_enabled())
        return status, None, installed
    if integration.kind == PLUGIN:
        status.status, status.detail = INSTRUMENTED, integration.note
        return status, None, installed
    status.status, status.detail = PENDING, f"enables when {installed[0]} is imported"
    if not lazy or any(t in sys.modules for t in installed):
        return status, "now", installed
    return status, "hook", installed


def auto_instrument(
    *,
    tracer_provider: Any = None,
    config: Any = None,
    instrumentations: Optional[Iterable[str]] = None,
    disabled_instrumentations: Optional[Iterable[str]] = None,
    wire: Optional[bool] = None,
    lazy: bool = True,
    settings: Optional[AutoConfig] = None,
) -> Dict[str, str]:
    """Enable every installed traceAI integration, plus the wire layer.

    Additive: later calls never disable, replace or reconfigure active integrations.
    Integrations whose library is installed but not yet
    imported are enabled on first import (``pending``), so unused libraries cost nothing.

    Args:
        tracer_provider: Provider to trace with. Defaults to the global provider.
        config: ``TraceConfig`` for masking. Defaults to one read from the environment.
        instrumentations: Only enable these integrations (names from ``report()``).
        disabled_instrumentations: Never enable these. ``"wire"`` disables the wire layer.
        wire: Turn the wire layer on or off, overriding the environment.
        lazy: Wait for the first import instead of importing libraries now.

    Returns:
        Integration name mapped to its status, as in ``report()``.
    """
    from fi_instrumentation import TraceConfig

    overrides: Dict[str, Any] = {}
    if instrumentations is not None:
        overrides["instrumentations"] = list(instrumentations)
    if disabled_instrumentations is not None:
        overrides["disabled_instrumentations"] = list(disabled_instrumentations)
    if wire is not None:
        overrides["wire_capture"] = wire
    if settings is None:
        settings = resolve(overrides, trace_config=config)
    elif overrides:
        # An explicit configuration is the base, not permission to reread the
        # environment and discard its exclusions, credentials or safety limits.
        from fi_instrumentation.auto._config import _as_list

        values = {key: _as_list(value) if key.endswith("instrumentations") else value
                  for key, value in overrides.items()}
        settings = replace(settings, **values,
                           sources={**settings.sources, **{key: "argument" for key in overrides}})
    actions: List[Tuple[Integration, str, List[str]]] = []
    with _state.lock:
        if _state.stopped:
            return {name: s.status for name, s in _state.statuses.items()}
        if _state.provider is None:
            _state.provider = tracer_provider or trace_api.get_tracer_provider()
            _state.trace_config = config if config is not None else settings.trace_config or TraceConfig()
            _state.settings = settings
        for integration in _registry.ALL:
            current = _state.statuses.get(integration.name)
            if current is not None and current.status in (INSTRUMENTED, PENDING):
                continue
            status, action, installed = _plan(integration, settings, lazy)
            _state.statuses[integration.name] = status
            actions.append((integration, action, installed))
        _enable_wire(settings)

    for integration, action, installed in actions:
        if action != "hook":
            _reconcile(integration)
        elif integration.name not in _state.hooked:
            _state.hooked.add(integration.name)
            for target in installed:
                wrapt.register_post_import_hook(lambda _module, i=integration: _on_import(i), target)
    with _state.lock:
        return {name: s.status for name, s in _state.statuses.items()}


def _enable_wire(settings: AutoConfig) -> None:
    from fi_instrumentation import FITracer
    from fi_instrumentation.auto import wire as wire_layer
    from fi_instrumentation.instrumentation.version import __version__

    disabled = set(settings.disabled_instrumentations)
    names = [n for n in wire_layer.TRANSPORTS if n not in disabled and f"wire.{n}" not in disabled]
    if not settings.wire_enabled() or not names:
        for name in wire_layer.TRANSPORTS:
            _state.statuses.setdefault(f"wire.{name}", IntegrationStatus(f"wire.{name}", DISABLED, "wire", "fi-instrumentation-otel",
                                                                "disabled by configuration"))
        return
    tracer = FITracer(trace_api.get_tracer("fi_instrumentation.auto.wire", __version__, _state.provider),
                      config=_state.trace_config)
    statuses = wire_layer.enable(tracer, transports=names, max_body_bytes=settings.wire_max_body_bytes)
    for name in names:
        status = statuses.get(name, DISABLED)
        detail = ""
        if status.startswith("failed"):
            status, detail = FAILED, status.split(":", 1)[1].strip()
        elif status == PENDING:
            detail = f"hooks {name} when it is imported"
        _state.statuses[f"wire.{name}"] = IntegrationStatus(f"wire.{name}", status, "wire", "fi-instrumentation-otel", detail)


def plan(settings: Optional[AutoConfig] = None) -> List[IntegrationStatus]:
    """What ``init()`` would enable in this environment, without enabling anything."""
    from fi_instrumentation.auto import wire as wire_layer

    settings = settings or resolve()
    statuses = [_plan(i, settings, lazy=True)[0] for i in _registry.ALL]
    disabled = set(settings.disabled_instrumentations)
    for name, status in wire_layer.transport_statuses().items():
        if not settings.wire_enabled() or name in disabled or f"wire.{name}" in disabled:
            status, detail = DISABLED, "disabled by configuration"
        else:
            detail = "" if status == NOT_INSTALLED else f"hooks {name} when it is imported"
            status = PENDING if status != NOT_INSTALLED else status
        statuses.append(IntegrationStatus(f"wire.{name}", status, "wire", "fi-instrumentation-otel", detail))
    return sorted(statuses, key=lambda s: (s.category, s.name))


def report() -> List[IntegrationStatus]:
    """Current status of every integration, refreshed for libraries imported since startup."""
    from fi_instrumentation.auto import wire as wire_layer

    with _state.lock:
        live = wire_layer.transport_statuses()
        for name, status in live.items():
            entry = _state.statuses.get(f"wire.{name}")
            if entry is not None and entry.status == PENDING and status == INSTRUMENTED:
                entry.status, entry.detail = INSTRUMENTED, ""
        return sorted(_state.statuses.values(), key=lambda s: (s.category, s.name))


def format_report(statuses: Optional[List[IntegrationStatus]] = None, include_absent: bool = False) -> str:
    statuses = report() if statuses is None else statuses
    rows = [s for s in statuses if include_absent or s.status != NOT_INSTALLED]
    if not rows:
        return "traceAI: no supported libraries found in this environment."
    width = max(len(s.name) for s in rows)
    lines = ["traceAI integrations:"]
    for s in rows:
        detail = f"  {s.detail}" if s.detail else ""
        lines.append(f"  {s.name.ljust(width)}  {s.status:<20}{detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Provider setup
# ---------------------------------------------------------------------------


class _ResourceOverlayProcessor(SpanProcessor):
    """Forwards spans of an existing SDK provider to Future AGI with the project resource.

    Used when the application already configured OpenTelemetry: its exporters keep working
    and Future AGI receives the same spans, tagged with the project it needs for routing.
    """

    def __init__(self, delegate: SpanProcessor, resource: Resource,
                 config: Any = None) -> None:
        self._traceai_route = True
        self._delegate = delegate
        self._resource = resource
        self.config = config
        self.enabled = True
        self._operation = threading.Lock()
        self._shutdown_operation = threading.Lock()
        self._closed = False
        self.sampler: Optional[Sampler] = None
        self.mappers: list = []
        from weakref import WeakValueDictionary

        self._live_spans: Any = WeakValueDictionary()

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        # Retain only weak sampling bookkeeping, never request credentials.
        self._live_spans[(span.context.trace_id, span.context.span_id)] = span
        span._traceai_route_owner = self
        if self.sampler is not None and not hasattr(span, "_traceai_route_sampled"):
            from fi_instrumentation.auto._sampling import route_sample

            span._traceai_route_sampled = route_sample(
                self, parent_context, span.context.trace_id, span.name, span.kind,
                span.attributes, span.links)

    def on_end(self, span: ReadableSpan) -> None:
        if not self.enabled:
            return
        from fi_instrumentation import TraceConfig
        from fi_instrumentation.auto.wire._privacy import export_span

        try:
            live = self._live_spans.pop((span.context.trace_id, span.context.span_id), None)
            if not getattr(live, "_traceai_route_sampled", True):
                return
            span = export_span(span, self.config or TraceConfig())
        except Exception:
            logger.debug("traceAI: export privacy failed; dropping span", exc_info=True)
            return
        if self.mappers:
            from copy import copy

            span = copy(span)
            from fi_instrumentation._attribute_limits import snapshot_attributes

            for mapper in self.mappers:
                span._attributes = snapshot_attributes(mapper(dict(span.attributes or {})), span._attributes)
        # Resource.merge revalidates mixed arrays (including scrubbed NaN/Infinity)
        # and silently drops them. Merge detached values; the exporter sanitizes last.
        from copy import copy

        resource = copy(span.resource or self._resource)
        resource._attributes = {**dict(getattr(span.resource, "attributes", {}) or {}),
                                **dict(self._resource.attributes)}
        # Keep the sanitized bounded containers (and their drop accounting).
        forwarded = copy(span)
        forwarded._context = trace_api.SpanContext(
            span.context.trace_id, span.context.span_id, span.context.is_remote,
            trace_api.TraceFlags(span.context.trace_flags | trace_api.TraceFlags.SAMPLED),
            span.context.trace_state)
        forwarded._resource = resource
        self._delegate.on_end(forwarded)

    def _detach_shutdown(self) -> Optional[Tuple[Any, Any]]:
        """Detach the one owned route; never shut down application processors."""
        self.enabled = False
        if self._closed:
            return None
        self._closed = True
        delegate, operation = self._delegate, self._operation

        def stop() -> None:
            with operation:
                delegate.shutdown()

        return stop, self._shutdown_operation

    def shutdown(self) -> None:
        task = self._detach_shutdown()
        if task is not None:
            _bounded_call(task[0], 5000, task[1])

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        delegate = self._delegate
        return _bounded_call(lambda: delegate.force_flush(timeout_millis), timeout_millis, self._operation)


def _exporter(settings: AutoConfig, kwargs: Optional[Mapping[str, Any]] = None) -> Any:
    from fi_instrumentation.otel import GRPCSpanExporter, HTTPSpanExporter, Transport, _normalized_endpoint
    from fi_instrumentation.settings import get_env_grpc_collector_endpoint

    kwargs = kwargs or {}
    from fi_instrumentation._credentials import merge_headers

    defaults = {"x-api-key": settings.api_key, "x-secret-key": settings.secret_key}
    headers = merge_headers({k.lower(): v for k, v in (kwargs.get("headers") or {}).items()}, defaults)
    transport = kwargs.get("transport", Transport.HTTP)
    if transport == Transport.GRPC:
        return GRPCSpanExporter(endpoint=settings.base_url or get_env_grpc_collector_endpoint(),
                                headers=headers, timeout=kwargs.get("timeout"))
    if transport != Transport.HTTP:
        raise ValueError(f"Invalid transport: {transport}")
    _, endpoint = _normalized_endpoint(settings.base_url) if settings.base_url else _normalized_endpoint(None)
    return HTTPSpanExporter(endpoint=endpoint, headers=headers, timeout=kwargs.get("timeout"))


def _fi_resource(settings: AutoConfig, project_type: Any, kwargs: Optional[Mapping[str, Any]] = None) -> Resource:
    """The resource ``register()`` would set, for spans exported through another provider."""
    import json
    import uuid

    from fi_instrumentation.otel import (
        EVAL_TAGS, METADATA, PROJECT_NAME, PROJECT_TYPE, PROJECT_VERSION_ID, PROJECT_VERSION_NAME,
        SEMANTIC_CONVENTION, SemanticConvention,
    )
    from fi_instrumentation.settings import get_env_project_name, get_env_project_version_name

    kwargs = kwargs or {}
    return Resource(attributes={
        PROJECT_NAME: settings.project_name or get_env_project_name(),
        PROJECT_TYPE: project_type.value,
        PROJECT_VERSION_NAME: settings.project_version_name or get_env_project_version_name(),
        PROJECT_VERSION_ID: str(uuid.uuid4()),
        EVAL_TAGS: json.dumps([]),
        METADATA: json.dumps(kwargs.get("metadata") or {}),
        SEMANTIC_CONVENTION: kwargs.get("semantic_convention", SemanticConvention.FI).value,
    })


def _build_provider(settings: AutoConfig, register_kwargs: Mapping[str, Any], config: Any,
                    supplied: Any = None) -> Any:
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from fi_instrumentation.otel import BatchSpanProcessor, SimpleSpanProcessor, TracerProvider as FiTracerProvider

    project_type = ProjectType.OBSERVE if settings.project_type == "observe" else ProjectType.EXPERIMENT
    kwargs = dict(register_kwargs)
    existing = supplied if supplied is not None else trace_api.get_tracer_provider()
    from fi_instrumentation._secrets import register_secret, seed_environment
    from fi_instrumentation.auto.wire._privacy import observe_request

    seed_environment()
    register_secret(settings.api_key)
    register_secret(settings.secret_key)
    # Explicit exporter-header credentials are effective FI credentials too.
    observe_request(kwargs.get("headers") or {})
    sampler = kwargs.get("sampler") or ParentBased(TraceIdRatioBased(settings.sample_rate))
    if isinstance(existing, FiTracerProvider) and supplied is None and not kwargs.get("span_exporter"):
        _protect_provider(existing, config)
        logger.info("traceAI: using the Future AGI provider the application registered")
        return existing, False
    selected = kwargs.get("span_exporter")
    if selected is None and settings.has_credentials:
        selected = _exporter(settings, kwargs)
    if isinstance(existing, SdkTracerProvider) and (supplied is not None or kwargs.get("set_global_tracer_provider", True)):
        if selected is None:
            _protect_provider(existing, config)
            return existing, False
        lease = _PrivacyExporter(selected, config)
        options = {k: kwargs[k] for k in ("max_queue_size", "schedule_delay_millis",
                   "max_export_batch_size", "export_timeout_millis") if k in kwargs}
        processor = (BatchSpanProcessor(span_exporter=lease, **options) if kwargs.get("batch", True)
                     else SimpleSpanProcessor(span_exporter=lease))
        overlay = _ResourceOverlayProcessor(processor, _fi_resource(settings, project_type, kwargs), config)
        root = TraceIdRatioBased(settings.sample_rate)
        overlay.sampler = kwargs.get("sampler") or ParentBased(
            root, local_parent_sampled=root, local_parent_not_sampled=root)
        overlay._exporter = selected
        existing.add_span_processor(overlay)
        _state.overlays[existing] = overlay
        _protect_provider(existing, config)
        return existing, False

    kwargs["span_exporter"] = _PrivacyExporter(selected, config)
    kwargs["sampler"] = sampler
    kwargs.setdefault("set_global_tracer_provider", True)
    kwargs.setdefault("project_name", settings.project_name)
    kwargs.setdefault("project_type", project_type)
    kwargs.setdefault("project_version_name", settings.project_version_name if project_type == ProjectType.EXPERIMENT else None)
    kwargs.setdefault("verbose", False)
    kwargs["signal_handlers"] = False
    provider = register(**kwargs)
    processors = provider._active_span_processor._span_processors
    overlay = _ResourceOverlayProcessor(processors[0], provider.resource, config)
    overlay._exporter = selected
    provider._active_span_processor._span_processors = (overlay,) + processors[1:]
    _state.overlays[provider] = overlay
    _protect_provider(provider, config)
    atexit.unregister(provider.shutdown)
    handler = getattr(provider, "_atexit_handler", None)
    if handler is not None:
        atexit.unregister(handler)
        provider._atexit_handler = None
    return provider, True


def _protect_provider(provider: Any, config: Any) -> None:
    """Snapshot global/native spans for other destinations too, without modifying them.

    Cached tracers keep the SDK's chain, so interpose its callbacks, not the provider.
    The FI exporter still sanitizes immediately before serialization after its queue.
    """
    from fi_instrumentation.auto.wire._privacy import export_span

    active = provider._active_span_processor

    def protect_exporters(processors: Any) -> None:
        for owner, key, exporter in _exporter_slots(processors):
            if not isinstance(exporter, _PrivacyExporter):
                setattr(owner, key, _PrivacyExporter(exporter, config))

    protect_exporters(active._span_processors)
    original_add = active.add_span_processor

    def add(processor: Any) -> None:
        protect_exporters((processor,))
        original_add(processor)

    active.add_span_processor = add
    on_start, on_end = active.on_start, active.on_end

    def start(span: Any, parent_context: Any = None) -> None:
        from fi_instrumentation._attribute_limits import adopt_admission_hints
        from fi_instrumentation.instrumentation._tracers import _mark_model_coverage

        adopt_admission_hints(span)  # media context survives SDK eviction (S1 at export)
        _mark_model_coverage(span, parent_context)
        on_start(span, parent_context=parent_context)

    def end(span: Any) -> None:
        on_end(export_span(span, config))

    active.on_start, active.on_end = start, end


_warned_credentials = False
_sigterm_chained = False
_lifecycle_lock = threading.Lock()
_shutdown_lock = threading.Lock()
_exit_hook_registered = False


def _bounded_call(function: Any, timeout_millis: int, lock: Any = None) -> bool:
    """OTel processors may ignore timeouts. Never wait on their export lock in a handler.

    A single daemon operation per lock bounds both waiting and abandoned worker count.
    """
    lock = lock if lock is not None else _lifecycle_lock
    if timeout_millis <= 0 or not lock.acquire(blocking=False):
        return False
    completed = threading.Event()
    result = [False]

    def run() -> None:
        try:
            result[0] = function() is not False
        except Exception:
            logger.debug("traceAI lifecycle operation failed", exc_info=True)
        finally:
            lock.release()
            completed.set()

    try:
        threading.Thread(target=run, name="traceai-flush", daemon=True).start()
    except Exception:
        lock.release()
        return False
    return completed.wait(timeout_millis / 1000.0) and result[0]


def _flush_on_sigterm(provider: Any, timeout_millis: int = 5000) -> None:
    """Flush on SIGTERM, then do whatever SIGTERM did before.

    Servers such as uvicorn handle SIGTERM, shut down, restore the previous handler and raise
    the signal again, so the process dies by the signal and ``atexit`` never runs. Chaining in
    front of the existing handler keeps the last batch of spans without changing how the
    process exits.
    """
    global _sigterm_chained
    import signal

    if _sigterm_chained or threading.current_thread() is not threading.main_thread():
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (ValueError, AttributeError):
        return

    def handler(signum: int, frame: Any) -> None:
        try:
            target = _state.provider or provider
            _bounded_call(lambda: target.force_flush(timeout_millis), timeout_millis)
        except Exception:
            pass
        if callable(previous):
            previous(signum, frame)
        elif previous != signal.SIG_IGN:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    try:
        signal.signal(signal.SIGTERM, handler)
        _sigterm_chained = True
    except (ValueError, OSError):
        pass


def _once(function: Any) -> Any:
    @wraps(function)
    def initialize(*args: Any, **kwargs: Any) -> Any:
        with _state.init_lock:
            if _state.initialized:
                logger.info("traceAI: already initialized; returning the existing provider")
                return _state.provider
            return function(*args, **kwargs)
    return initialize


@_once
def init(
    *,
    project_name: Optional[str] = None,
    project_type: Optional[str] = None,
    project_version_name: Optional[str] = None,
    wire_max_body_bytes: Optional[int] = None,
    api_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    base_url: Optional[str] = None,
    instrumentations: Optional[Iterable[str]] = None,
    disabled_instrumentations: Optional[Iterable[str]] = None,
    wire: Optional[bool] = None,
    sample_rate: Optional[float] = None,
    debug: Optional[bool] = None,
    tracer_provider: Any = None,
    config: Any = None,
    config_file: Optional[str] = None,
    lazy: bool = True,
    **register_kwargs: Any,
) -> Any:
    """Trace everything in one line: set up export to Future AGI and enable every integration.

    ``fi_instrumentation.init()`` reads ``FI_API_KEY``/``FI_SECRET_KEY`` and the rest of its
    settings once from arguments, then the environment, then ``futureagi.toml``. It defaults to an
    ``observe`` project, enables every installed integration and the wire layer, leaves the
    application's signal handlers alone, and flushes spans at exit.

    Returns the tracer provider, or None when tracing is off (``OTEL_SDK_DISABLED=true``, or
    no credentials and no ``span_exporter``/``tracer_provider``).

    Raises:
        ConfigError: an argument, variable or config-file value is invalid.
    """
    global _warned_credentials, _exit_hook_registered
    overrides = {
        "project_name": project_name, "project_type": project_type, "api_key": api_key,
        "secret_key": secret_key, "base_url": base_url, "sample_rate": sample_rate, "debug": debug,
        "wire_capture": wire, "wire_max_body_bytes": wire_max_body_bytes,
        "project_version_name": project_version_name,
        "instrumentations": list(instrumentations) if instrumentations is not None else None,
        "disabled_instrumentations": list(disabled_instrumentations) if disabled_instrumentations is not None else None,
    }
    settings = resolve(overrides, config_file=config_file, trace_config=config)
    config = settings.trace_config
    with _state.lock:
        # Pin the one resolved policy (S2) before building exporters, so every default
        # TraceConfig() in this process (manual instrumentors too) resolves to it.
        if _state.trace_config is None:
            _state.trace_config = config
            _state.settings = settings
    if settings.sdk_disabled:
        logger.info("traceAI: OTEL_SDK_DISABLED is set; tracing is off")
        _state.initialized = True
        return None
    if tracer_provider is None and not settings.has_credentials and "span_exporter" not in register_kwargs:
        if not _warned_credentials:
            _warned_credentials = True
            logger.warning("traceAI: FI_API_KEY and FI_SECRET_KEY are not set; tracing is off")
        _state.initialized = True
        return None
    tracer_provider, owned = _build_provider(settings, register_kwargs, config, tracer_provider)
    _state.owned_provider = owned
    _state.initialized = True
    if not _exit_hook_registered:
        atexit.register(shutdown)
        _exit_hook_registered = True
    if hasattr(tracer_provider, "force_flush"):
        _flush_on_sigterm(tracer_provider)
    auto_instrument(tracer_provider=tracer_provider, config=config, settings=settings, lazy=lazy)
    if settings.debug:
        logger.warning("%s", format_report())
    return tracer_provider


def flush(timeout_millis: int = 30000) -> bool:
    """Export buffered spans now. Call before a short-lived process (a Lambda, a job) exits."""
    provider = _state.provider
    if provider is not None and hasattr(provider, "force_flush"):
        return _bounded_call(lambda: provider.force_flush(timeout_millis), timeout_millis)
    return True


def shutdown(timeout_millis: int = 5000) -> None:
    """Close the agent once, bounded by the deadline. Restart requires a new process."""
    from fi_instrumentation.auto import wire as wire_layer

    state = _state
    if state.stopped:
        return
    state.stopped = True
    if wire_layer._recorder is not None:
        wire_layer._recorder.enabled = False
    if timeout_millis <= 0:
        # No time for teardown, but the agent is stopped: close every FI export route now
        # (a flag write, never a wait). Patches stay until the process exits.
        for overlay in list(state.overlays.values()):
            overlay.enabled = False

    def stop() -> None:
        with state.lock:
            cleanups, state.cleanups = state.cleanups, {}
            overlays = list(state.overlays.values())
            wire_layer.disable()
            for entry in state.statuses.values():
                if entry.status in (PENDING, INSTRUMENTED):
                    entry.status, entry.detail = DISABLED, "agent shut down; restart requires a new process"
        for name, callback in cleanups.items():
            _cleanup(callback, name)
        with _lifecycle_lock:
            for overlay in overlays:
                overlay.shutdown()
            if state.owned_provider and state.provider is not None and not overlays:
                state.owned_provider = False
                state.provider.shutdown()

    _bounded_call(stop, timeout_millis, _shutdown_lock)


def _after_fork() -> None:
    """No worker threads survive fork, so none of their mutexes may survive it."""
    global _lifecycle_lock, _shutdown_lock
    _lifecycle_lock = threading.Lock()
    _shutdown_lock = threading.Lock()
    _state.lock = threading.RLock()
    _state.init_lock = threading.RLock()
    _state.enabling.clear()
    for overlay in _state.overlays.values():
        overlay._operation = threading.Lock()
        overlay._shutdown_operation = threading.Lock()
        processor = overlay._delegate
        if hasattr(processor, "_shutdown_lock"):
            processor._shutdown_lock = threading.Lock()
    from fi_instrumentation.auto import wire as wire_layer

    wire_layer._lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
