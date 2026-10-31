"""The S1 boundary on every traceAI-owned export route, with or without the auto agent.

One sanitizer implementation (``auto.wire._privacy.export_span``) runs immediately before an
owned exporter serializes a batch, on detached snapshots. Routes:

- ``PrivacyExporter`` wraps an exporter handed to a traceAI processor (``register()``,
  ``fi_instrumentation.BatchSpanProcessor`` / ``SimpleSpanProcessor``) and the auto agent's routes.
- ``HTTPSpanExporter`` / ``GRPCSpanExporter`` sanitize their own batches when used directly
  (for example under an upstream OpenTelemetry processor).

A batch already sanitized by an outer ``PrivacyExporter`` is not sanitized twice: the outer
boundary marks the synchronous inner ``export`` call through a context variable, never by a
marker on span objects (which a later ``copy`` could carry onto changed data).
"""

import contextvars
from typing import Any, Mapping, Optional, Sequence

import wrapt

_SANITIZED: contextvars.ContextVar = contextvars.ContextVar("traceai_batch_sanitized", default=False)


def policy(config: Any = None) -> Any:
    """The explicit policy, else the process policy (pinned by the agent, else env > file)."""
    if config is not None:
        return config
    from fi_instrumentation.instrumentation.config import TraceConfig

    return TraceConfig()


def observe_exporter_credentials(exporter: Any) -> None:
    """Learn an exporter's own authentication header values as known secrets (S5)."""
    try:
        from fi_instrumentation._secrets import seed_environment
        from fi_instrumentation.auto.wire._privacy import observe_request

        seed_environment()
        # OTel 1.x stores headers on the exporter; newer releases use _client.
        for owner in (exporter, getattr(exporter, "_client", None)):
            headers = getattr(owner, "_headers", None)
            if isinstance(headers, Mapping):
                observe_request(headers)
            elif type(headers) in (tuple, list):  # gRPC metadata
                observe_request(dict(headers))
    except Exception:
        pass  # Optional exporter inspection must not change initialization.


def sanitize_batch(spans: Sequence[Any], config: Any = None) -> Sequence[Any]:
    """Detached, sanitized snapshots of ``spans`` unless an outer boundary already did it."""
    if _SANITIZED.get():
        return spans
    from fi_instrumentation.auto.wire._privacy import export_span

    effective = policy(config)
    return [export_span(span, effective) for span in spans]


class PrivacyExporter(wrapt.ObjectProxy):  # type: ignore[misc]
    """The single sanitizer immediately before the wrapped exporter serializes each batch.

    ``config=None`` follows the process policy at export time, so a route created before
    the agent pinned its configuration still applies it.
    """

    def __init__(self, exporter: Any, config: Any = None) -> None:
        super().__init__(exporter)
        self._self_config = config
        # Supplied OTLP exporters may already hold explicit authentication headers.
        # Learn them before the first batch is sanitized, not during its HTTP send.
        observe_exporter_credentials(exporter)

    def export(self, spans: Any) -> Any:
        clean = sanitize_batch(list(spans), self._self_config)
        token = _SANITIZED.set(True)
        try:
            return self.__wrapped__.export(clean)
        finally:
            _SANITIZED.reset(token)


def protect_exporter(exporter: Any, config: Any = None) -> Any:
    """Put ``exporter`` behind the boundary once; self-sanitizing owned exporters stay as they are."""
    if exporter is None or isinstance(exporter, PrivacyExporter) or getattr(exporter, "_traceai_sanitizes", False):
        return exporter
    return PrivacyExporter(exporter, config)

