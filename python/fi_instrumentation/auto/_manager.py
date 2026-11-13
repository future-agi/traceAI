"""Turns on every installed traceAI integration and reports what each one did."""

import atexit
import importlib
import importlib.machinery
import logging
import os
import sys
import threading
from collections import OrderedDict
from copy import copy
from dataclasses import dataclass, replace
from functools import wraps
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import wrapt
from opentelemetry import trace as trace_api
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from fi_instrumentation.auto import _registry
from fi_instrumentation.auto._config import AutoConfig, resolve
from fi_instrumentation.auto._registry import INSTRUMENTOR, MANUAL, PLUGIN, SERVICE, Integration

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
            kwargs: Dict[str, Any] = {"tracer_provider": provider}
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
    """The provider as a setup adapter sees it: processor additions are recorded for cleanup."""

    def __init__(self, provider: Any, chain: Any) -> None:
        super().__init__(provider)
        self._self_chain = chain

    @property
    def _active_span_processor(self) -> Any:
        return self._self_chain


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
    # Legacy mapping adapters may replace an exporter; put the original back on cleanup.
    replacements = [(slot_owner, key, old, getattr(slot_owner, key)) for slot_owner, key, old in slots
                    if getattr(slot_owner, key) is not old]
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
        for slot_owner, key, old, replacement in replacements:
            if getattr(slot_owner, key) is replacement:
                setattr(slot_owner, key, old)
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
            if result.status == INSTRUMENTED and result.detail != "already instrumented":
                # The Future AGI route carries the spans of integrations the agent enabled.
                for overlay in state.overlays.values():
                    overlay.admit(integration)
            state.statuses[integration.name] = result
        # Shutdown either owns the published callback or the installer owns it, never both.
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
        status.detail = f"not traced yet: pip install {integration.package}"
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
    lazy: bool = True,
    settings: Optional[AutoConfig] = None,
) -> Dict[str, str]:
    """Enable every installed traceAI integration.

    Additive: later calls never disable, replace or reconfigure active integrations.
    Integrations whose library is installed but not yet imported are enabled on first
    import (``pending``), so unused libraries cost nothing.

    Args:
        tracer_provider: Provider to trace with. Defaults to the global provider.
        config: ``TraceConfig`` for masking. Defaults to one read from the environment
            and ``futureagi.toml``.
        instrumentations: Only enable these integrations (names from ``report()``).
        disabled_instrumentations: Never enable these.
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
    if settings is None:
        settings = resolve(overrides, trace_config=config)
    elif overrides:
        # An explicit configuration is the base, not permission to reread the
        # environment and discard its exclusions or credentials.
        from fi_instrumentation.auto._config import _as_list

        values = {key: _as_list(value) for key, value in overrides.items()}
        settings = replace(settings, **values,
                           sources={**settings.sources, **{key: "argument" for key in overrides}})
    actions: List[Tuple[Integration, Optional[str], List[str]]] = []
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

    for integration, action, installed in actions:
        if action == "now":
            _reconcile(integration)
        elif action == "hook" and integration.name not in _state.hooked:
            _state.hooked.add(integration.name)
            for target in installed:
                wrapt.register_post_import_hook(lambda _module, i=integration: _on_import(i), target)
    with _state.lock:
        return {name: s.status for name, s in _state.statuses.items()}


def plan(settings: Optional[AutoConfig] = None) -> List[IntegrationStatus]:
    """What ``init()`` would enable in this environment, without enabling or importing anything."""
    settings = settings or resolve()
    statuses = [_plan(i, settings, lazy=True)[0] for i in _registry.ALL]
    return sorted(statuses, key=lambda s: (s.category, s.name))


def report() -> List[IntegrationStatus]:
    """Current status of every integration, refreshed for libraries imported since startup."""
    with _state.lock:
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


#: Instrumentation scopes whose spans are AI spans by construction: traceAI packages and the core.
AI_SCOPES = ("traceai", "fi_instrumentation")
# Traces with an AI span whose request root span has not ended yet. Bounded: a trace whose root
# never ends must not hold memory, and a root that ends after eviction is simply not sent.
_AI_TRACES_LIMIT = 4096


def _masked(attributes: Mapping[str, Any], config: Any) -> Dict[str, Any]:
    """The TraceConfig hide settings applied to every attribute, as FITracer applies them."""
    masked = {}
    for key, value in attributes.items():
        value = config.mask(key, value)
        if value is not None:
            masked[key] = value
    return masked


class _ResourceOverlayProcessor(SpanProcessor):
    """The agent's Future AGI export route on a tracer provider.

    Future AGI receives AI spans only: spans from traceAI instrumentation scopes and the library
    spans of integrations the agent enabled, plus the request root span of a web server the agent
    enabled when that request led to an AI span. Every other span the provider ends goes to the
    application's own processors and exporters only. The copy sent to Future AGI is masked with
    the agent's TraceConfig, mapped by the integrations' mappers and, on a provider the application
    configured, tagged with the project resource Future AGI needs for routing. The application's
    processors see the original spans. On the provider the agent created, the route wraps that
    provider's exporter. Either way it can be closed at shutdown.
    """

    def __init__(self, delegate: SpanProcessor, resource: Optional[Resource] = None,
                 sample_rate: float = 1.0, config: Any = None) -> None:
        self._traceai_route = True
        self._delegate = delegate
        self._resource = resource
        self._config = config
        # Ratio sampling decides by trace id, so a trace is kept or dropped as a whole.
        self._ratio = TraceIdRatioBased(sample_rate) if sample_rate < 1.0 else None
        self.mappers: list = []
        self.span_mappers: list = []
        self.scopes: Tuple[str, ...] = AI_SCOPES
        self.server_scopes: Tuple[str, ...] = ()
        self._ai_traces: "OrderedDict[int, None]" = OrderedDict()
        self._ai_traces_lock = threading.Lock()
        self._warned = False
        self.enabled = True
        self._operation = threading.Lock()
        self._shutdown_operation = threading.Lock()
        self._closed = False
        self._merged: Tuple[Any, Any] = (None, None)

    @property
    def span_exporter(self) -> Any:
        """The Future AGI exporter behind this route.

        Integrations look for the exporters already on a provider (``span_exporter``, or a batch
        processor's ``_batch_processor._exporter``) to avoid adding a second one for the same
        destination, and some replace one with a mapping exporter.
        """
        exporter = getattr(self._delegate, "span_exporter", None)
        if exporter is None:
            exporter = getattr(getattr(self._delegate, "_batch_processor", None), "_exporter", None)
        return exporter

    @span_exporter.setter
    def span_exporter(self, exporter: Any) -> None:
        batch = getattr(self._delegate, "_batch_processor", None)
        if batch is not None and hasattr(batch, "_exporter"):
            batch._exporter = exporter
        else:
            self._delegate.span_exporter = exporter

    def admit(self, integration: Integration) -> None:
        """Carry the spans of an integration the agent enabled: its scopes, or its request roots."""
        scopes = tuple(scope.lower() for scope in integration.scopes)
        if integration.category == "web":
            self.server_scopes += scopes
        else:
            self.scopes += scopes

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        # Deliberately not forwarded: a processor that tracks open spans would end the
        # application's still-running spans when the agent shuts this route down.
        pass

    def _keeps(self, span: ReadableSpan) -> bool:
        if self._ratio is None:
            return True
        return self._ratio.should_sample(None, span.context.trace_id, span.name).decision.is_sampled()

    def _forwards(self, span: ReadableSpan) -> bool:
        """Whether a span is an AI span, or the request root of a trace that had one."""
        name = (getattr(getattr(span, "instrumentation_scope", None), "name", None) or "").lower()
        trace_id = span.context.trace_id
        if name.startswith(self.scopes):
            with self._ai_traces_lock:
                self._ai_traces[trace_id] = None
                self._ai_traces.move_to_end(trace_id)
                while len(self._ai_traces) > _AI_TRACES_LIMIT:
                    self._ai_traces.popitem(last=False)
            return True
        if not self.server_scopes or not name.startswith(self.server_scopes):
            return False
        if span.kind != trace_api.SpanKind.SERVER or (span.parent is not None and not span.parent.is_remote):
            return False
        # A request's spans end before its root, so an AI call in it has been seen by now.
        with self._ai_traces_lock:
            if trace_id not in self._ai_traces:
                return False
            del self._ai_traces[trace_id]
            return True

    def _routed_resource(self, resource: Any) -> Resource:
        source, merged = self._merged
        if source is not resource:
            attributes = {**dict(getattr(resource, "attributes", None) or {}), **dict(self._resource.attributes)}
            merged = Resource(attributes, getattr(resource, "schema_url", None))
            self._merged = (resource, merged)
        return merged

    def _future_agi_copy(self, span: ReadableSpan) -> Optional[ReadableSpan]:
        for span_mapper in self.span_mappers:
            span = span_mapper(span)
            if span is None:
                return None
        # The application's processors keep their own view of the span.
        span = copy(span)
        attributes = dict(span.attributes or {})
        for mapper in self.mappers:
            attributes = mapper(attributes)
        if self._config is not None:
            attributes = _masked(attributes, self._config)
        span._attributes = attributes
        if self._resource is not None:
            span._resource = self._routed_resource(span.resource)
        return span

    def on_end(self, span: ReadableSpan) -> None:
        if not self.enabled or not self._keeps(span):
            return
        try:
            if not self._forwards(span):
                return
            routed = self._future_agi_copy(span)
        except Exception as error:
            # Never fail the application's span.end(); a copy that cannot be prepared is not sent.
            if not self._warned:
                self._warned = True
                logger.warning("traceAI: a span could not be prepared for Future AGI and was not sent (%s)",
                               type(error).__name__)
            logger.debug("traceAI: Future AGI copy failed", exc_info=True)
            return
        if routed is not None:
            self._delegate.on_end(routed)

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


def _exporter(settings: AutoConfig, kwargs: Mapping[str, Any]) -> Any:
    """The Future AGI exporter for the resolved keys and base URL."""
    from fi_instrumentation.otel import GRPCSpanExporter, HTTPSpanExporter, Transport, _normalized_endpoint

    headers = {str(k).lower(): v for k, v in (kwargs.get("headers") or {}).items()}
    # Explicit credential headers win, as they do for register().
    if not any(name in headers for name in ("authorization", "x-api-key", "x-secret-key")):
        headers.update({"x-api-key": settings.api_key, "x-secret-key": settings.secret_key})
    transport = kwargs.get("transport", Transport.HTTP)
    if transport == Transport.GRPC:
        return GRPCSpanExporter(headers=headers, timeout=kwargs.get("timeout"))
    if transport != Transport.HTTP:
        raise ValueError(f"Invalid transport: {transport}")
    _, endpoint = _normalized_endpoint(settings.base_url)
    return HTTPSpanExporter(endpoint=endpoint, headers=headers, timeout=kwargs.get("timeout"))


def _fi_resource(settings: AutoConfig, project_type: Any, kwargs: Mapping[str, Any]) -> Resource:
    """The resource ``register()`` would set, for spans exported through another provider."""
    import json
    import uuid

    from fi_instrumentation.otel import (
        EVAL_TAGS, METADATA, PROJECT_NAME, PROJECT_TYPE, PROJECT_VERSION_ID, PROJECT_VERSION_NAME,
        SEMANTIC_CONVENTION, SemanticConvention,
    )
    from fi_instrumentation.settings import get_env_project_name, get_env_project_version_name

    return Resource(attributes={
        PROJECT_NAME: settings.project_name or get_env_project_name(),
        PROJECT_TYPE: project_type.value,
        PROJECT_VERSION_NAME: settings.project_version_name or get_env_project_version_name(),
        PROJECT_VERSION_ID: str(uuid.uuid4()),
        EVAL_TAGS: json.dumps([]),
        METADATA: json.dumps(kwargs.get("metadata") or {}),
        SEMANTIC_CONVENTION: SemanticConvention(kwargs.get("semantic_convention", SemanticConvention.FI)).value,
    })


def _build_provider(settings: AutoConfig, register_kwargs: Mapping[str, Any],
                    supplied: Any = None) -> Tuple[Any, bool]:
    """The provider to trace with, and whether the agent created (and so owns) it."""
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from fi_instrumentation.otel import BatchSpanProcessor, SimpleSpanProcessor
    from fi_instrumentation.otel import TracerProvider as FiTracerProvider

    project_type = ProjectType.OBSERVE if settings.project_type == "observe" else ProjectType.EXPERIMENT
    kwargs = dict(register_kwargs)
    existing = supplied if supplied is not None else trace_api.get_tracer_provider()
    if isinstance(existing, FiTracerProvider) and not kwargs.get("span_exporter"):
        # A Future AGI provider already exports to Future AGI: add nothing.
        logger.info("traceAI: using the Future AGI provider the application registered")
        return existing, False
    if supplied is not None and not isinstance(supplied, SdkTracerProvider):
        # Not an SDK provider, so there is no processor chain to join: trace with it as given.
        return supplied, False
    selected = kwargs.get("span_exporter")
    if selected is None and settings.has_credentials:
        selected = _exporter(settings, kwargs)
    if isinstance(existing, SdkTracerProvider) and (supplied is not None or kwargs.get("set_global_tracer_provider", True)):
        # The application configured OpenTelemetry: keep its sampler and exporters, and add
        # one Future AGI route next to them.
        if selected is None:
            return existing, False
        options = {k: kwargs[k] for k in ("max_queue_size", "schedule_delay_millis",
                   "max_export_batch_size", "export_timeout_millis") if k in kwargs}
        processor = (BatchSpanProcessor(span_exporter=selected, **options) if kwargs.get("batch", True)
                     else SimpleSpanProcessor(span_exporter=selected))
        overlay = _ResourceOverlayProcessor(processor, _fi_resource(settings, project_type, kwargs),
                                            settings.sample_rate, config=settings.trace_config)
        existing.add_span_processor(overlay)
        _state.overlays[existing] = overlay
        return existing, False

    kwargs["span_exporter"] = selected
    kwargs.setdefault("sampler", ParentBased(TraceIdRatioBased(settings.sample_rate)))
    kwargs.setdefault("set_global_tracer_provider", True)
    kwargs.setdefault("project_name", settings.project_name)
    kwargs.setdefault("project_type", project_type)
    kwargs.setdefault("project_version_name",
                      settings.project_version_name if project_type == ProjectType.EXPERIMENT else None)
    kwargs.setdefault("verbose", False)
    provider = register(**kwargs)
    active = provider._active_span_processor
    processors = tuple(active._span_processors)
    overlay = _ResourceOverlayProcessor(processors[0], config=settings.trace_config)
    active._span_processors = (overlay,) + processors[1:]
    _state.overlays[provider] = overlay
    # The agent flushes and closes its route at exit itself, bounded, and only once.
    atexit.unregister(provider.shutdown)
    handler = getattr(provider, "_atexit_handler", None)
    if handler is not None:
        atexit.unregister(handler)
        provider._atexit_handler = None
    return provider, True


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
    except RuntimeError as error:
        if "shutdown" not in str(error):
            lock.release()
            return False
        # Python 3.12+ refuses new threads once the interpreter is exiting (atexit), so the
        # exit flush runs inline, bounded by the processors' own timeouts as register()'s is.
        run()
        return result[0]
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
    api_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    base_url: Optional[str] = None,
    instrumentations: Optional[Iterable[str]] = None,
    disabled_instrumentations: Optional[Iterable[str]] = None,
    sample_rate: Optional[float] = None,
    debug: Optional[bool] = None,
    tracer_provider: Any = None,
    config: Any = None,
    config_file: Optional[str] = None,
    lazy: bool = True,
    **register_kwargs: Any,
) -> Any:
    """Trace everything in one line: set up export to Future AGI and enable every installed integration.

    ``fi_instrumentation.init()`` reads ``FI_API_KEY``/``FI_SECRET_KEY`` and the rest of its
    settings once, from arguments, then the environment, then ``futureagi.toml``. It defaults to
    an ``observe`` project, enables every installed traceAI integration when its library is first
    imported, leaves the application's signal handlers alone, and flushes spans at exit. Other
    keyword arguments are passed to ``register()``. Only the first call in a process configures
    anything; later calls return the same provider.

    Returns the tracer provider, or None when tracing is off (``OTEL_SDK_DISABLED=true``, or
    no keys and no ``span_exporter``/``tracer_provider``).

    Raises:
        ConfigError: an argument, variable or config-file value is invalid.
    """
    global _warned_credentials, _exit_hook_registered
    overrides = {
        "project_name": project_name, "project_type": project_type, "api_key": api_key,
        "secret_key": secret_key, "base_url": base_url, "sample_rate": sample_rate, "debug": debug,
        "project_version_name": project_version_name,
        "instrumentations": list(instrumentations) if instrumentations is not None else None,
        "disabled_instrumentations": list(disabled_instrumentations) if disabled_instrumentations is not None else None,
    }
    settings = resolve(overrides, config_file=config_file, trace_config=config)
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
    provider, owned = _build_provider(settings, register_kwargs, tracer_provider)
    _state.owned_provider = owned
    _state.initialized = True
    if not _exit_hook_registered:
        atexit.register(shutdown)
        _exit_hook_registered = True
    if hasattr(provider, "force_flush"):
        _flush_on_sigterm(provider)
    auto_instrument(tracer_provider=provider, config=settings.trace_config, settings=settings, lazy=lazy)
    if settings.debug:
        logger.warning("%s", format_report())
    return provider


def flush(timeout_millis: int = 30000) -> bool:
    """Export buffered spans now. Call before a short-lived process (a Lambda, a job) exits."""
    provider = _state.provider
    if provider is not None and hasattr(provider, "force_flush"):
        return _bounded_call(lambda: provider.force_flush(timeout_millis), timeout_millis)
    return True


def shutdown(timeout_millis: int = 5000) -> None:
    """Close the agent once, bounded by the deadline. Restart requires a new process."""
    state = _state
    if state.stopped:
        return
    state.stopped = True
    if timeout_millis <= 0:
        # No time for teardown, but the agent is stopped: close every Future AGI route now
        # (a flag write, never a wait). Patches stay until the process exits.
        for overlay in list(state.overlays.values()):
            overlay.enabled = False

    def stop() -> None:
        with state.lock:
            cleanups, state.cleanups = state.cleanups, {}
            overlays = list(state.overlays.values())
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
        overlay._ai_traces_lock = threading.Lock()
        processor = overlay._delegate
        if hasattr(processor, "_shutdown_lock"):
            processor._shutdown_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)
