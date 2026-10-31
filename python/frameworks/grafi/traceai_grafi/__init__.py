"""Configure Graphite's native tracing without global providers or SDK patches."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Self
from urllib.parse import urlsplit

from grafi.runtime import ExecutionServices, GrafiRuntime
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    Compression,
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NoOpTracer

from ._exporter import GrafiExporter

__version__ = "0.1.0"  # x-release-please-version
__all__ = ["GrafiExporter", "Handle", "configure", "create_tracer_provider"]
_TRUE = {"1", "true", "yes", "on"}


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE


def create_tracer_provider(
    project_name: str | None = None,
    *,
    capture_content: bool | None = None,
    capture_errors: bool = False,
    base_url: str | None = None,
    api_key: str | None = None,
    secret_key: str | None = None,
) -> TracerProvider:
    """Create an owned provider for native ``ExecutionServices(tracer=...)``.

    No global provider is registered. The caller must flush/shutdown this provider.
    FI_CAPTURE_CONTENT enables input/output only; capture_errors is a separate
    explicit opt-in, with FI_HIDE_ERROR_DETAILS=true taking precedence.
    """
    project_name = project_name or os.getenv("FI_PROJECT_NAME") or "graphite-app"
    capture_content = (
        _enabled("FI_CAPTURE_CONTENT") if capture_content is None else capture_content
    )
    capture_errors = capture_errors and not _enabled("FI_HIDE_ERROR_DETAILS")
    base_url = (
        base_url or os.getenv("FI_BASE_URL") or "https://api.futureagi.com"
    ).rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "base_url must be an HTTP(S) origin without credentials, path, query or fragment"
        )
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Use HTTPS for non-loopback collectors")
    api_key = api_key or os.getenv("FI_API_KEY")
    secret_key = secret_key or os.getenv("FI_SECRET_KEY")
    if not api_key or not secret_key:
        raise ValueError("Set both FI_API_KEY and FI_SECRET_KEY")
    exporter = OTLPSpanExporter(
        endpoint=base_url + "/tracer/v1/traces",
        headers={"X-Api-Key": api_key, "X-Secret-Key": secret_key},
        compression=Compression.NoCompression,
        timeout=10,
    )
    # Do not run environment/host/process resource detection.
    provider = TracerProvider(
        resource=Resource(
            {
                "project_name": project_name,
                "project_type": "observe",
                "service.name": "graphite",
            }
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            GrafiExporter(
                exporter,
                capture_content=capture_content,
                capture_errors=capture_errors,
            )
        )
    )
    return provider


@dataclass(frozen=True)
class Handle:
    """Owns the provider; the runtime retains Graphite's normal invocation API."""

    provider: TracerProvider
    runtime: GrafiRuntime

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.provider.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self.provider.shutdown()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.shutdown()


def configure(
    project_name: str | None = None,
    *,
    services: ExecutionServices | None = None,
    capture_content: bool | None = None,
    capture_errors: bool = False,
    base_url: str | None = None,
    api_key: str | None = None,
    secret_key: str | None = None,
) -> Handle:
    """Bind one private provider to a fresh runtime, preserving other services.

    Already-traced services are rejected instead of silently replacing a tracer.
    To compose with a pre-existing provider, explicitly use GrafiExporter on its
    destination and the native ExecutionServices/GrafiRuntime APIs instead.
    """
    services = services if services is not None else ExecutionServices()
    if not isinstance(services.tracer, NoOpTracer):
        raise ValueError("services already has a tracer; configure would replace it")  # noqa: TRY004 - state conflict, not an invalid tracer type
    provider = create_tracer_provider(
        project_name,
        capture_content=capture_content,
        capture_errors=capture_errors,
        base_url=base_url,
        api_key=api_key,
        secret_key=secret_key,
    )
    runtime = GrafiRuntime(replace(services, tracer=provider.get_tracer("grafi")))
    return Handle(provider, runtime)
