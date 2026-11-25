from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.config import (
    REDACTED_VALUE,
    TraceConfig,
    suppress_tracing,
)
from fi_instrumentation.instrumentation.context_attributes import (
    get_attributes_from_context,
    using_attributes,
    using_metadata,
    using_prompt_template,
    using_session,
    using_tags,
    using_user,
    using_simulator_attributes,
)
from fi_instrumentation.instrumentation.helpers import safe_json_dumps
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from .otel import (
    PROJECT_NAME,
    PROJECT_TYPE,
    PROJECT_VERSION_NAME,
    SEMANTIC_CONVENTION,
    BatchSpanProcessor,
    HTTPSpanExporter,
    Sampler,
    SemanticConvention,
    SimpleSpanProcessor,
    SpanLimits,
    TracerProvider,
    Transport,
    register,
)

# fi_instrumentation.init and fi_instrumentation.auto_instrument are not listed: they load on first
# use (see __getattr__ below), and a star import must not load the zero-code agent.
__all__ = [
    # Context and attributes
    "get_attributes_from_context",
    "using_attributes",
    "using_metadata",
    "using_prompt_template",
    "using_session",
    "using_tags",
    "using_user",
    "using_simulator_attributes",
    # Helpers and config
    "safe_json_dumps",
    "suppress_tracing",
    "TraceConfig",
    "FITracer",
    "REDACTED_VALUE",
    # OpenTelemetry components
    "TracerProvider",
    "SimpleSpanProcessor",
    "BatchSpanProcessor",
    "HTTPSpanExporter",
    "Sampler",
    "ParentBased",
    "TraceIdRatioBased",
    "SpanLimits",
    "Resource",
    "PROJECT_NAME",
    "PROJECT_TYPE",
    "PROJECT_VERSION_NAME",
    "SEMANTIC_CONVENTION",
    "Transport",
    "SemanticConvention",
    "register",
]


def __getattr__(name: str):
    # The zero-code entry points load on first use, so `import fi_instrumentation` stays as light as before.
    if name in ("init", "auto_instrument"):
        from fi_instrumentation import auto

        return getattr(auto, name)
    raise AttributeError(f"module 'fi_instrumentation' has no attribute {name!r}")
