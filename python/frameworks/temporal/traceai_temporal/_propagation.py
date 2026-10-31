"""Bounded W3C trace context only: never propagate baggage or application content."""

from typing import Mapping

from opentelemetry import context, trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from temporalio.api.common.v1 import Payload
from temporalio.converter import PayloadConverter

HEADER = "_traceai-temporal"
PROPAGATOR = TraceContextTextMapPropagator()
# Force OTel's lazy context initialization outside workflow execution.
context.get_current()


def decode(headers: Mapping[str, Payload]) -> dict:
    try:
        payload = headers.get(HEADER)
        if payload is None or len(payload.data) > 4096:
            return {}
        if payload.metadata.get("encoding") != b"json/plain":
            return {}
        carrier = PayloadConverter.default.from_payloads([payload])[0]
        if type(carrier) is not dict:
            return {}
        if carrier.get("disabled") is True:
            return {"disabled": True}
        # Tracestate is deliberately omitted: it may contain vendor secrets.
        value = carrier.get("traceparent")
        return {"traceparent": value} if type(value) is str and len(value) <= 512 else {}
    except Exception:
        return {}


def extract(headers: Mapping[str, Payload]) -> context.Context:
    carrier = decode(headers)
    if carrier.get("disabled"):
        return context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True, context.Context())
    try:
        return PROPAGATOR.extract(carrier, context=context.Context())
    except Exception:
        return context.Context()


def inject(headers: Mapping[str, Payload], ctx: context.Context) -> Mapping[str, Payload]:
    try:
        carrier = {}
        if context.get_value(_SUPPRESS_INSTRUMENTATION_KEY, ctx):
            carrier["disabled"] = True
        elif trace.get_current_span(ctx).get_span_context().is_valid:
            PROPAGATOR.inject(carrier, context=ctx)
            carrier.pop("tracestate", None)
        if not carrier:
            return headers
        payload = PayloadConverter.default.to_payloads([carrier])[0]
        return {**headers, HEADER: payload}
    except Exception:
        return headers
