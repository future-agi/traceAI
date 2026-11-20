import atexit
import inspect
import json
import logging
import os
import re
import socket
import threading
import uuid
import warnings
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple, Type, Union
from urllib.parse import ParseResult, unquote, urlparse, urlsplit

import requests
from fi_instrumentation.fi_types import (
    EvalName,
    EvalTag,
    ProjectType,
    prepare_eval_tags,
)
from fi_instrumentation.instrumentation.constants import (
    DEFAULT_MAX_ACTIVE_SPANS_TRACKED,
    FI_MAX_ACTIVE_SPANS_TRACKED,
)
from fi_instrumentation.settings import (
    UuidIdGenerator,
    get_env_collector_endpoint,
    get_env_fi_auth_header,
    get_env_grpc_collector_endpoint,
    get_env_project_name,
    get_env_project_version_name,
)

try:
    from fi_instrumentation.settings import get_env_traces_endpoint
except ImportError:
    # An older fi_instrumentation.settings beside this module (the published-core
    # compatibility checks import this file next to a released package).
    def get_env_traces_endpoint() -> Optional[str]:
        traces = (os.getenv("FI_TRACES_URL") or "").strip()
        return traces or get_env_collector_endpoint()

from jsonschema import ValidationError
from opentelemetry import trace as trace_api
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    OTLPSpanExporter as _HTTPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanLimits, SpanProcessor
from opentelemetry.sdk.trace import TracerProvider as _TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor as _BatchSpanProcessor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor as _SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.sampling import Sampler
from opentelemetry.trace import Status, StatusCode

try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as _GRPCSpanExporter,
    )

    _GRPC_INSTALLED = True
except ImportError:
    _GRPCSpanExporter = object
    _GRPC_INSTALLED = False

logger = logging.getLogger(__name__)

PROJECT_NAME = "project_name"
PROJECT_TYPE = "project_type"
PROJECT_VERSION_NAME = "project_version_name"
PROJECT_VERSION_ID = "project_version_id"
EVAL_TAGS = "eval_tags"
METADATA = "metadata"

CONTENT_TYPE = "Content-Type"
AUTHORIZATION = "authorization"


class Transport(str, Enum):
    GRPC = "grpc"
    HTTP = "http"


class SemanticConvention(str, Enum):
    """
    Semantic convention selector recorded on the provider's resource.

    This describes the convention emitted by the application or framework; it
    does not rewrite, rename, or add span attributes.
    - FI: Future AGI native convention (default)
    - OTEL_GENAI: OpenTelemetry GenAI SIG convention
    - OPENINFERENCE: OpenInference convention
    - OPENLLMETRY: OpenLLMetry convention
    """
    FI = "fi"
    OTEL_GENAI = "otel_genai"
    OPENINFERENCE = "openinference"
    OPENLLMETRY = "openllmetry"


SEMANTIC_CONVENTION = "semantic_convention"


def register(
    *,
    project_name: Optional[str] = None,
    project_type: Optional[ProjectType] = ProjectType.EXPERIMENT,
    project_version_name: Optional[str] = None,
    eval_tags: Optional[List[EvalTag]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    batch: bool = True,
    set_global_tracer_provider: bool = False,
    headers: Optional[Dict[str, str]] = None,
    verbose: bool = True,
    transport: Transport = Transport.HTTP,
    semantic_convention: SemanticConvention = SemanticConvention.FI,
    max_queue_size: Optional[int] = None,
    schedule_delay_millis: Optional[float] = None,
    max_export_batch_size: Optional[int] = None,
    export_timeout_millis: Optional[float] = None,
    span_exporter: Optional[SpanExporter] = None,
    timeout: Optional[float] = None,
    sampler: Optional[Sampler] = None,
    span_limits: Optional[SpanLimits] = None,
) -> _TracerProvider:
    """Configure and return a Future AGI `TracerProvider`.

    Span processors added later with `add_span_processor` run next to the Future AGI
    exporter, not in place of it. To export somewhere else instead, pass `span_exporter`.

    All tuning args below default to `None`, meaning the corresponding OpenTelemetry
    environment variable / upstream default applies (no behavior change when unset).

    Args:
        project_name, project_type, project_version_name, eval_tags, metadata:
            Future AGI project/eval configuration.
        batch (bool): Use `BatchSpanProcessor` (default) vs `SimpleSpanProcessor`.
        set_global_tracer_provider (bool): Register as the global OTel provider.
        headers (dict): Extra headers sent to the collector.
        verbose (bool): Print configuration details to stdout.
        transport (Transport): HTTP (default) or gRPC.
        semantic_convention (SemanticConvention): Resource selector describing the
            emitted convention; does not convert span attributes.

        Batch processor tuning (ignored unless `batch=True`):
        max_queue_size (int): Max spans buffered before overflow drops
            (`OTEL_BSP_MAX_QUEUE_SIZE`, upstream default 2048).
        schedule_delay_millis (float): Delay between exports, in **milliseconds**.
        max_export_batch_size (int): Max spans per export batch (upstream default 512).
        export_timeout_millis (float): Batch export timeout, in **milliseconds**.

        Exporter tuning (used only when `span_exporter` is not provided):
        span_exporter (SpanExporter): Pre-built exporter; bypasses the default one
            (and `timeout` below).
        timeout (float): Per-export timeout, in **seconds** (note: seconds here vs
            milliseconds for the batch params above).

        Provider tuning:
        sampler (Sampler): Head sampler (e.g. `TraceIdRatioBased`) to shed volume.
        span_limits (SpanLimits): Caps on attributes/events/links per span.
    """

    eval_tags = eval_tags or []
    metadata = metadata or {}

    if eval_tags:
        for tag in eval_tags:
            if isinstance(tag.eval_name, EvalName):
                tag.eval_name = tag.eval_name.value
        eval_tags = prepare_eval_tags(eval_tags)

    if project_type == ProjectType.OBSERVE:
        if eval_tags:
            raise ValidationError("Eval tags are not allowed for project type OBSERVE")
        if project_version_name:
            raise ValidationError(
                "Project Version Name not allowed for project type OBSERVE"
            )

    project_name = project_name or get_env_project_name()
    project_version_name = project_version_name or get_env_project_version_name()
    project_version_id = str(uuid.uuid4())

    custom_eval_names = [tag["custom_eval_name"] for tag in eval_tags]

    # Check for duplicate custom eval names
    if len(custom_eval_names) != len(set(custom_eval_names)):
        raise ValidationError("Duplicate custom eval names are not allowed")

    custom_eval_exists = check_custom_eval_config_exists(
        project_name=project_name,
        project_type=project_type.value,
        eval_tags=eval_tags,
    )

    if custom_eval_exists:
        raise ValidationError(
            "Custom eval configuration already exists for this project"
        )

    resource_attributes = {
        PROJECT_NAME: project_name,
        PROJECT_TYPE: project_type.value,
        PROJECT_VERSION_NAME: project_version_name,
        PROJECT_VERSION_ID: project_version_id,
        EVAL_TAGS: json.dumps(eval_tags),
        METADATA: json.dumps(metadata),
        SEMANTIC_CONVENTION: semantic_convention.value,
    }

    resource = Resource(attributes=resource_attributes)

    # Only forward provider tuning when set, so unset -> upstream env/default.
    provider_kwargs: Dict[str, Any] = {}
    if sampler is not None:
        provider_kwargs["sampler"] = sampler
    if span_limits is not None:
        provider_kwargs["span_limits"] = span_limits

    tracer_provider = TracerProvider(
        resource=resource, verbose=False, id_generator=UuidIdGenerator(),
        transport=transport, **provider_kwargs,
    )
    span_processor: SpanProcessor
    if batch:
        span_processor = BatchSpanProcessor(
            span_exporter=span_exporter,
            headers=headers,
            transport=transport,
            max_queue_size=max_queue_size,
            schedule_delay_millis=schedule_delay_millis,
            max_export_batch_size=max_export_batch_size,
            export_timeout_millis=export_timeout_millis,
            timeout=timeout,
        )
    else:
        batch_only = {
            "max_queue_size": max_queue_size,
            "schedule_delay_millis": schedule_delay_millis,
            "max_export_batch_size": max_export_batch_size,
            "export_timeout_millis": export_timeout_millis,
        }
        ignored = [name for name, value in batch_only.items() if value is not None]
        if ignored:
            warnings.warn(
                f"batch=False: ignoring batch-only tuning {ignored}; "
                "these apply only to BatchSpanProcessor.",
                stacklevel=2,
            )
        span_processor = SimpleSpanProcessor(
            span_exporter=span_exporter,
            headers=headers,
            transport=transport,
            timeout=timeout,
        )
    # Replaces the default the provider added on construction. This processor is not marked as a
    # default: a later add_span_processor() call adds next to it, as in upstream OpenTelemetry.
    tracer_provider.add_span_processor(span_processor)

    if set_global_tracer_provider:
        trace_api.set_tracer_provider(tracer_provider)
        global_provider_msg = (
            "|  \n"
            "|  `register` has set this TracerProvider as the global OpenTelemetry default.\n"
            "|  To disable this behavior, call `register` with "
            "`set_global_tracer_provider=False`.\n"
        )
    else:
        global_provider_msg = ""

    tracer_provider.setup_signal_handlers()

    details = tracer_provider._tracing_details()
    if verbose:
        print(f"{details}" f"{global_provider_msg}")
    return tracer_provider


class TracerProvider(_TracerProvider):
    """
    An extension of `opentelemetry.sdk.trace.TracerProvider` with Future AGI aware defaults.

    Extended keyword arguments are documented in the `Args` section. For further documentation, see
    the OpenTelemetry documentation at https://opentelemetry.io/docs/specs/otel/trace/sdk/.

    On construction a default `SimpleSpanProcessor` is added, exporting over `transport`.
    The collector endpoint is inferred from `transport` and the `FI_TRACES_URL` (else `FI_BASE_URL`)/`FI_GRPC_URL`
    environment variables. The first `add_span_processor` call replaces the default; later calls
    add processors next to it. The provider `register()` returns has no such default: processors
    added to it run next to its exporter. In addition to the arguments below, all upstream
    `opentelemetry.sdk.trace.TracerProvider` keyword arguments (`sampler`, `resource`,
    `span_limits`, `id_generator`, ...) are accepted and forwarded.

    Args:
        transport (Transport): Transport (HTTP or gRPC) used for the default exporter.
        verbose (bool): If True, configuration details will be printed to stdout.
    """

    def __init__(
        self,
        *args: Any,
        verbose: bool = True,
        transport: Transport = Transport.HTTP,
        **kwargs: Any,
    ):
        if "shutdown_on_exit" not in kwargs:
            kwargs["shutdown_on_exit"] = True

        sig = _get_class_signature(_TracerProvider)
        bound_args = sig.bind_partial(*args, **kwargs)
        bound_args.apply_defaults()
        if bound_args.arguments.get("resource") is None:
            bound_args.arguments["resource"] = Resource.create(
                {
                    PROJECT_NAME: get_env_project_name(),
                    PROJECT_TYPE: ProjectType.EXPERIMENT.value,
                    PROJECT_VERSION_NAME: get_env_project_version_name(),
                }
            )
        super().__init__(*bound_args.args, **bound_args.kwargs)

        self._default_processor = False


        if transport == Transport.HTTP:
            endpoint = get_env_traces_endpoint()
            parsed_url, endpoint = _normalized_endpoint(endpoint)
            exporter: SpanExporter = HTTPSpanExporter(endpoint=endpoint)
        elif transport == Transport.GRPC:
            if not _GRPC_INSTALLED:
                raise RuntimeError(
                    "gRPC transport is not available. "
                    'Please install the gRPC dependencies with: pip install "fi-instrumentation-otel[grpc]"'
                )
            endpoint = get_env_grpc_collector_endpoint()
            exporter: SpanExporter = GRPCSpanExporter(endpoint=endpoint)
        else:
            raise ValueError(f"Invalid transport: {transport}")

        self.add_span_processor(SimpleSpanProcessor(span_exporter=exporter))
        self._default_processor = True
        if verbose:
            print(self._tracing_details())

    def add_span_processor(self, *args: Any, **kwargs: Any) -> None:
        """
        Registers a new `SpanProcessor` for this `TracerProvider`.

        On a `TracerProvider` constructed directly, the first call replaces the default
        `SimpleSpanProcessor` added on construction. Any other processor is kept, including the
        one `register()` adds: the new processor runs next to it, as in upstream OpenTelemetry.
        """

        if self._default_processor:
            self._active_span_processor.shutdown()
            self._active_span_processor._span_processors = tuple()
            self._default_processor = False
        return super().add_span_processor(*args, **kwargs)

    def _tracing_details(self) -> str:
        project = self.resource.attributes.get(PROJECT_NAME)
        project_type = self.resource.attributes.get(PROJECT_TYPE)
        project_version_name = self.resource.attributes.get(PROJECT_VERSION_NAME)
        eval_tags = self.resource.attributes.get(EVAL_TAGS)

        processor_name: Optional[str] = None
        endpoint: Optional[str] = None
        transport: Optional[str] = None
        headers: Optional[Union[Dict[str, str], str]] = None

        if self._active_span_processor:
            if processors := self._active_span_processor._span_processors:
                if len(processors) == 1:
                    span_processor = self._active_span_processor._span_processors[0]
                    if exporter := getattr(span_processor, "span_exporter", None):
                        processor_name = span_processor.__class__.__name__
                        endpoint = getattr(exporter, "_endpoint", None)
                        transport = _exporter_transport(exporter)
                        headers = _printable_headers(getattr(exporter, "_headers", {}) or {})
                else:
                    processor_name = "Multiple Span Processors"
                    endpoint = "Multiple Span Exporters"
                    transport = "Multiple Span Exporters"
                    headers = "Multiple Span Exporters"

        if os.name == "nt":
            details_header = "OpenTelemetry Tracing Details"
        else:
            details_header = "🔭 OpenTelemetry Tracing Details 🔭"

        configuration_msg = "|  Using a default SpanProcessor. `add_span_processor` will overwrite this default.\n"

        details_msg = (
            f"{details_header}\n"
            f"|  FI Project: {project}\n"
            f"|  FI Project Type: {project_type}\n"
            f"|  FI Project Version Name: {project_version_name}\n"
            f"|  Span Processor: {processor_name}\n"
            f"|  Collector Endpoint: {endpoint}\n"
            f"|  Transport: {transport}\n"
            f"|  Transport Headers: {headers}\n"
            f"|  Eval Tags: {eval_tags}\n"
            "|  \n"
            f"{configuration_msg if self._default_processor else ''}"
        )
        return details_msg

    def shutdown(self) -> None:
        """Override shutdown to force flush first"""
        # Force flush to ensure all processors export their spans
        if hasattr(self, "force_flush"):
            try:
                self.force_flush(timeout_millis=5000)
            except Exception as e:
                print(f"Error during force flush: {e}")

        # Call the parent shutdown method
        super().shutdown()

    def setup_signal_handlers(self):
        """Register normal-exit cleanup without changing application signal policy.

        Applications that handle termination signals can explicitly flush or
        shut down their provider in their own handler. Default SIGTERM does not
        run Python's atexit handlers.
        """
        atexit.register(self.shutdown)
        return self


def _auto_set_ok_status(span: Any) -> None:
    """
    Auto-set OK status on spans that have UNSET status and no error events.

    Called in SpanProcessor.on_end() before the span is exported. At this point
    the span is no longer recording so we set _status directly.
    """
    if not hasattr(span, "status"):
        return
    if span.status.status_code != StatusCode.UNSET:
        return
    has_error = any(
        event.name in ("exception", "error")
        for event in (getattr(span, "events", None) or [])
    )
    if not has_error:
        span._status = Status(StatusCode.OK)


def _build_default_exporter(
    transport: Transport,
    headers: Optional[Dict[str, str]],
    timeout: Optional[float],
) -> SpanExporter:
    """Build the default OTLP exporter for `transport` (endpoint from env)."""
    if transport == Transport.HTTP:
        _, endpoint = _normalized_endpoint(get_env_traces_endpoint())
        return HTTPSpanExporter(endpoint=endpoint, headers=headers, timeout=timeout)
    if transport == Transport.GRPC:
        endpoint = get_env_grpc_collector_endpoint()
        return GRPCSpanExporter(endpoint=endpoint, headers=headers, timeout=timeout)
    raise ValueError(f"Invalid transport: {transport}")


class SimpleSpanProcessor(_SimpleSpanProcessor):
    """
    Simple SpanProcessor implementation.

    SimpleSpanProcessor is an implementation of `SpanProcessor` that passes ended spans directly to
    the configured `SpanExporter`.

    Args:
        span_exporter (SpanExporter, optional): The `SpanExporter` to which ended spans will be
            passed. If not provided, a default OTLP exporter is created for `transport`; its
            collector endpoint is taken from the `FI_TRACES_URL` (else `FI_BASE_URL`)/`FI_GRPC_URL` environment
            variables.
        headers (dict, optional): Optional headers to include in the request to the collector.
            If not provided, the `FI_API_KEY` and `FI_SECRET_KEY`
            environment variable will be used.
        transport (Transport, optional): Transport (HTTP or gRPC) used for the default exporter.
        timeout (float, optional): Per-export timeout in seconds for the default exporter.
    """

    def __init__(
        self,
        span_exporter: Optional[SpanExporter] = None,
        headers: Optional[Dict[str, str]] = None,
        transport: Transport = Transport.HTTP,
        timeout: Optional[float] = None,
    ):
        self._active_spans = {}
        self._shutdown_lock = threading.Lock()

        if span_exporter is None:
            span_exporter = _build_default_exporter(transport, headers, timeout)

        super().__init__(span_exporter)

    def on_start(self, span: Any, parent_context: Optional[Any] = None) -> None:
        """Track span when it starts"""
        if hasattr(span, "context") and hasattr(span.context, "span_id"):
            max_active_spans_tracked = int(
                os.getenv(FI_MAX_ACTIVE_SPANS_TRACKED, DEFAULT_MAX_ACTIVE_SPANS_TRACKED)
            )
            if len(self._active_spans) >= max_active_spans_tracked:
                return
            self._active_spans[span.context.span_id] = span

        super().on_start(span, parent_context)

    def on_end(self, span: Any) -> None:
        """Auto-set OK status for UNSET spans and remove from tracking."""
        _auto_set_ok_status(span)

        if hasattr(span, "context") and hasattr(span.context, "span_id"):
            self._active_spans.pop(span.context.span_id, None)

        super().on_end(span)

    def shutdown(self) -> None:
        """Override shutdown to ensure all active spans get exported"""
        try:
            with self._shutdown_lock:
                if self._active_spans:
                    logger.warning("Ending %d active spans during shutdown", len(self._active_spans))
                    active_spans = list(self._active_spans.values())

                    for span in active_spans:
                        if hasattr(span, "is_recording") and span.is_recording():
                            try:
                                span.set_attribute("gen_ai.span.leaked", True)
                                span.end()
                            except Exception:
                                pass

                    self._active_spans.clear()
        finally:
            super().shutdown()


class BatchSpanProcessor(_BatchSpanProcessor):
    """
    Batch SpanProcessor implementation.

    `BatchSpanProcessor` is an implementation of `SpanProcessor` that batches ended spans and
    pushes them to the configured `SpanExporter`.

    `BatchSpanProcessor` is configurable with the following environment variables which correspond
    to constructor parameters:

    - :envvar:`OTEL_BSP_SCHEDULE_DELAY`
    - :envvar:`OTEL_BSP_MAX_QUEUE_SIZE`
    - :envvar:`OTEL_BSP_MAX_EXPORT_BATCH_SIZE`
    - :envvar:`OTEL_BSP_EXPORT_TIMEOUT`

    Args:
        span_exporter (SpanExporter, optional): The `SpanExporter` to which ended spans will be
            passed. If not provided, a default OTLP exporter is created for `transport`; its
            collector endpoint is taken from the `FI_TRACES_URL` (else `FI_BASE_URL`)/`FI_GRPC_URL` environment
            variables.
        headers (dict, optional): Optional headers to include in the request to the collector.
            If not provided, the `FI_API_KEY` and `FI_SECRET_KEY`
            environment variable will be used.
        transport (Transport, optional): Transport (HTTP or gRPC) used for the default exporter.
        max_queue_size (int, optional): The maximum queue size.
        schedule_delay_millis (float, optional): The delay between two consecutive exports in
            milliseconds.
        max_export_batch_size (int, optional): The maximum batch size.
        export_timeout_millis (float, optional): The batch export timeout in milliseconds.
        timeout (float, optional): Per-export timeout in seconds for the default exporter.
    """

    def __init__(
        self,
        span_exporter: Optional[SpanExporter] = None,
        headers: Optional[Dict[str, str]] = None,
        transport: Transport = Transport.HTTP,
        max_queue_size: Optional[int] = None,
        schedule_delay_millis: Optional[float] = None,
        max_export_batch_size: Optional[int] = None,
        export_timeout_millis: Optional[float] = None,
        timeout: Optional[float] = None,
    ):
        if span_exporter is None:
            span_exporter = _build_default_exporter(transport, headers, timeout)

        super().__init__(
            span_exporter,
            max_queue_size=max_queue_size,
            schedule_delay_millis=schedule_delay_millis,
            max_export_batch_size=max_export_batch_size,
            export_timeout_millis=export_timeout_millis,
        )

    def on_end(self, span: Any) -> None:
        """Auto-set OK status for UNSET spans before batching."""
        _auto_set_ok_status(span)
        super().on_end(span)


_CREDENTIAL_HEADERS = (AUTHORIZATION, "x-api-key", "x-secret-key")


def _is_blank(value: Any) -> bool:
    """None, or a str/bytes value that is empty after stripping whitespace."""
    if value is None:
        return True
    if isinstance(value, (bytes, bytearray)):
        return not bytes(value).strip()
    return not str(value).strip()


def _with_fi_auth_headers(headers: Any, add_env_keys: bool = True) -> Dict[str, Any]:
    """Lower-case explicit headers and add the FI_API_KEY/FI_SECRET_KEY pair once.

    The environment pair is added only when ``add_env_keys`` is true, which the
    exporters set for a Future AGI endpoint; a collector the user chose does not
    receive the Future AGI keys.

    Explicit credentials win: the environment pair is added only when no
    Authorization, X-Api-Key or X-Secret-Key header was passed. A credential
    header with an empty value is dropped rather than treated as a credential,
    so an empty Authorization no longer hides valid environment keys. Keys are
    lower-cased on both sides, so the request carries no case-variant
    duplicates such as ``x-api-key`` and ``X-Api-Key``.
    """
    pairs = () if not headers else (headers.items() if isinstance(headers, Mapping) else headers)
    merged: Dict[str, Any] = {}
    for key, value in pairs:
        name = (key.decode() if isinstance(key, bytes) else str(key)).lower()
        # Skip a blank credential before collapsing case variants, so an empty
        # duplicate cannot erase an explicit one and pull in the env pair.
        if name in _CREDENTIAL_HEADERS and _is_blank(value):
            continue
        merged[name] = value
    if add_env_keys and not any(name in merged for name in _CREDENTIAL_HEADERS):
        auth_header = get_env_fi_auth_header()
        if auth_header:
            merged.update({k.lower(): v for k, v in auth_header.items()})
    return merged


_AMBIENT_HEADER_ENV_VARS = ("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS")
_MISSING = object()


def _ambient_headers() -> dict[str, str]:
    """Headers named by the OTLP environment variables, keyed by lower-case name.

    Upstream OpenTelemetry exporters read these when they are built (and, from
    1.4x, merge them under the headers passed in). They are meant for the
    collector the user configured, which is not necessarily Future AGI.
    """
    from opentelemetry.util.re import parse_env_headers

    found: dict[str, str] = {}
    for var in reversed(_AMBIENT_HEADER_ENV_VARS):
        raw = os.environ.get(var)
        if raw:
            found.update({k.lower(): v for k, v in parse_env_headers(raw, liberal=True).items()})
    return found


def _materialize_headers(headers: Any, lower: bool = False) -> dict[str, Any] | None:
    """The caller's headers read once into a dict (None when there are none).

    Accepts a mapping, a sequence or one-shot iterator of pairs, or the comma-separated
    ``name=value`` string gRPC exporters take. With ``lower`` the names are lower-cased, as
    gRPC requires; values are untouched, and entries whose value is None are left out.
    """
    if headers is None:
        return None
    if isinstance(headers, str):
        from opentelemetry.util.re import parse_env_headers

        pairs: Any = parse_env_headers(headers, liberal=True).items()
    else:
        pairs = headers.items() if isinstance(headers, Mapping) else headers
    result: dict[str, Any] = {}
    given: dict[str, str] = {}  # lower-case name -> the name as it appears in result
    for key, value in pairs:
        if value is None:
            continue
        name = key.decode() if isinstance(key, bytes) else str(key)
        folded = name.lower()
        if folded in given:
            # Names that differ only in case are one header: a blank duplicate does not replace a value
            # that was given, and a given value replaces an earlier blank one.
            if _is_blank(value):
                continue
            del result[given[folded]]
        result[folded if lower else name] = value
        given[folded] = folded if lower else name
    return result or None


def _caller_headers(headers: Any) -> dict[str, Any]:
    """The headers a caller passed to an exporter, keyed by lower-case name.

    Read by the same rules as ``_materialize_headers``: names that differ only in case are one header,
    a blank duplicate does not replace a given value, and a value of None is left out.
    """
    return _materialize_headers(headers, lower=True) or {}


class UnsupportedFutureAGIEndpointError(ValueError):
    """An endpoint that names Future AGI but cannot be verified, used with Future AGI credentials."""


_HOST_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?")
_PLAIN_GRPC_TARGET = re.compile(r"([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::([0-9]{1,5}))?")


def _normalize_host(host: str | None) -> str | None:
    """A host name in one comparable form, or None when it is not a plain ASCII DNS name or address.

    Lower case, one trailing dot removed, labels of letters, digits and hyphens only. A name
    that does not normalise matches nothing.
    """
    if not host or not host.isascii():
        return None
    host = host.lower().removesuffix(".")
    if host.startswith("[") and host.endswith("]"):
        return host if re.fullmatch(r"\[[0-9a-f:.]+\]", host) else None
    labels = host.split(".")
    return host if host and all(_HOST_LABEL.fullmatch(label) for label in labels) else None


_DOT_FORMS = "\u3002\uff0e\uff61"


def _host_forms(token: str) -> set[str]:
    """The spellings of one host token a client may resolve: unescaped, width-folded, dots unified, ``xn--`` labels decoded."""
    import unicodedata

    forms: set[str] = set()
    for variant in {token, unquote(token), unicodedata.normalize("NFKC", token), unicodedata.normalize("NFKC", unquote(token))}:
        text = variant.lower()
        for dot in _DOT_FORMS:
            text = text.replace(dot, ".")
        text = text.rstrip(".")
        forms.add(text)
        decoded = []
        for label in text.split("."):
            if label.startswith("xn--"):
                try:
                    label = unicodedata.normalize("NFKC", label[4:].encode("ascii").decode("punycode")).lower()
                except (UnicodeError, ValueError):
                    pass
            decoded.append(label)
        forms.add(".".join(decoded))
    return forms


def _names_future_agi_host(endpoint: str, grpc: bool = False) -> bool:
    """True when the host of ``endpoint``, in any escaping, width or ``xn--`` spelling, is futureagi.com or a subdomain.

    Only the host is read: an HTTP URL's authority, or the whole gRPC target (its grammar can place the host after a
    prefix). A path, a query, a fragment or a longer name such as ``myfutureagi.corp`` does not count.
    """
    if grpc:
        text = endpoint
    else:
        rest = endpoint.split("//", 1)[1] if "//" in endpoint else endpoint
        text = re.split(r"[/?#]", rest, maxsplit=1)[0]
    candidates = {text}
    unescaped = text
    for _ in range(3):
        unescaped = unquote(unescaped)
        candidates.add(unescaped)
    for candidate in candidates:
        for token in re.split(r"[@\\:/?#,\s]+", candidate):
            if token and any(_is_futureagi_host(form) for form in _host_forms(token)):
                return True
    return False


def _http_origin(endpoint: str | None) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an http(s) URL on which urlsplit and urllib3 agree, else None.

    The URL carries no userinfo, no backslash and no percent-escape in its authority; the port is
    explicit or the scheme's default.
    """
    if not endpoint or "\\" in endpoint or any(ord(c) < 33 or ord(c) == 127 for c in endpoint):
        return None
    try:
        import urllib3.util

        split = urlsplit(endpoint)
        parsed = urllib3.util.parse_url(endpoint)
        scheme = split.scheme.lower()
        if scheme not in ("http", "https") or "@" in split.netloc or "%" in split.netloc or parsed.auth:
            return None
        host = _normalize_host(split.hostname)
        if host is None or host != _normalize_host(parsed.host):
            return None
        if split.port != parsed.port:
            return None
        return scheme, host, split.port or (80 if scheme == "http" else 443)
    except (ValueError, ImportError):  # an endpoint the parsers cannot read matches nothing
        return None


def _grpc_target(endpoint: str | None) -> tuple[str, str, int] | None:
    """(scheme, host, port) of the exact target grpc receives, or None unless it is a plain host[:port].

    The exporters pass ``urlparse(endpoint).netloc`` to grpc when there is one, so that is the string read
    here; a target-grammar prefix (dns:, ipv4:, unix:, xds: ...), userinfo or a backslash is not plain.
    """
    if not endpoint:
        return None
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return None
    target = parsed.netloc or endpoint
    scheme = parsed.scheme.lower() if parsed.netloc else ""
    match = _PLAIN_GRPC_TARGET.fullmatch(target)
    if match is None:
        return None
    host = _normalize_host(match.group(1))
    if host is None:
        return None
    return scheme, host, int(match.group(2)) if match.group(2) else 443


def _known_future_agi_origins(grpc: bool) -> set[tuple[str, int]]:
    """The (host, port) pairs the Future AGI environment and defaults name.

    For gRPC the port of a host is the one ``FI_GRPC_URL`` names for it, otherwise 443; an endpoint argument and
    ``FI_GRPC_URL`` are held to the same rule.
    """
    known: set[tuple[str, int]] = set()
    if grpc:
        named = _grpc_target(get_env_grpc_collector_endpoint())
        if named:
            known.add((named[1], named[2]))
        if not named or named[1] != "grpc.futureagi.com":
            known.add(("grpc.futureagi.com", 443))
    else:
        candidates = [get_env_traces_endpoint(), get_env_collector_endpoint(), "https://api.futureagi.com"]
        for url in candidates:
            origin = _http_origin(url)
            if origin:
                known.add((origin[1], origin[2]))
    return known


def _is_futureagi_host(host: str) -> bool:
    """True for futureagi.com and its subdomains; one trailing dot is read as absent."""
    host = host.lower().removesuffix(".")
    return host == "futureagi.com" or host.endswith(".futureagi.com")


_CREDENTIAL_ESCAPES = ("%5c", "%40")


def _authority_text(endpoint: str) -> str:
    """The authority of a URL or target as written: the text after ``//`` up to ``/``, ``?`` or ``#``, else the leading
    token of a bare ``host:port``."""
    rest = endpoint.split("//", 1)[1] if "//" in endpoint else endpoint
    return re.split(r"[/?#]", rest, maxsplit=1)[0]


def _is_disputed(endpoint: str) -> bool:
    """True when parsers can read the authority of ``endpoint`` differently, so no parser's reading is trusted.

    That is the case for whitespace or control characters, a backslash or ``@`` (userinfo, or text that follows an
    ``@``) or an escaped backslash or ``@`` in the endpoint, an empty authority that a path follows (the host is not
    re-read from the path), and a host that ``urlsplit``, ``urllib3`` and the written text read differently.
    """
    if any(ord(c) < 33 or ord(c) == 127 for c in endpoint) or "\\" in endpoint or "@" in endpoint:
        return True
    lowered = endpoint.lower()
    if any(escape in lowered for escape in _CREDENTIAL_ESCAPES):
        return True
    if "//" in endpoint and not _authority_text(endpoint) and endpoint.split("//", 1)[1].strip("/"):
        return True
    if "//" in endpoint:
        try:
            import urllib3.util

            hosts = {
                host.lower().strip("[]").removesuffix(".")
                for host in (urlsplit(endpoint).hostname or "", urllib3.util.parse_url(endpoint).host or "", _written_host(endpoint) or "")
            }
        except Exception:  # a parser that cannot read the endpoint disagrees with the others
            return True
        if len(hosts) > 1:
            return True
    return False


def _is_env_named(endpoint: str) -> bool:
    """True when the authority of ``endpoint`` is the authority of ``FI_BASE_URL``, ``FI_TRACES_URL`` or ``FI_GRPC_URL``."""
    wanted = _authority_text(endpoint)
    return any(
        value and _authority_text(value) == wanted
        for value in (get_env_traces_endpoint(), get_env_collector_endpoint(), get_env_grpc_collector_endpoint())
    )


def _future_agi_decision(endpoint: str | None, grpc: bool = False, named: bool = False) -> str:
    """``"fi"`` for a Future AGI endpoint, ``"refuse"`` for one that names Future AGI but cannot be
    verified, ``"other"`` for a collector of the user's own.

    ``endpoint`` is the URL (HTTP) or the target an exporter received or resolved (gRPC; either
    spelling). It is Future AGI only when it is a plain target on exact host and port the Future AGI
    environment or defaults name. A Future AGI HTTP host is reached over https only; a gRPC target of any
    scheme is bound and uses TLS (see ``_force_tls_for_future_agi``). An endpoint whose authority parsers can read
    differently (see ``_is_disputed``) is refused when it names Future AGI or is the one the environment names
    (``named``), whichever parser reads it.
    """
    if not endpoint:
        return "other"
    if _is_disputed(endpoint) and (_names_future_agi_host(endpoint, True) or named or _is_env_named(endpoint)):
        return "refuse"
    if grpc:
        target = _grpc_target(endpoint)
        host = target[1] if target else None
        origin = (target[1], target[2]) if target else None
        plain_http = False
    else:
        parsed = _http_origin(endpoint)
        host = parsed[1] if parsed else None
        origin = (parsed[1], parsed[2]) if parsed else None
        plain_http = bool(parsed and parsed[0] == "http")
    if origin is not None and origin in _known_future_agi_origins(grpc):
        # A futureagi.com HTTP host is reached over https; a self-hosted base the environment names may use http.
        return "refuse" if plain_http and _is_futureagi_host(host or "") else "fi"
    if _names_future_agi_host(endpoint, grpc) or (host is not None and _is_futureagi_host(host)):
        return "refuse"
    return "other"


def _is_future_agi_endpoint(endpoint: str | None, grpc: bool | None = None) -> bool:
    """True when ``endpoint`` is a Future AGI endpoint (see _future_agi_decision).

    Without ``grpc`` the endpoint may be either kind: a URL or a plain host:port target.
    """
    if grpc is None:
        return _future_agi_decision(endpoint, False) == "fi" or _future_agi_decision(endpoint, True) == "fi"
    return _future_agi_decision(endpoint, grpc) == "fi"


def _fi_keys_configured(caller_headers: Any = None) -> bool:
    """True when Future AGI credentials would be sent: from the environment or the caller's headers."""
    if get_env_fi_auth_header():
        return True
    final = {**_ambient_headers(), **_caller_headers(caller_headers)}
    return any(name in _CREDENTIAL_HEADERS and not _is_blank(value) for name, value in final.items())


def _check_future_agi_endpoint(endpoint: str | None, grpc: bool, caller_headers: Any = None, named: bool = False) -> str:
    """The decision for ``endpoint``; raises when it names Future AGI and credentials are set. The credentials are the
    final outgoing set: the environment keys, the caller's headers and the OpenTelemetry environment headers."""
    decision = _future_agi_decision(endpoint, grpc, named)
    if decision == "refuse" and _fi_keys_configured(caller_headers):
        raise UnsupportedFutureAGIEndpointError(
            "The exporter endpoint names Future AGI but cannot be verified as a Future AGI endpoint, so the Future AGI "
            "credentials are not sent to it. Set FI_BASE_URL (or the endpoint option) to a plain https Future AGI URL "
            "with no userinfo and the default port, or remove the credentials."
        )
    return decision


def _guard_future_agi_request(
    url: str, named: bool = False, allow: bool = False, session: Any = None, base: str | None = None, from_env: bool = False
) -> bool:
    """True when Future AGI keys may be attached to an HTTP request to ``url``.

    The endpoint rules of the exporters apply before any request: a Future AGI host reached over http, or in a form that
    cannot be verified, raises UnsupportedFutureAGIEndpointError (which names FI_BASE_URL) when Future AGI credentials are
    set. ``named`` marks a URL the user configured as their Future AGI base, which is then treated as self-hosted when
    it is not a futureagi.com host. A plain http base on a host other than loopback is refused unless it is opted in
    (see ``_authorize_plain_text``).
    """
    # The base as written is checked first (``from_env``: it is the one the environment names), then the request URL.
    _check_future_agi_endpoint(base or url, False, named=named or from_env)
    decision = _check_future_agi_endpoint(url, False)
    if decision == "other" and named and _http_origin(url) is not None:
        decision = "fi"
    if decision != "fi":
        return False
    _authorize_http_endpoint(url, allow, session)
    return True


_SUPPORTED_OTLP_EXPORTERS = "1.29.0 to 1.45.x"


class UnsupportedOTLPExporterError(RuntimeError):
    """An exporter bound for Future AGI whose headers this release cannot set."""


def _unsupported_exporter(exporter: Any, package: str) -> UnsupportedOTLPExporterError:
    """The error for an exporter bound for Future AGI whose headers cannot be pinned.

    The message names the exporter class, the package version and the supported
    versions, and no header or environment value.
    """
    try:
        from importlib.metadata import version

        installed = version(package)
    except Exception:  # pragma: no cover - metadata unavailable
        installed = "unknown version"
    return UnsupportedOTLPExporterError(
        f"Cannot send traces to Future AGI with {type(exporter).__name__} from {package} {installed}: "
        "its header handling is not one this release can set, so headers from "
        "OTEL_EXPORTER_OTLP_HEADERS cannot be kept out of its requests. "
        f"Supported versions are {_SUPPORTED_OTLP_EXPORTERS}."
    )


def _copy_session(session: requests.Session) -> requests.Session:
    """A new session with the caller's settings and adapters, whose headers can be changed freely.

    The copy is an instance of the caller's session class when that class can be created without arguments, and a
    ``requests.Session`` otherwise.
    """
    try:
        copy = type(session)()
    except (TypeError, ValueError, AttributeError):  # a subclass that needs arguments
        copy = requests.Session()
    copy.headers = session.headers.copy()
    copy.auth = session.auth
    copy.proxies = dict(session.proxies)
    copy.hooks = {name: list(hooks) for name, hooks in session.hooks.items()}
    copy.params = session.params
    copy.stream = session.stream
    copy.verify = session.verify
    copy.cert = session.cert
    copy.max_redirects = session.max_redirects
    copy.trust_env = session.trust_env
    copy.cookies = session.cookies.copy()
    # Fresh adapters with the same settings: closing the exporter closes these, not the caller's connection pools.
    copy.adapters = type(session.adapters)()
    for prefix, adapter in session.adapters.items():
        settings_of = {
            "pool_connections": getattr(adapter, "_pool_connections", 10),
            "pool_maxsize": getattr(adapter, "_pool_maxsize", 10),
            "max_retries": getattr(adapter, "max_retries", 0),
            "pool_block": getattr(adapter, "_pool_block", False),
        }
        try:
            fresh = type(adapter)(**settings_of)
        except (TypeError, ValueError):  # an adapter class with its own constructor
            fresh = requests.adapters.HTTPAdapter(**settings_of)
        copy.adapters[prefix] = fresh
    return copy


def _pin_http_headers(
    exporter: Any, sdk_headers: Mapping[str, Any], caller_headers: Any = None, caller_session: Any = None
) -> None:
    """Make an HTTP exporter send the OTLP defaults plus exactly ``sdk_headers``.

    OpenTelemetry 1.29 sends from ``exporter._session.headers`` (and
    ``exporter._headers``); both take environment headers only when no headers
    are passed. From 1.4x the sent headers live in ``exporter._client._headers``
    and always include the environment headers. Both are rewritten here, so the
    environment is not read again at export time and ``os.environ`` is untouched.
    An exporter with neither layout raises UnsupportedOTLPExporterError, so it
    cannot send.
    """
    try:
        from opentelemetry.exporter.otlp.proto.http import _OTLP_HTTP_HEADERS
    except ImportError:  # pragma: no cover - layout of an unknown release
        _OTLP_HTTP_HEADERS = {}
    defaults = {k.lower(): v for k, v in _OTLP_HTTP_HEADERS.items()}
    sdk = {str(k).lower(): v for k, v in sdk_headers.items()}
    # Headers the exporter took from the environment or from the caller, which the SDK headers replace.
    ambient = {**_ambient_headers(), **_caller_headers(caller_headers)}

    pinned_any = False
    if caller_session is not None and getattr(exporter, "_session", None) is caller_session:
        # The Future AGI headers go on a copy: the caller's own session is left as the caller built it.
        exporter._session = _copy_session(caller_session)
    client = getattr(exporter, "_client", None)
    current = getattr(client, "_headers", None)
    if isinstance(current, dict):
        pinned_any = True
        pinned = dict(defaults)
        # The client adds Content-Encoding for the compression in use; it is not an environment header.
        for key, value in current.items():
            if key.lower() == "content-encoding" and "content-encoding" not in ambient:
                pinned["content-encoding"] = value
        pinned.update(sdk)
        current.clear()
        current.update(pinned)

    session = getattr(exporter, "_session", None)
    session_headers = getattr(session, "headers", None)
    if session_headers is not None and isinstance(getattr(exporter, "_headers", None), dict):
        pinned_any = True
        for name, value in ambient.items():
            if name not in sdk and session_headers.get(name) == value:
                del session_headers[name]
                if name in defaults:
                    session_headers[name] = defaults[name]
        session_headers.update(sdk)
        exporter._headers = dict(sdk)
    if not pinned_any:
        raise _unsupported_exporter(exporter, "opentelemetry-exporter-otlp-proto-http")


def _pin_grpc_headers(exporter: Any, sdk_headers: Mapping[str, Any], caller_headers: Any = None) -> None:
    """Make a gRPC exporter send its metadata from exactly ``sdk_headers``.

    The metadata is ``exporter._headers`` in 1.29 and 1.4x. Both take environment
    headers whenever no headers are passed, so the pairs that came from the
    environment are removed and the SDK's are set. ``None`` means no metadata;
    any other layout raises UnsupportedOTLPExporterError, so it cannot send.
    """
    sdk = {str(k).lower(): v for k, v in sdk_headers.items()}
    current = getattr(exporter, "_headers", _MISSING)
    if current is None:
        exporter._headers = tuple(sdk.items()) or None
        return
    if not isinstance(current, (tuple, list)):
        raise _unsupported_exporter(exporter, "opentelemetry-exporter-otlp-proto-grpc")
    ambient = {**_ambient_headers(), **_caller_headers(caller_headers)}
    kept = [
        (k, v)
        for k, v in current
        if str(k).lower() not in sdk and not (str(k).lower() in ambient and ambient[str(k).lower()] == v)
    ]
    exporter._headers = tuple(kept) + tuple(sdk.items())


def _written_host(endpoint: str | None) -> str | None:
    """The host of a URL or gRPC target as written, lower-cased and without brackets, before any normalisation.

    One trailing dot stays on the host, and forms a parser rewrites (``127.1``, ``0x7f.0.0.1``, ``2130706433``) stay as
    written. Every loopback decision reads this host.
    """
    if not endpoint:
        return None
    try:
        split = urlsplit(endpoint)
        text = split.netloc if split.netloc else endpoint
        text = text.rsplit("@", 1)[-1]
        if text.startswith("["):
            return text[1 : text.index("]")].lower() if "]" in text else None
        return text.split(":", 1)[0].lower() or None
    except ValueError:
        return None


_LOOPBACK_FORMS = ("localhost", "127.0.0.1", "::1")


def _is_loopback_host(host: str | None) -> bool:
    """True only for the literal forms ``localhost``, ``127.0.0.1`` and ``::1`` (``[::1]``), as written.

    A form a parser rewrites into loopback (``127.1``, ``0x7f.0.0.1``, ``2130706433``), a trailing dot
    (``127.0.0.1.``, ``localhost.``) and a name that merely ends in ``.localhost`` are not loopback: they are treated
    like any other self-hosted host.
    """
    return bool(host) and host.strip("[]").lower() in _LOOPBACK_FORMS


def _plain_text_requested(target: tuple[str, str, int], insecure: Any) -> bool:
    """True when the settings ask for a plain gRPC channel to ``target``, in the order the exporter reads them:
    the ``insecure`` argument, ``OTEL_EXPORTER_OTLP_TRACES_INSECURE``, ``OTEL_EXPORTER_OTLP_INSECURE``, then an
    ``http`` scheme. An ``https`` scheme is TLS."""
    if target[0] == "https":
        return False
    if insecure is not None:
        return bool(insecure)
    for name in ("OTEL_EXPORTER_OTLP_TRACES_INSECURE", "OTEL_EXPORTER_OTLP_INSECURE"):
        value = os.environ.get(name)
        if value is not None:
            return value.lower() == "true"
    return target[0] == "http"


def _force_tls_for_future_agi(endpoint: str | None, insecure: Any, allow: bool = False) -> bool:
    """True when a gRPC exporter for this Future AGI target is built with TLS whatever the plain-text settings say.

    A futureagi.com target always uses TLS, whatever its scheme, ``OTEL_EXPORTER_OTLP_INSECURE`` and
    ``OTEL_EXPORTER_OTLP_TRACES_INSECURE`` say; asking for plain text with the ``insecure`` argument raises
    UnsupportedFutureAGIEndpointError, which names the argument. Loopback keeps the configured channel. Any other
    host uses TLS unless the settings ask for plain text, which needs the opt-in (see ``_authorize_plain_text``);
    with it, the configured plain channel is kept.
    """
    target = _grpc_target(endpoint)
    if target is None or _is_loopback_host(_written_host(endpoint)):
        return False
    if not _is_futureagi_host(target[1]):
        if _plain_text_requested(target, insecure):
            _authorize_plain_text(_written_host(endpoint) or target[1], target[2], allow, grpc=True)
            return False
        return True
    if insecure:
        raise UnsupportedFutureAGIEndpointError(
            "A Future AGI gRPC endpoint is reached over TLS, so the insecure option cannot be turned on for it. "
            "Remove the insecure option, or set FI_GRPC_URL to your own collector."
        )
    return True


_PLAIN_TEXT_WARNED: set[str] = set()
_ALLOW_INSECURE_HTTP = "FI_ALLOW_INSECURE_HTTP"


def _grpc_bypassed(host: str, no_proxy: str) -> bool:
    """True when ``host`` is on the ``no_grpc_proxy``/``no_proxy`` list as grpcio reads it (measured on grpcio 1.63, 1.70 and 1.85).

    Entries are comma separated and trimmed, matched without regard to case: an exact host, a whole-label suffix (``internal``
    or ``.internal``), an exact address, or a network range for an address host. An entry with a port, ``*`` and a partial
    label do not match. Only matches that every measured release agrees on are counted.
    """
    import ipaddress

    host = host.strip("[]").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    for entry in (item.strip().lower() for item in no_proxy.split(",")):
        if not entry or "*" in entry:
            continue
        if address is not None:
            try:
                if "/" in entry:
                    if address in ipaddress.ip_network(entry, strict=False):
                        return True
                elif address == ipaddress.ip_address(entry):
                    return True
                continue
            except ValueError:
                continue
        if ":" in entry or "/" in entry:
            continue
        if entry.startswith("."):
            if host.endswith(entry):
                return True
        elif host == entry or host.endswith("." + entry):
            return True
    return False


def _grpc_proxy_applies(host: str) -> bool:
    """True when grpcio routes a plain channel to ``host`` through a proxy.

    grpcio reads only lower-case names, in this order: ``grpc_proxy``, ``https_proxy``, ``http_proxy``; the first one that is
    set decides, and an empty one means no proxy. ``no_grpc_proxy``, when set (even empty), is the bypass list; otherwise
    ``no_proxy``. ``ALL_PROXY`` and the upper-case names are not read. Any non-empty proxy value counts as a proxy.
    """
    proxy = next((os.environ[name] for name in ("grpc_proxy", "https_proxy", "http_proxy") if name in os.environ), "")
    if not proxy.strip():
        return False
    bypass = os.environ["no_grpc_proxy"] if "no_grpc_proxy" in os.environ else os.environ.get("no_proxy", "")
    return not _grpc_bypassed(host, bypass)


def _proxy_applies(host: str, port: int, session: Any = None, grpc: bool = False) -> bool:
    """True when a plain (http or non-TLS gRPC) request to ``host:port`` would go through a proxy.

    HTTP: ``HTTP_PROXY`` or ``ALL_PROXY`` (upper or lower case, non-empty) routes it unless ``NO_PROXY`` bypasses the
    host, read as the standard library reads it (``urllib.request.proxy_bypass_environment``: a host or a suffix, with or
    without a leading dot, ``*``, ``host:port``; no network ranges). ``HTTPS_PROXY`` alone does not route a plain request.
    A caller session with proxies of its own always counts. gRPC: the rule grpcio reads (see ``_grpc_proxy_applies``).
    """
    import urllib.request

    if grpc:
        return _grpc_proxy_applies(host)
    if session is not None and any((getattr(session, "proxies", None) or {}).values()):
        return True
    proxies = urllib.request.getproxies_environment()
    if not (proxies.get("http") or proxies.get("all")):
        return False
    return not urllib.request.proxy_bypass_environment(f"{host}:{port}")


def _authorize_plain_text(host: str, port: int, allow: bool = False, session: Any = None, grpc: bool = False) -> None:
    """Allow Future AGI keys on a plain (not TLS) channel to ``host:port``, or raise UnsupportedFutureAGIEndpointError.

    Loopback needs no opt-in. Any other host needs ``FI_ALLOW_INSECURE_HTTP=1`` or the ``allow_insecure_http`` option;
    the opt-in does not apply when the request would go through a proxy. An allowed channel logs one warning per
    process that names the setting and the origin (scheme, host and port), and no userinfo, path, query or key.
    """
    if _is_loopback_host(host):
        return
    if not (allow or os.environ.get(_ALLOW_INSECURE_HTTP) == "1"):
        raise UnsupportedFutureAGIEndpointError(
            "Future AGI keys are not sent over a plain channel to a host other than loopback. Use https (or TLS), or set "
            f"{_ALLOW_INSECURE_HTTP}=1 (or the allow_insecure_http option) to allow it for your own self-hosted collector."
        )
    if _proxy_applies(host, port, session, grpc):
        names = "grpc_proxy, https_proxy or http_proxy, unless no_grpc_proxy or no_proxy lists the host" if grpc else (
            "HTTP_PROXY or ALL_PROXY, unless NO_PROXY lists the host")
        raise UnsupportedFutureAGIEndpointError(
            f"{_ALLOW_INSECURE_HTTP} does not apply when the request goes through a proxy ({names}). "
            "Use https (or TLS), or remove the proxy for this host."
        )
    if _PLAIN_TEXT_WARNED:
        return
    _PLAIN_TEXT_WARNED.add(f"{host}:{port}")
    logger.warning(
        "Future AGI keys are sent over a plain channel to http://%s:%s, allowed by %s. Use https (or TLS) to send them over TLS.",
        host,
        port,
        _ALLOW_INSECURE_HTTP,
    )


def _authorize_http_endpoint(endpoint: str | None, allow: bool = False, session: Any = None) -> None:
    """The plain-channel rule for an HTTP endpoint that carries Future AGI keys (see ``_authorize_plain_text``)."""
    origin = _http_origin(endpoint)
    if origin is not None and origin[0] == "http":
        _authorize_plain_text(_written_host(endpoint) or origin[1], origin[2], allow, session)


def _close_after_refusal(exporter: Any, base: Any) -> None:
    """Close what an exporter opened while it was being built, before its constructor raises."""
    steps = [lambda: base.shutdown(exporter)]
    for attribute in ("_channel", "_session"):
        held = getattr(exporter, attribute, None)
        if held is not None:
            steps.append(held.close)
    for step in steps:
        try:
            step()
        except (OSError, ValueError, AttributeError, RuntimeError):  # best effort; the refusal is what the caller needs
            logger.debug("exporter cleanup after a refusal failed", exc_info=True)


class GRPCSpanExporter(_GRPCSpanExporter):
    """
    OTLP span exporter using gRPC.
    Args:
        endpoint (str, optional): OpenTelemetry Collector receiver endpoint. If omitted or None,
            `FI_GRPC_URL` is used, defaulting to the Future AGI gRPC endpoint.
        headers: Headers to send when exporting. For a Future AGI endpoint, the
            `FI_API_KEY` and `FI_SECRET_KEY` environment variables are added when no
            credential header is given.
        allow_insecure_http (bool): Allow Future AGI keys over a plain channel to a self-hosted host other
            than loopback, like ``FI_ALLOW_INSECURE_HTTP=1``. It does not apply to a Future AGI host, or
            when the request goes through a proxy.
    """

    def __init__(self, *args: Any, allow_insecure_http: bool = False, **kwargs: Any):
        bound_args = _get_class_signature(_GRPCSpanExporter, "grpc").bind_partial(*args, **kwargs)
        # Read once; gRPC metadata names are lower case.
        caller_headers = _materialize_headers(bound_args.arguments.get("headers"), lower=True)
        if "headers" in bound_args.arguments:
            bound_args.arguments["headers"] = caller_headers

        from_env = bound_args.arguments.get("endpoint") is None
        if from_env:
            bound_args.arguments["endpoint"] = get_env_grpc_collector_endpoint()

        # Built once, with the caller's own arguments; the Future AGI headers are set afterwards,
        # and only when the exporter resolved a Future AGI endpoint.
        decision = _check_future_agi_endpoint(bound_args.arguments["endpoint"], True, caller_headers, named=from_env)
        forced = False
        if decision == "fi" and _force_tls_for_future_agi(
            bound_args.arguments["endpoint"], bound_args.arguments.get("insecure"), allow_insecure_http
        ):
            # TLS for a Future AGI target: the argument and both OTEL_EXPORTER_OTLP*_INSECURE variables are not read.
            bound_args.arguments["insecure"] = False
            forced = True
        super().__init__(*bound_args.args, **bound_args.kwargs)
        try:
            resolved = _check_future_agi_endpoint(getattr(self, "_endpoint", None), True, caller_headers)
            resolved_target = _grpc_target(getattr(self, "_endpoint", None))
            written = _written_host(getattr(self, "_endpoint", None))
            if resolved == "fi" and not forced and resolved_target and not _is_loopback_host(written) and _fi_keys_configured(caller_headers):
                if _is_futureagi_host(resolved_target[1]):
                    raise UnsupportedFutureAGIEndpointError(
                        "The exporter resolved a Future AGI endpoint that was not set up for TLS, so the Future AGI "
                        "credentials are not sent to it. Set FI_GRPC_URL to the Future AGI gRPC target, or remove the credentials."
                    )
                _authorize_plain_text(written or resolved_target[1], resolved_target[2], allow_insecure_http, grpc=True)
        except UnsupportedFutureAGIEndpointError:
            _close_after_refusal(self, _GRPCSpanExporter)
            raise
        if resolved == "fi":
            _pin_grpc_headers(self, _with_fi_auth_headers(caller_headers, add_env_keys=True), caller_headers)

    def shutdown(self) -> None:
        """Clean up any resources before shutting down."""
        try:
            if hasattr(self, "_session") and self._session:
                self._session.close()
        except Exception as e:
            logger.error("Error during gRPC exporter shutdown: %s", e)
        finally:
            super().shutdown()


class HTTPSpanExporter(_HTTPSpanExporter):
    """
    OTLP span exporter using HTTP.

    For more information, see:
    - `opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter`

    Args:
        endpoint (str, optional): OpenTelemetry Collector receiver endpoint. If omitted or None,
            the endpoint is derived from `FI_TRACES_URL`, else `FI_BASE_URL`, defaulting to the
            Future AGI HTTP endpoint.
        headers: Headers to send when exporting. For a Future AGI endpoint, the
            `FI_API_KEY` and `FI_SECRET_KEY` environment variables are added when no
            credential header is given.
        allow_insecure_http (bool): Allow Future AGI keys over plain http to a self-hosted host other than
            loopback, like ``FI_ALLOW_INSECURE_HTTP=1``. It does not apply to a Future AGI host, or when the
            request goes through a proxy.
    """

    def __init__(self, *args: Any, allow_insecure_http: bool = False, **kwargs: Any):
        bound_args = _get_class_signature(_HTTPSpanExporter, "http").bind_partial(*args, **kwargs)
        caller_headers = _materialize_headers(bound_args.arguments.get("headers"))
        if "headers" in bound_args.arguments:
            bound_args.arguments["headers"] = caller_headers

        if bound_args.arguments.get("endpoint") is None:
            # The environment's endpoint is checked as written, before the exporter's own rewrite of its path.
            _check_future_agi_endpoint(get_env_traces_endpoint(), False, caller_headers, named=True)
            _, endpoint = _normalized_endpoint(None)
            bound_args.arguments["endpoint"] = endpoint

        # Built once, with the caller's own arguments: nothing Future AGI specific reaches the
        # session or the headers a caller supplied until the resolved endpoint is known.
        if _check_future_agi_endpoint(bound_args.arguments["endpoint"], False, caller_headers) == "fi":
            _authorize_http_endpoint(bound_args.arguments["endpoint"], allow_insecure_http, bound_args.arguments.get("session"))
        super().__init__(*bound_args.args, **bound_args.kwargs)
        try:
            resolved = _check_future_agi_endpoint(getattr(self, "_endpoint", None), False, caller_headers)
        except UnsupportedFutureAGIEndpointError:
            _close_after_refusal(self, _HTTPSpanExporter)
            raise
        if resolved == "fi":
            _pin_http_headers(
                self,
                _with_fi_auth_headers(caller_headers, add_env_keys=True),
                caller_headers,
                bound_args.arguments.get("session"),
            )

    def shutdown(self) -> None:
        """Clean up any resources before shutting down."""
        try:
            if hasattr(self, "_session") and self._session:
                self._session.close()
        except Exception as e:
            logger.error("Error during HTTP exporter shutdown: %s", e)
        finally:
            super().shutdown()


def _exporter_transport(exporter: SpanExporter) -> str:
    if isinstance(exporter, _HTTPSpanExporter):
        return "HTTP"
    if isinstance(exporter, _GRPCSpanExporter):
        return "gRPC"
    else:
        return exporter.__class__.__name__


def _printable_headers(
    headers: Union[List[Tuple[str, str]], Dict[str, str]]
) -> Dict[str, str]:
    if isinstance(headers, dict):
        return {key: "****" for key, _ in headers.items()}
    return {key: "****" for key, _ in headers}


def _construct_http_endpoint(parsed_endpoint: ParseResult) -> ParseResult:
    return parsed_endpoint._replace(path="/tracer/v1/traces")


def _normalized_endpoint(endpoint: Optional[str]) -> Tuple[ParseResult, str]:
    if endpoint is None:
        endpoint = get_env_traces_endpoint()

    parsed = urlparse(endpoint)
    parsed = _construct_http_endpoint(parsed)

    return parsed, parsed.geturl()


_EXPORTER_PACKAGES = {
    "http": "opentelemetry-exporter-otlp-proto-http",
    "grpc": "opentelemetry-exporter-otlp-proto-grpc",
}


def _installed_minor(kind: str) -> int | None:
    """The minor version of the installed exporter package (1.<minor>.x), if it can be read."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        parts = version(_EXPORTER_PACKAGES[kind]).split(".")
        return int(parts[1]) if parts[0] == "1" else None
    except (PackageNotFoundError, ValueError, IndexError):  # pragma: no cover - metadata unavailable or unusual
        return None


def _documented_init_signature(kind: str) -> inspect.Signature:
    """The ``__init__`` parameters the OTLP span exporters document, without ``self``.

    Used when the class's ``__init__`` is wrapped by a bare ``(self, *args, **kwargs)``
    function, so its own signature names none of them. The positional order is the
    one of the installed release: ``channel_options`` follows ``compression`` from 1.35
    and ``retryable_error_codes`` follows it from 1.42 (gRPC). Parameters a release
    does not have pass through ``**kwargs`` unchanged.
    """
    P = inspect.Parameter
    minor = _installed_minor(kind)
    if kind == "http":
        names = [
            "endpoint",
            "certificate_file",
            "client_key_file",
            "client_certificate_file",
            "headers",
            "timeout",
            "compression",
            "session",
        ]
    else:
        names = ["endpoint", "insecure", "credentials", "headers", "timeout", "compression"]
        if minor is None or minor >= 35:
            names.append("channel_options")
        if minor is None or minor >= 42:
            names.append("retryable_error_codes")
    params = [P(n, P.POSITIONAL_OR_KEYWORD, default=None) for n in names]
    params.append(P("kwargs", P.VAR_KEYWORD))
    return inspect.Signature(params)


def _get_class_signature(fn: Type[Any], kind: str | None = None) -> inspect.Signature:
    """The signature callers of ``fn`` bind against, following any ``__wrapped__`` chain.

    When what remains is only ``*args``/``**kwargs`` (a wrapper that does not expose
    the function it wraps), ``kind`` ("http" or "grpc") selects the exporter's
    documented parameters instead.
    """
    sig = inspect.signature(fn, follow_wrapped=True)
    named = [
        p
        for p in sig.parameters.values()
        if p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]
    if kind is not None and not named:
        return _documented_init_signature(kind)
    return sig


def check_custom_eval_config_exists(
    project_name: str,
    eval_tags: list,
    project_type: str = ProjectType.EXPERIMENT.value,
    base_url: Optional[str] = None,
    allow_insecure_http: bool = False,
) -> bool:
    """
    Check if a custom eval config exists for a given project.
    """
    if not eval_tags:
        return False

    from_env = base_url is None
    if base_url is None:
        base_url = get_env_collector_endpoint()

    url = f"{base_url}/tracer/custom-eval-config/check_exists/"
    send_keys = _guard_future_agi_request(url, allow=allow_insecure_http, base=base_url, from_env=from_env)

    try:
        headers = {
            CONTENT_TYPE: "application/json",
            **((get_env_fi_auth_header() or {}) if send_keys else {}),
        }

        response = requests.post(
            url,
            headers=headers,
            json={"project_name": project_name, "eval_tags": eval_tags, "project_type": project_type},
        )

        response.raise_for_status()
        return response.json().get("result", {}).get("exists", False)

    except Exception as e:
        print(f"Failed to check custom eval config: {e}")
        return False
