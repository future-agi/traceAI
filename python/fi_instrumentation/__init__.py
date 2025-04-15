from fi_instrumentation.instrumentation.config import (
    REDACTED_VALUE,
    TraceConfig,
    suppress_tracing,
)
from fi_instrumentation.instrumentation._tracers import FITracer
from fi_instrumentation.instrumentation.context_attributes import (
    get_attributes_from_context,
    using_attributes,
    using_metadata,
    using_prompt_template,
    using_session,
    using_tags,
    using_user,
)
from fi_instrumentation.instrumentation.helpers import safe_json_dumps
from opentelemetry.sdk.resources import Resource

from .otel import (
    PROJECT_NAME,
    PROJECT_TYPE,
    PROJECT_VERSION_NAME,
    BatchSpanProcessor,
    HTTPSpanExporter,
    SimpleSpanProcessor,
    TracerProvider,
    register,
)

__all__ = [
    # Context and attributes
    "get_attributes_from_context",
    "using_attributes",
    "using_metadata",
    "using_prompt_template",
    "using_session",
    "using_tags",
    "using_user",
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
    "Resource",
    "PROJECT_NAME",
    "PROJECT_TYPE",
    "PROJECT_VERSION_NAME",
    "register",
]
