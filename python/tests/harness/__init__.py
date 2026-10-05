"""Small, loopback-only utilities for OTLP contract tests."""

from __future__ import annotations

import copy
import difflib
import json
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class Receiver:
    """An in-memory OTLP/HTTP receiver for tests.

    The receiver only listens on loopback and never forwards received spans.
    """

    def __init__(self) -> None:
        self._spans: list[dict[str, Any]] = []
        self._requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _read_body(self) -> bytes:
                # The Node OTLP exporter streams with Transfer-Encoding: chunked
                # and sends no Content-Length, so both framings are accepted.
                if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                    chunks = []
                    while True:
                        size_line = self.rfile.readline().split(b";", 1)[0].strip()
                        size = int(size_line, 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            return b"".join(chunks)
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                length = int(self.headers.get("Content-Length", "0"))
                return self.rfile.read(length)

            def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
                path = urlsplit(self.path).path
                if path not in ("/v1/traces", "/tracer/v1/traces"):
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return

                try:
                    body = self._read_body()
                    content_type = self.headers.get("Content-Type", "").lower()

                    if content_type.startswith("application/json"):
                        request = json.loads(body.decode("utf-8"))
                    elif content_type.startswith(("application/x-protobuf", "application/protobuf")):
                        from google.protobuf.json_format import MessageToDict
                        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                            ExportTraceServiceRequest,
                        )

                        protobuf_request = ExportTraceServiceRequest()
                        protobuf_request.ParseFromString(body)
                        request = MessageToDict(protobuf_request)
                    else:
                        self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                        return

                    spans: list[dict[str, Any]] = []
                    resource_attributes: list[dict[str, Any]] = []
                    for resource_spans in request.get(
                        "resourceSpans", request.get("resource_spans", [])
                    ):
                        resource_attributes.append(
                            _flatten_attributes(
                                resource_spans.get("resource", {}).get("attributes", [])
                            )
                        )
                        for scope_spans in resource_spans.get(
                            "scopeSpans", resource_spans.get("scope_spans", [])
                        ):
                            spans.extend(scope_spans.get("spans", []))
                except (ImportError, TypeError, UnicodeDecodeError, ValueError) as error:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(error))
                    return

                with owner._lock:
                    owner._spans.extend(copy.deepcopy(spans))
                    owner._requests.append(
                        {
                            "path": path,
                            "headers": {
                                key.lower(): value for key, value in self.headers.items()
                            },
                            "resource_attributes": resource_attributes,
                        }
                    )
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, _format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:{0}".format(self._server.server_port)
        self.endpoint = "{0}/v1/traces".format(self.origin)
        self.collector_endpoint = "{0}/tracer/v1/traces".format(self.origin)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def __enter__(self) -> "Receiver":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()

    def spans(self) -> list[dict[str, Any]]:
        """Return a copy of the decoded spans received so far."""
        with self._lock:
            return copy.deepcopy(self._spans)

    def requests(self) -> list[dict[str, Any]]:
        """Return one record per accepted export: path, lower-cased headers, resource attributes."""
        with self._lock:
            return copy.deepcopy(self._requests)

    def clear(self) -> None:
        """Remove every decoded span and request record received so far."""
        with self._lock:
            self._spans.clear()
            self._requests.clear()


def _flatten_attributes(attributes: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Turn OTLP key/value attributes into a plain dict of scalar values."""
    flat: dict[str, Any] = {}
    for attribute in attributes:
        value = attribute.get("value", {})
        for kind in ("stringValue", "boolValue", "intValue", "doubleValue"):
            if kind in value:
                flat[attribute.get("key", "")] = value[kind]
                break
        else:
            flat[attribute.get("key", "")] = value
    return flat


def run(
    argv: Sequence[str],
    env: Mapping[str, str],
    stdin: Optional[bytes],
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run one process and return its captured output, including after a timeout."""
    process = subprocess.Popen(
        argv,
        env=dict(env),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        stdout, stderr = process.communicate()

    result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    result.timed_out = timed_out
    return result


def post_otlp(spans: dict[str, Any], endpoint: str) -> int:
    """Post one JSON-encoded OTLP request to a loopback receiver."""
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1":
        raise ValueError("post_otlp only sends to a loopback HTTP endpoint")

    request = Request(
        endpoint,
        data=json.dumps(spans).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=5) as response:
        return response.status


def compare(actual: Sequence[dict[str, Any]], golden_path: Union[str, Path]) -> None:
    """Fail with a diff unless span names, attributes, and statuses match a golden."""
    with Path(golden_path).open(encoding="utf-8") as golden_file:
        expected = json.load(golden_file)

    def canonical(spans: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = []
        for span in spans:
            attributes = span.get("attributes", [])
            if isinstance(attributes, dict):
                attributes = [
                    {"key": key, "value": value} for key, value in attributes.items()
                ]
            normalized.append(
                {
                    "name": span.get("name", ""),
                    "attributes": sorted(
                        attributes,
                        key=lambda attribute: json.dumps(
                            attribute, sort_keys=True, separators=(",", ":")
                        ),
                    ),
                    "status": span.get("status", {}),
                }
            )
        return normalized

    actual_normalized = canonical(actual)
    expected_normalized = canonical(expected)
    if actual_normalized != expected_normalized:
        diff = "\n".join(
            difflib.unified_diff(
                json.dumps(expected_normalized, indent=2, sort_keys=True).splitlines(),
                json.dumps(actual_normalized, indent=2, sort_keys=True).splitlines(),
                fromfile=str(golden_path),
                tofile="actual",
                lineterm="",
            )
        )
        print(diff)
        raise AssertionError("spans do not match golden")
