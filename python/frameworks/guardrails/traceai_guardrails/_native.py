"""Semantic and privacy adaptation at the native telemetry boundary.

Only Guardrails' module-local tracer references are bound. No global OTel API,
Guard/Runner execution method, contextvars module or validator is patched.
"""

from contextlib import contextmanager
from typing import Any, Mapping

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import trace
from wrapt import ObjectProxy

# Never export native argument/history dumps, validator metadata, invocation
# parameters or baggage. They mix directions and can include credentials.
_STRUCTURAL = frozenset(
    {
        "guardrails.version",
        "type",
        "guard.name",
        "validation_passed",
        "execution_id",
        "token_consumption",
        "number_of_reasks",
        "number_of_llm_calls",
        "step.index",
        "validator.name",
        "validator.on_fail",
        "validator.instance_id",
        "validation_session_id",
        "on_fail_descriptor",
        "instance_id",
        "exception.type",
        "exception.escaped",
        "exception.message",
        "exception.stacktrace",
        "error.type",
        "error.message",
    }
)
_ALIASES = {
    "openinference.span.kind": SpanAttributes.GEN_AI_SPAN_KIND,
    "llm.model_name": SpanAttributes.GEN_AI_REQUEST_MODEL,
    "llm.token_count.prompt": SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS,
    "llm.token_count.completion": SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS,
    "llm.token_count.total": SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS,
}
_CANONICAL = frozenset(_ALIASES.values())


def _attribute(name: str, key: str, value: Any):
    if key in _ALIASES:
        return _ALIASES[key], value
    if key in _STRUCTURAL or key in _CANONICAL:
        return key, value
    for old, new in (
        ("llm.input_messages.", "gen_ai.input.messages."),
        ("llm.output_messages.", "gen_ai.output.messages."),
    ):
        if key.startswith(old):
            return new + key[len(old) :], value
    if key.startswith(("gen_ai.input.messages.", "gen_ai.output.messages.")):
        return key, value
    if key == "input" or key == "validator.validate.input.value":
        return SpanAttributes.INPUT_VALUE, value
    if key == "validator.validate.output.outcome":
        return SpanAttributes.OUTPUT_VALUE, value
    if key in ("input.value", "output.value", "input.mime_type", "output.mime_type"):
        # The validator's generic input/output fields contain metadata and
        # diagnostics; the explicit value/outcome aliases above are safe.
        if name in ("guard", "stream_guard_span", "async_stream_span"):
            return key, value
        # Only materialized call output is safe. Generic call inputs include
        # arbitrary invocation kwargs; steps contain mixed Call/Iteration state.
        if name == "call" and key.startswith("output."):
            return key, value
    return None


class NativeSpan(ObjectProxy):
    def __init__(self, span, name):
        super().__init__(span)
        self._self_name = name

    def set_attribute(self, key, value):
        mapped = _attribute(self._self_name, key, value)
        if mapped is not None:
            self.__wrapped__.set_attribute(*mapped)

    def set_attributes(self, attributes):
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def add_event(self, name, attributes=None, timestamp=None):
        # Native events are not needed for content; exceptions are governed by
        # FI's error-detail policy. Do not export arbitrary event names/payloads.
        if name == "exception":
            self.__wrapped__.add_event(
                name, _attributes(self._self_name, attributes), timestamp
            )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.__wrapped__.__exit__(*args)


def _attributes(name: str, attributes: Mapping[str, Any] = None):
    return dict(
        mapped
        for key, value in (attributes or {}).items()
        if (mapped := _attribute(name, key, value)) is not None
    )


class NativeTracer(trace.Tracer):
    def __init__(self, tracer, config: TraceConfig):
        # register() already returns an FITracer. Use this adapter's explicit
        # policy once, rather than nesting a provider-default policy over it.
        if isinstance(tracer, FITracer):
            tracer = tracer.__wrapped__
        self._tracer = FITracer(tracer, config=config)

    def start_span(
        self,
        name,
        context=None,
        kind=trace.SpanKind.INTERNAL,
        attributes=None,
        links=(),
        start_time=None,
        record_exception=True,
        set_status_on_exception=True,
    ):
        attrs = _attributes(name, attributes)
        attrs.setdefault(SpanAttributes.GEN_AI_SPAN_KIND, "GUARDRAIL")
        safe_links = (
            None
            if links is None
            else [
                trace.Link(link.context, _attributes(name, link.attributes))
                for link in links
            ]
        )
        return NativeSpan(
            self._tracer.start_span(
                name,
                context=context,
                kind=kind,
                attributes=attrs,
                links=safe_links,
                start_time=start_time,
                record_exception=record_exception,
                set_status_on_exception=set_status_on_exception,
            ),
            name,
        )

    @contextmanager
    def start_as_current_span(
        self,
        name,
        context=None,
        kind=trace.SpanKind.INTERNAL,
        attributes=None,
        links=(),
        start_time=None,
        record_exception=True,
        set_status_on_exception=True,
        end_on_exit=True,
    ):
        span = self.start_span(
            name,
            context,
            kind,
            attributes,
            links,
            start_time,
            record_exception,
            set_status_on_exception,
        )
        with trace.use_span(
            span,
            end_on_exit=end_on_exit,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        ):
            yield span


class NativeTraceAPI(ObjectProxy):
    def __init__(self, provider, config):
        super().__init__(trace)
        self._self_provider = provider
        self._self_config = config

    def get_tracer(
        self,
        instrumenting_module_name,
        instrumenting_library_version=None,
        tracer_provider=None,
        schema_url=None,
        attributes=None,
    ):
        kwargs = {"schema_url": schema_url}
        if attributes is not None:
            kwargs["attributes"] = attributes
        tracer = self._self_provider.get_tracer(
            instrumenting_module_name,
            instrumenting_library_version,
            **kwargs,
        )
        return NativeTracer(tracer, self._self_config)


def add_step_attributes(step_span, response, *args, **kwargs):
    """Keep step structure; never serialize the mutable Call/Iteration history."""
    step_span.set_attribute("type", "guardrails/guard/step")
    index = args[1] if len(args) > 1 else kwargs.get("index", 0)
    if isinstance(index, int):
        step_span.set_attribute("step.index", index)


def add_call_attributes(call_span, response, *args, **kwargs):
    """Project materialized LLM output without serializing or consuming streams."""
    call_span.set_attribute("type", "guardrails/guard/step/call")
    if response is None:
        return
    if isinstance(response.output, str):
        call_span.set_attribute(SpanAttributes.OUTPUT_VALUE, response.output)
    for field, key in (
        ("prompt_token_count", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
        ("response_token_count", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
    ):
        value = getattr(response, field, None)
        if isinstance(value, int):
            call_span.set_attribute(key, value)
