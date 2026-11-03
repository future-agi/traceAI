"""Who traces a Messages call when the Anthropic SDK has its own OpenTelemetry spans.

Anthropic SDK 1.14.0 and later give every API call a span of their own. When that
span would go to the tracer provider this instrumentor exports to, the call would be
recorded twice. The instrumentor's span is the one that carries what Future AGI reads:
the context of `using_session` / `using_attributes` (session, user, metadata, tags,
prompt template), the span kind, input and output values, and the `TraceConfig` /
`FI_HIDE_*` controls. So the instrumentor keeps its span and the SDK skips its own for
that one call.

The call itself is not changed: the original method runs on the application's client,
through any other wrappers on it, so client state such as lazily loaded cloud
credentials stays on that client. While it runs, the SDK's span start
(`start_api_call_span`, which may return `None` for "no span") returns `None` for the
tracer of this client. OpenTelemetry gives clients with the same tracer provider the same
tracer, so an SDK call made inside the Messages call to that same provider (by another
wrapper, say) has no span either; calls whose SDK spans go to another provider keep them.

When the SDK's span goes elsewhere (another provider, or nowhere), both are left as
they are, so each provider gets the call once.
"""

import importlib
import logging
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any, Callable, Optional

from opentelemetry import trace as trace_api
from wrapt import FunctionWrapper

logger = logging.getLogger(__name__)

_NOTICE = (
    "Anthropic SDK native OpenTelemetry tracing sends Messages calls to the tracer "
    "provider of AnthropicInstrumentor. traceAI records those calls itself and turns "
    "the SDK's span off for each of them, so every call is recorded once. To skip this "
    "per-call step, create the client with open_telemetry=False."
)

_NO_HOOK = (
    "Could not turn off Anthropic SDK native tracing for Messages calls with this "
    "anthropic version, so they may be recorded twice in the same tracer provider. "
    "Create the client with open_telemetry=False to keep only traceAI spans."
)

# Where the SDK starts the span of each API call (anthropic>=1.14), sync and async.
_SPAN_START_MODULE = "anthropic._base_client"
_SPAN_START_NAME = "start_api_call_span"

# The client whose SDK span the running Messages call turns off, if any.
_owned_client: ContextVar[Any] = ContextVar("traceai_anthropic_owned_client", default=None)


def _skip_owned_span(wrapped: Callable[..., Any], instance: Any, args: Any, kwargs: Any) -> Any:
    """`start_api_call_span`, returning `None` (no span) for calls with the owned client's tracer."""
    client = _owned_client.get()
    if client is not None:
        try:
            tracer = args[0] if args else kwargs.get("tracer")
            if tracer is not None and tracer is client._tracer:
                return None
        except Exception:
            logger.debug("Could not match the Anthropic SDK span to the traced call", exc_info=True)
    return wrapped(*args, **kwargs)


def _span_start_module() -> Optional[Any]:
    try:
        module = importlib.import_module(_SPAN_START_MODULE)
    except Exception:
        return None
    return module if callable(vars(module).get(_SPAN_START_NAME)) else None


class _NativeTracing:
    """Keeps each Messages call to one span in this instrumentor's provider."""

    def __init__(self, tracer_provider: Optional[trace_api.TracerProvider]) -> None:
        # Without an explicit provider, the instrumentor's tracer follows the global
        # provider, as the SDK's does, so both are resolved at call time.
        if isinstance(tracer_provider, trace_api.ProxyTracerProvider):
            tracer_provider = None
        self._tracer_provider = tracer_provider
        self._hook: Optional[FunctionWrapper] = None
        self._original: Any = None
        self._notified = False
        self._no_hook_notified = False

    def install(self) -> None:
        """Hooks the SDK's span start. SDKs without one (before 1.14) are left alone."""
        module = _span_start_module()
        if module is None or self._hook is not None:
            return
        self._original = vars(module)[_SPAN_START_NAME]
        self._hook = FunctionWrapper(self._original, _skip_owned_span)
        setattr(module, _SPAN_START_NAME, self._hook)

    def uninstall(self) -> None:
        if self._hook is None:
            return
        module = _span_start_module()
        if module is not None and vars(module).get(_SPAN_START_NAME) is self._hook:
            setattr(module, _SPAN_START_NAME, self._original)
        self._hook = None
        self._original = None

    def shares_provider(self, resource: Any) -> bool:
        """Whether the SDK traces this call of `resource` (a Messages resource) into our provider.

        Reads the client's resolved public `open_telemetry` option. Older SDKs, and any
        option this cannot read, count as not shared: the call is left as it is.
        """
        try:
            options = getattr(getattr(resource, "_client", None), "open_telemetry", None)
            if not isinstance(options, Mapping):
                return False
            traces = options.get("traces")
            if not isinstance(traces, Mapping) or not traces.get("enabled", False):
                return False
            native_provider = options.get("tracer_provider")
            if native_provider is None:
                native_provider = trace_api.get_tracer_provider()
            own_provider = self._tracer_provider
            if own_provider is None:
                own_provider = trace_api.get_tracer_provider()
            return native_provider is own_provider and not isinstance(
                native_provider, trace_api.ProxyTracerProvider
            )
        except Exception:
            logger.debug("Could not read the Anthropic client's OpenTelemetry option", exc_info=True)
            return False

    def _client_to_own(self, instance: Any) -> Any:
        """The client whose SDK span this call turns off, or `None` to leave the call as it is."""
        if not self.shares_provider(instance):
            return None
        if self._hook is None:
            # A second span is better than a call without the instrumentor's span.
            if not self._no_hook_notified:
                self._no_hook_notified = True
                logger.warning(_NO_HOOK)
            return None
        if not self._notified:
            self._notified = True
            logger.info(_NOTICE)
        return instance._client

    def without_native_span(self, wrapped: Callable[..., Any], instance: Any) -> Callable[..., Any]:
        """`wrapped` (the original method bound to `instance`), with the SDK's span off when it would duplicate ours."""
        client = self._client_to_own(instance)
        if client is None:
            return wrapped

        def call(*args: Any, **kwargs: Any) -> Any:
            token = _owned_client.set(client)
            try:
                return wrapped(*args, **kwargs)
            finally:
                _owned_client.reset(token)

        return call

    def without_native_span_async(self, wrapped: Callable[..., Any], instance: Any) -> Callable[..., Any]:
        """`without_native_span` for coroutine methods: the SDK starts its span while awaited."""
        client = self._client_to_own(instance)
        if client is None:
            return wrapped

        async def call(*args: Any, **kwargs: Any) -> Any:
            token = _owned_client.set(client)
            try:
                return await wrapped(*args, **kwargs)
            finally:
                _owned_client.reset(token)

        return call
