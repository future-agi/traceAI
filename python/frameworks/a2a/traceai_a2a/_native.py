"""Adapt the SDK's native spans; do not add a second client span layer.

The SDK's trace_class decorator treats async generators as synchronous calls.
An operation therefore owns the native span until exhaustion/close, and makes
it current only while SDK code runs (never while control is yielded to users).
"""

import asyncio
import copy
import functools
import inspect
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from importlib import import_module
from urllib.parse import urlsplit

from opentelemetry import context, propagate, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import INVALID_SPAN, SpanKind, StatusCode

from traceai_a2a._semantic_conventions import (
    A2A_AGENT_URL,
    A2A_MESSAGE_PARTS_COUNT,
    A2A_MESSAGE_ROLE,
    A2A_PROPAGATED_TRACE_ID,
    A2A_SPAN_KIND_CLIENT,
    A2A_STREAMING,
    A2A_TASK_ID,
    A2A_TASK_STATE,
)

_operation = ContextVar("traceai_a2a_operation", default=None)
_stream_owner = ContextVar("traceai_a2a_stream_owner", default=None)


logger = logging.getLogger(__name__)


def safe_origin(value: object) -> str | None:
    """Keep the endpoint origin, never credentials, path, query or fragment."""
    try:
        url = urlsplit(str(value))
        if url.scheme not in ("http", "https") or not url.hostname:
            return None
        host = url.hostname
        if ":" in host:
            host = f"[{host}]"
        return f"{url.scheme}://{host}" + (f":{url.port}" if url.port else "")
    except ValueError:
        return None


def _field(value, name, default=None):
    return (
        value.get(name, default)
        if isinstance(value, dict)
        else getattr(value, name, default)
    )


def _enum(value, owner, field):
    # Pydantic SDKs use enums; SDK 1.x uses protobuf integer enums.
    descriptor = getattr(owner, "DESCRIPTOR", None)
    if descriptor and isinstance(value, int):
        enum_type = descriptor.fields_by_name[field].enum_type
        member = enum_type.values_by_number.get(value)
        return member.name.lower() if member else None
    return str(getattr(value, "value", value)) if value is not None else None


def _message_attributes(request):
    request = _field(request, "params", request)
    message = _field(request, "message")
    if message is None:
        return {}
    attrs = {}
    role = _enum(_field(message, "role"), message, "role")
    if role:
        attrs[A2A_MESSAGE_ROLE] = role.removeprefix("role_")
    parts = _field(message, "parts")
    if parts is not None:
        attrs[A2A_MESSAGE_PARTS_COUNT] = len(parts)
    return attrs


class _SafeSpan:
    """Native decorators must not export exception messages or stack traces."""

    def __init__(self, span, operation=None):
        self._span = span
        self._operation = operation

    def __getattr__(self, name):
        return getattr(self._span, name)

    def record_exception(self, exception, *args, **kwargs):
        if self._operation:
            self._operation.error(exception)
        else:
            self._span.set_attribute("error.type", type(exception).__name__)

    def set_status(self, status, description=None):
        code = getattr(status, "status_code", status)
        # OK is terminal in OTel. A generator-factory OK must not prevent a
        # later iteration failure from marking the operation as ERROR.
        if self._operation and code != StatusCode.ERROR:
            return
        self._span.set_status(code)


class _NativeTracer:
    def __init__(self, tracer):
        self._tracer = tracer

    @contextmanager
    def start_as_current_span(self, name, *args, **kwargs):
        op = _operation.get()
        client_operation = op is not None and name.startswith(op.prefix)
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY) or (
            client_operation and op.suppressed
        ):
            yield INVALID_SPAN
            return
        if client_operation and op.span is not None:
            # The SDK decorates private helpers as CLIENT spans too. Retain
            # the current operation as their context without duplicate spans.
            yield INVALID_SPAN
            return
        attributes = dict(kwargs.get("attributes") or {})
        if client_operation:
            attributes.update(op.attributes)
        try:
            span = self._tracer.start_span(
                name,
                context=kwargs.get("context"),
                kind=kwargs.get("kind", SpanKind.INTERNAL),
                attributes=attributes,
            )
        except Exception:
            # Telemetry is best effort: a provider failure must not prevent
            # the SDK request. Suppress helper retries for this operation.
            logger.warning("Could not start native A2A span", exc_info=True)
            if client_operation:
                op.suppressed = True
            yield INVALID_SPAN
            return
        if client_operation:
            op.span = span
        try:
            with trace.use_span(
                span,
                end_on_exit=False,
                record_exception=False,
                set_status_on_exception=False,
            ):
                if client_operation:
                    carrier = {}
                    propagate.inject(carrier)
                    # Caller-owned mappings are never changed. Replace stale
                    # propagation headers case-insensitively in our private copy.
                    for key in list(op.headers):
                        if key.lower() in {k.lower() for k in carrier}:
                            del op.headers[key]
                    op.headers.update(carrier)
                    span.set_attribute(
                        A2A_PROPAGATED_TRACE_ID,
                        format(span.get_span_context().trace_id, "032x"),
                    )
                yield _SafeSpan(span, op if client_operation else None)
        finally:
            if not client_operation:
                span.end()


class _NativeTrace:
    """Route only the SDK's own tracers to the explicitly supplied provider."""

    def __init__(self, provider):
        self._provider = provider

    def get_tracer(self, *args, **kwargs):
        return _NativeTracer(self._provider.get_tracer(*args, **kwargs))


class _Operation:
    def __init__(self, instance, method, request, headers, streaming):
        self.prefix = f"{type(instance).__module__}.{type(instance).__name__}."
        self.headers = headers
        self.span = None
        self.ended = False
        self.failed = False
        self.cancelled = False
        self.suppressed = bool(context.get_value(_SUPPRESS_INSTRUMENTATION_KEY))
        self.attributes = {
            "gen_ai.span.kind": A2A_SPAN_KIND_CLIENT,
            "rpc.system": "a2a",
            "rpc.method": method,
            A2A_STREAMING: streaming,
            **_message_attributes(request),
        }
        origin = safe_origin(getattr(instance, "url", None))
        if origin:
            self.attributes[A2A_AGENT_URL] = origin

    @contextmanager
    def active(self):
        token = _operation.set(self)
        try:
            if self.span is None:
                yield
            else:
                with trace.use_span(
                    self.span,
                    end_on_exit=False,
                    record_exception=False,
                    set_status_on_exception=False,
                ):
                    yield
        finally:
            _operation.reset(token)

    def observe(self, result):
        if self.span is None:
            return
        # Unwrap Pydantic RootModel/JSON-RPC envelopes, then protobuf responses.
        result = _field(result, "root", result)
        if _field(result, "error") is not None:
            self.failed = True
            self.span.set_attribute("error.type", "A2AProtocolError")
            return
        result = _field(result, "result", result)
        oneof = getattr(result, "WhichOneof", None)
        if oneof:
            for group in result.DESCRIPTOR.oneofs:
                selected = oneof(group.name)
                if selected in ("task", "status_update", "artifact_update"):
                    result = getattr(result, selected)
                    break
        state = _field(result, "status")
        if state is not None:
            task_id = (
                _field(result, "id")
                or _field(result, "task_id")
                or _field(result, "taskId")  # SDK 0.2 Pydantic event field
            )
            if task_id:
                self.span.set_attribute(A2A_TASK_ID, str(task_id))
            value = _enum(_field(state, "state"), state, "state")
            if value:
                value = value.removeprefix("task_state_")
                self.span.set_attribute(A2A_TASK_STATE, value)
                if value in ("failed", "rejected"):
                    self.failed = True
        artifact = _field(result, "artifact")
        parts = _field(artifact, "parts") if artifact is not None else None
        if parts:
            part = _field(parts[0], "root", parts[0])
            if hasattr(part, "WhichOneof"):
                kind = part.WhichOneof("content")
                if kind in ("raw", "url"):
                    kind = "file"
            else:
                kind = _field(part, "kind") or _field(part, "type")
            if kind in ("text", "file", "data"):
                self.span.set_attribute("gen_ai.a2a.artifact.type", kind)

    def error(self, exc):
        if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
            self.cancelled = True
            if self.span is not None:
                self.span.set_attribute("gen_ai.a2a.cancelled", True)
        else:
            self.failed = True
            if self.span is not None:
                self.span.set_attribute("error.type", type(exc).__name__)

    def finish(self):
        if self.ended:
            return
        self.ended = True
        if self.span is not None:
            if self.failed:
                self.span.set_status(StatusCode.ERROR)
            elif not self.cancelled:
                self.span.set_status(StatusCode.OK)
            self.span.end()


class _Stream:
    def __init__(self, iterator, operation):
        self._iterator = iterator
        self._operation = operation
        owner = _stream_owner.get()
        if owner is not None:
            owner.append(self)

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.asend(None)

    async def asend(self, value):
        return await self._advance(self._iterator.asend, value)

    async def athrow(self, *args):
        return await self._advance(self._iterator.athrow, *args)

    async def _advance(self, advance, *args):
        op = self._operation
        if op.ended:
            raise StopAsyncIteration
        with op.active():
            try:
                value = await advance(*args)
                op.observe(value)
                return value
            except StopAsyncIteration:
                op.finish()
                raise
            except BaseException as exc:
                op.error(exc)
                op.finish()
                raise

    async def aclose(self):
        op = self._operation
        with op.active():
            try:
                close = getattr(self._iterator, "aclose", None)
                if close:
                    await close()
            except BaseException as exc:
                op.error(exc)
                raise
            finally:
                op.finish()

    def __del__(self):
        # Best effort only; callers abandoning a stream should use aclosing().
        self._operation.finish()


def _wrap(cls, name, original):
    signature = inspect.signature(original)
    streaming = inspect.isasyncgenfunction(inspect.unwrap(original))

    def prepare(instance, args, kwargs):
        bound = signature.bind(instance, *args, **kwargs)
        if "context" in signature.parameters:
            from a2a.client import ClientCallContext

            source = bound.arguments.get("context")
            call_context = (
                copy.copy(source) if source is not None else ClientCallContext()
            )
            if "service_parameters" in type(call_context).model_fields:
                call_context.service_parameters = dict(
                    getattr(source, "service_parameters", None) or {}
                )
                headers = call_context.service_parameters
            else:
                # SDK 0.3 transports pass HTTP options through context.state.
                call_context.state = dict(call_context.state)
                http_kwargs = dict(call_context.state.get("http_kwargs") or {})
                headers = dict(http_kwargs.get("headers") or {})
                http_kwargs["headers"] = headers
                call_context.state["http_kwargs"] = http_kwargs
            bound.arguments["context"] = call_context
        else:
            http_kwargs = dict(bound.arguments.get("http_kwargs") or {})
            headers = dict(http_kwargs.get("headers") or {})
            http_kwargs["headers"] = headers
            bound.arguments["http_kwargs"] = http_kwargs
        op = _Operation(
            instance, name, bound.arguments.get("request"), headers, streaming
        )
        # Native span names use the declaring class, not a user subclass.
        op.prefix = f"{cls.__module__}.{cls.__name__}."
        return op, bound

    @functools.wraps(original)
    async def call(instance, *args, **kwargs):
        op, bound = prepare(instance, args, kwargs)
        with op.active():
            try:
                result = await original(*bound.args, **bound.kwargs)
                op.observe(result)
                return result
            except BaseException as exc:
                op.error(exc)
                raise
            finally:
                op.finish()

    @functools.wraps(original)
    def stream(instance, *args, **kwargs):
        op, bound = prepare(instance, args, kwargs)
        with op.active():
            try:
                return _Stream(original(*bound.args, **bound.kwargs), op)
            except BaseException as exc:
                op.error(exc)
                op.finish()
                raise

    return stream if streaming else call


class _OwnedStream:
    """Close transport spans when a factory client's generator stops early."""

    def __init__(self, iterator):
        self._iterator = iterator
        self._children = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.asend(None)

    async def asend(self, value):
        return await self._advance(self._iterator.asend, value)

    async def athrow(self, *args):
        return await self._advance(self._iterator.athrow, *args)

    async def _advance(self, advance, *args):
        token = _stream_owner.set(self._children)
        try:
            return await advance(*args)
        except BaseException:
            await self.aclose()
            raise
        finally:
            _stream_owner.reset(token)

    async def aclose(self):
        try:
            await self._iterator.aclose()
        finally:
            children, self._children = self._children, []
            for child in reversed(children):
                await child.aclose()


def _own_stream(original):
    @functools.wraps(original)
    def stream(*args, **kwargs):
        return _OwnedStream(original(*args, **kwargs))

    return stream


def _grpc_metadata(original):
    @functools.wraps(original)
    def metadata(*args, **kwargs):
        result = original(*args, **kwargs)
        op = _operation.get()
        if op is None or op.suppressed or not op.headers:
            return result
        values = dict(result or [])
        propagate.inject(values)
        return list(values.items())

    return metadata


class NativeAdapter:
    def __init__(self, provider: trace.TracerProvider) -> None:
        self._provider = provider
        self._patches = []

    def install(self) -> None:
        from a2a.utils import telemetry

        if not getattr(telemetry, "otel_enabled", True):
            raise RuntimeError(
                "A2A native tracing is disabled by OTEL_INSTRUMENTATION_A2A_SDK_ENABLED"
            )
        candidates = (
            ("a2a.client", "A2AClient"),
            ("a2a.client.transports.jsonrpc", "JsonRpcTransport"),
            ("a2a.client.transports.rest", "RestTransport"),
            ("a2a.client.transports.grpc", "GrpcTransport"),
            ("a2a.compat.v0_3.jsonrpc_transport", "CompatJsonRpcTransport"),
            ("a2a.compat.v0_3.rest_transport", "CompatRestTransport"),
            ("a2a.compat.v0_3.grpc_transport", "CompatGrpcTransport"),
        )
        for module, name in candidates:
            try:
                cls = getattr(import_module(module), name, None)
            except ImportError:
                continue  # Optional transport or an older SDK layout.
            if cls is None:
                continue
            for method, original in vars(cls).items():
                if method.startswith("_") or not inspect.isfunction(original):
                    continue
                # A 0.3 legacy facade delegates to transports and is not
                # natively decorated. Do not add another instrumentation layer.
                if (
                    original.__code__.co_filename
                    != telemetry.trace_function.__code__.co_filename
                ):
                    continue
                parameters = inspect.signature(original).parameters
                if "request" not in parameters or not (
                    {"context", "http_kwargs"} & parameters.keys()
                ):
                    continue
                self._patches.append(
                    (cls, method, original, _wrap(cls, method, original))
                )
            if name == "GrpcTransport" and hasattr(cls, "_get_grpc_metadata"):
                original = cls._get_grpc_metadata
                self._patches.append(
                    (cls, "_get_grpc_metadata", original, _grpc_metadata(original))
                )
        if not any(name == "send_message" for _, name, _, _ in self._patches):
            raise RuntimeError(
                "No supported native A2A client operations found; instrumentation was not installed"
            )
        # The public factory client has extra async-generator layers which
        # otherwise don't forward aclose() to the transport stream.
        try:
            from a2a.client import BaseClient
        except ImportError:
            BaseClient = None
        if BaseClient is not None:
            for name, original in vars(BaseClient).items():
                if not name.startswith("_") and inspect.isasyncgenfunction(original):
                    self._patches.append(
                        (BaseClient, name, original, _own_stream(original))
                    )
        self._telemetry = telemetry
        self._original_trace = telemetry.trace
        self._trace = _NativeTrace(self._provider)
        telemetry.trace = self._trace
        for cls, name, _, replacement in self._patches:
            setattr(cls, name, replacement)

    def uninstall(self) -> None:
        for cls, name, original, replacement in reversed(self._patches):
            if getattr(cls, name) is replacement:
                setattr(cls, name, original)
        self._patches.clear()
        if self._telemetry.trace is self._trace:
            self._telemetry.trace = self._original_trace
