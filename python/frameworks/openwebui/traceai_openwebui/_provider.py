"""Private provider: never forward Open WebUI's HTTP, auth or database spans."""

from __future__ import annotations

import threading
from collections.abc import Callable
from urllib.parse import urlsplit

import certifi
import requests
from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
    Compression,
    OTLPSpanExporter,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ALWAYS_ON


def endpoint_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Use a collector URL without credentials, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("Use HTTPS for a non-loopback collector")
    return value.rstrip("/") + (
        ""
        if parsed.path.rstrip("/").endswith("/tracer/v1/traces")
        else "/tracer/v1/traces"
    )


class _ExportSession(requests.Session):
    def __init__(self, auth_headers: dict[str, str]) -> None:
        super().__init__()
        self._auth_headers = auth_headers

    def prepare_request(self, request):
        prepared = super().prepare_request(request)
        # Some SDK versions merge native OTEL_* headers even when headers were
        # explicitly supplied. Allow only transport headers plus our own keys.
        prepared.headers.clear()
        prepared.headers.update(
            {
                "Content-Type": "application/x-protobuf",
                "Content-Length": str(len(prepared.body or b"")),
            }
        )
        prepared.headers.update(self._auth_headers)
        return prepared

    def request(self, method, url, **kwargs):
        # The SDK's transport can pass mTLS from OTEL_* environment variables
        # on each request. Override at the public requests boundary, not by
        # temporarily mutating the process environment used by native telemetry.
        kwargs["cert"] = None
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)


def make_provider(
    endpoint: str, project: str, api_key: str, secret_key: str
) -> TracerProvider:
    session = _ExportSession({"X-Api-Key": api_key, "X-Secret-Key": secret_key})
    # Never inherit proxy credentials or .netrc authentication from the host.
    session.trust_env = False
    exporter = OTLPSpanExporter(
        endpoint=endpoint_url(endpoint),
        headers={"X-Api-Key": api_key, "X-Secret-Key": secret_key},
        timeout=10,
        compression=Compression.NoCompression,
        certificate_file=certifi.where(),
        session=session,
    )
    # An unrelated native OTLP client certificate must not be sent to our endpoint.
    session.cert = None
    provider = TracerProvider(
        resource=Resource(
            {
                "project_name": project,
                "project_type": "observe",
                "service.name": "open-webui",
            }
        ),
        sampler=ALWAYS_ON,
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
            export_timeout_millis=10000,
        )
    )
    return provider


class _Generation:
    def __init__(self, key: tuple[str, ...], provider: TracerProvider) -> None:
        self.key = key
        self.provider = provider
        self.users = 0
        self.retired = False


class Providers:
    """Keep in-flight calls on their original project when an admin edits valves."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: _Generation | None = None

    @staticmethod
    def _retire(generation: _Generation) -> None:
        threading.Thread(
            target=generation.provider.shutdown,
            name="traceai-openwebui-close",
            daemon=True,
        ).start()

    def acquire(
        self, key: tuple[str, str, str, str]
    ) -> tuple[TracerProvider, Callable[[], None]]:
        with self._lock:
            current = self._current
            if current is None or current.key != key:
                new = _Generation(key, make_provider(*key))
                if current is not None:
                    current.retired = True
                    if current.users == 0:
                        self._retire(current)
                self._current = current = new
            current.users += 1
        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                current.users -= 1
                if current.retired and current.users == 0:
                    self._retire(current)

        return current.provider, release

    def flush(self, timeout_millis: int = 10000) -> bool:
        with self._lock:
            provider = self._current.provider if self._current else None
        return provider.force_flush(timeout_millis) if provider else True

    def close(self) -> None:
        with self._lock:
            current, self._current = self._current, None
            if current is not None:
                current.retired = True
                if current.users == 0:
                    self._retire(current)
