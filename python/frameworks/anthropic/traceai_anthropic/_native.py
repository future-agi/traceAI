"""Who traces a Messages call when the Anthropic SDK has its own OpenTelemetry spans.

Anthropic SDK 1.14.0 and later give every API call a span of their own. When that
span would go to the tracer provider this instrumentor exports to, the call would be
recorded twice. The instrumentor's span is the one that carries what Future AGI reads:
the context of `using_session` / `using_attributes` (session, user, metadata, tags,
prompt template), the span kind, input and output values, and the `TraceConfig` /
`FI_HIDE_*` controls. So the instrumentor keeps its span and runs that one call on a
copy of the client whose native tracing is off (`with_options(open_telemetry=False)`,
the SDK's public per-request option). The application's client is never changed.

When the SDK's span goes elsewhere (another provider, or nowhere), both are left as
they are, so each provider gets the call once.
"""

import logging
from collections.abc import Mapping
from typing import Any, Callable, Optional

from opentelemetry import trace as trace_api

logger = logging.getLogger(__name__)

_NOTICE = (
    "Anthropic SDK native OpenTelemetry tracing sends Messages calls to the tracer "
    "provider of AnthropicInstrumentor. traceAI records those calls itself and turns "
    "the SDK's span off for each of them, so every call is recorded once. To skip this "
    "per-call step, create the client with open_telemetry=False."
)

_COPY_FAILED = (
    "Could not turn off Anthropic SDK native tracing for a Messages call, so the call "
    "may be recorded twice in the same tracer provider. Create the client with "
    "open_telemetry=False to keep only traceAI spans."
)


class _NativeTracing:
    """Keeps each Messages call to one span in this instrumentor's provider."""

    def __init__(self, tracer_provider: Optional[trace_api.TracerProvider]) -> None:
        # Without an explicit provider, the instrumentor's tracer follows the global
        # provider, as the SDK's does, so both are resolved at call time.
        if isinstance(tracer_provider, trace_api.ProxyTracerProvider):
            tracer_provider = None
        self._tracer_provider = tracer_provider
        self._notified = False
        self._copy_failure_notified = False

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

    def without_native_span(self, wrapped: Callable[..., Any], instance: Any) -> Callable[..., Any]:
        """The original method to call for this call, with the SDK's own span off when it would duplicate ours.

        `wrapped` is the original method bound to `instance`. When the SDK's span would
        reach our provider, the same method is bound to a resource of a client copy
        with native tracing disabled. If that copy can't be made, `wrapped` is returned
        unchanged: a second span is better than a call without the instrumentor's span.
        """
        if not self.shares_provider(instance):
            return wrapped
        try:
            function = getattr(wrapped, "__func__", None)
            if function is None or getattr(wrapped, "__self__", None) is not instance:
                raise TypeError("the original method is not bound to the resource")
            client = instance._client.with_options(open_telemetry=False)
            options = client.open_telemetry
            if not isinstance(options, Mapping) or options["traces"]["enabled"]:
                raise ValueError("the client copy still has native tracing")
            resource = type(instance)(client)
        except Exception:
            logger.debug("Could not disable Anthropic SDK native tracing for one call", exc_info=True)
            if not self._copy_failure_notified:
                self._copy_failure_notified = True
                logger.warning(_COPY_FAILED)
            return wrapped
        if not self._notified:
            self._notified = True
            logger.info(_NOTICE)
        return function.__get__(resource, type(resource))
