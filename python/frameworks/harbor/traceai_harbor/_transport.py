"""OTLP protobuf transport; no global telemetry setup or MLflow APIs."""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)

ENDPOINT = "https://api.futureagi.com/tracer/v1/traces"


class DeliveryError(RuntimeError):
    """A sanitized delivery category. Response bodies are never included."""


@dataclass(frozen=True)
class Settings:
    endpoint: str = ENDPOINT
    project_name: str = "harbor"
    capture_content: bool = False
    api_key: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)

    @classmethod
    def from_env(
        cls, *, project_name: str | None = None, capture_content: bool = False
    ) -> Settings:
        return cls(
            endpoint=os.environ.get("FI_HARBOR_ENDPOINT", ENDPOINT),
            project_name=project_name or os.environ.get("FI_PROJECT_NAME", "harbor"),
            capture_content=capture_content,
            api_key=os.environ.get("FI_API_KEY", ""),
            secret_key=os.environ.get("FI_SECRET_KEY", ""),
        )

    def __post_init__(self) -> None:
        url = urlsplit(self.endpoint)
        if (
            (
                url.scheme != "https"
                and not (
                    url.scheme == "http"
                    and url.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Use an HTTPS trace endpoint (HTTP allowed only on loopback), without credentials, query or fragment"
            )
        if (
            not self.api_key
            or not self.secret_key
            or any(c in self.api_key + self.secret_key for c in "\r\n")
        ):
            raise ValueError("Set FI_API_KEY and FI_SECRET_KEY in the host environment")
        if (
            not self.project_name
            or len(self.project_name) > 256
            or type(self.capture_content) is not bool
        ):
            raise ValueError("Invalid project_name or capture_content")

    @property
    def destination(self) -> str:
        # Account and policy changes must not silently reuse another destination's ACKs.
        # Only a digest is persisted, never credentials or endpoint text.
        return hashlib.sha256(
            json.dumps(
                [
                    "traceai-harbor-policy-v1",
                    self.endpoint,
                    self.project_name,
                    self.capture_content,
                    self.api_key,
                    self.secret_key,
                ]
            ).encode()
        ).hexdigest()


def send(payload: bytes, settings: Settings) -> None:
    """ACK only a full, valid OTLP success. Partial success remains unacknowledged."""
    with httpx.Client(
        timeout=httpx.Timeout(10.0), follow_redirects=False, trust_env=False
    ) as client:
        for attempt in range(3):
            deadline = time.monotonic() + 10
            try:
                with client.stream(
                    "POST",
                    settings.endpoint,
                    content=payload,
                    headers={
                        "Content-Type": "application/x-protobuf",
                        "Accept-Encoding": "identity",
                        "FI_API_KEY": settings.api_key,
                        "FI_SECRET_KEY": settings.secret_key,
                    },
                ) as response:
                    if response.status_code in {429, 502, 503, 504}:
                        category = "retryable_http"
                    elif response.status_code != 200:
                        raise DeliveryError("http_rejected")
                    else:
                        if (
                            response.headers.get("Content-Encoding", "identity")
                            != "identity"
                        ):
                            raise DeliveryError("invalid_response")
                        body = bytearray()
                        for chunk in response.iter_raw(chunk_size=4096):
                            if time.monotonic() > deadline:
                                raise DeliveryError("response_deadline")
                            body.extend(chunk)
                            if len(body) > 65536:
                                raise DeliveryError("invalid_response")
                        try:
                            ack = ExportTraceServiceResponse.FromString(bytes(body))
                        except DecodeError:
                            raise DeliveryError("invalid_response") from None
                        if (
                            ack.partial_success.rejected_spans
                            or ack.partial_success.error_message
                        ):
                            raise DeliveryError("partial_success")
                        return
            except httpx.HTTPError:
                category = "transport_error"
            if attempt < 2:
                time.sleep(0.1 * (2**attempt))
        raise DeliveryError(category)
