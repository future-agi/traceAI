"""Who traces a Messages call when the Anthropic SDK has its own OpenTelemetry spans.

Anthropic SDK 1.14.0 and later give every API call a span of their own. When that
span goes to the tracer provider this instrumentor exports to, a second span from
the instrumentor would count the same call twice, so the instrumentor leaves the call
to the SDK. When the SDK's span goes elsewhere (another provider, or nowhere), the
instrumentor keeps tracing the call, so its provider never loses it.
"""

import logging
from collections.abc import Mapping
from typing import Any, Optional

from opentelemetry import trace as trace_api

logger = logging.getLogger(__name__)

_NOTICE = (
    "Anthropic SDK native OpenTelemetry tracing already sends Messages calls to the "
    "tracer provider of AnthropicInstrumentor, so traceAI leaves those calls to the SDK "
    "and emits no second span. TraceConfig and FI_HIDE_* do not apply to the SDK's spans: "
    "the client's open_telemetry option controls their content (metadata only unless "
    'traces content_mode is "content"). To keep traceAI spans instead, create the '
    "client with open_telemetry=False."
)


class _NativeTracing:
    """Decides, for each call, whether the SDK's own span reaches this instrumentor's provider."""

    def __init__(self, tracer_provider: Optional[trace_api.TracerProvider]) -> None:
        # Without an explicit provider, the instrumentor's tracer follows the global
        # provider, as the SDK's does, so both are resolved at call time.
        if isinstance(tracer_provider, trace_api.ProxyTracerProvider):
            tracer_provider = None
        self._tracer_provider = tracer_provider
        self._notified = False

    def owns(self, resource: Any) -> bool:
        """Whether the SDK traces this call of `resource` (a Messages resource) into our provider.

        Reads the client's resolved public `open_telemetry` option, never a span's
        recording state: the SDK's sampling decision must not trigger a second span.
        Older SDKs, and any option this cannot read, keep the instrumentor tracing.
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
            owns = native_provider is own_provider and not isinstance(
                native_provider, trace_api.ProxyTracerProvider
            )
        except Exception:
            logger.debug("Could not read the Anthropic client's OpenTelemetry option", exc_info=True)
            return False
        if owns and not self._notified:
            self._notified = True
            logger.warning(_NOTICE)
        return owns
