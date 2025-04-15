from opentelemetry.sdk.resources import Resource

from .fi_instrumentation.otel import (
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
