"""Loads the shared wire-layer golden fixtures (spec/auto-instrumentation/fixtures)."""

import gzip
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "spec" / "auto-instrumentation" / "fixtures"
BOUNDARY = "traceaifixtureboundary"


def load_fixtures() -> List[Dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURE_DIR.rglob("*.json"))]


def _compact(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_body(spec: Dict[str, Any]) -> Tuple[Dict[str, str], Optional[bytes]]:
    """Headers and body bytes for a fixture request."""
    headers = dict(spec.get("headers") or {})
    if spec.get("unavailable"):
        return headers, None
    if "json" in spec:
        headers.setdefault("content-type", "application/json")
        return headers, _compact(spec["json"])
    if "text" in spec:
        return headers, spec["text"].encode("utf-8")
    if "binary" in spec:
        headers.setdefault("content-type", spec["binary"]["content_type"])
        return headers, b"\0" * spec["binary"]["size"]
    if "multipart" in spec:
        parts = []
        for name, value in spec["multipart"].get("fields", {}).items():
            parts.append(
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        for file in spec["multipart"].get("files", []):
            head = (
                f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{file["name"]}"; '
                f'filename="{file["filename"]}"\r\nContent-Type: {file["content_type"]}\r\n\r\n'
            ).encode()
            parts.append(head + b"\0" * file["size"] + b"\r\n")
        parts.append(f"--{BOUNDARY}--\r\n".encode())
        headers["content-type"] = f"multipart/form-data; boundary={BOUNDARY}"
        return headers, b"".join(parts)
    return headers, b""


def _response_chunks(spec: Dict[str, Any]) -> Tuple[Dict[str, str], List[bytes]]:
    """Headers and body chunks for a fixture response (one chunk per SSE/NDJSON item)."""
    headers = dict(spec.get("headers") or {})
    if "json" in spec:
        return headers, [_compact(spec["json"])]
    if "text" in spec:
        return headers, [spec["text"].encode("utf-8")]
    if "binary" in spec:
        size = spec["binary"]["size"]
        return headers, [b"\1" * (size // 2), b"\1" * (size - size // 2)]
    if "sse" in spec:
        chunks = []
        for item in spec["sse"]:
            if isinstance(item, str):
                chunks.append(f"data: {item}\n\n".encode())
                continue
            item = dict(item)
            event = item.pop("event", None)
            prefix = f"event: {event}\n" if event else ""
            chunks.append(prefix.encode() + b"data: " + _compact(item) + b"\n\n")
        return headers, chunks
    if "ndjson" in spec:
        return headers, [_compact(item) + b"\n" for item in spec["ndjson"]]
    return headers, []


def response_chunks(spec: Dict[str, Any]) -> Tuple[Dict[str, str], List[bytes]]:
    headers, chunks = _response_chunks(spec)
    if spec.get("gzip"):
        headers["content-encoding"] = "gzip"
        chunks = [gzip.compress(b"".join(chunks))]
    if "chunk_sizes" in spec:
        wire = b"".join(chunks)
        chunks, start = [], 0
        for size in spec["chunk_sizes"]:
            chunks.append(wire[start:start + size])
            start += size
        assert start == len(wire)
    if "error_after_chunks" in spec:
        chunks = chunks[:spec["error_after_chunks"]]
    return headers, chunks


def response_arrivals(spec: Dict[str, Any], chunks: List[bytes]) -> List[Tuple[int, float]]:
    arrivals, size = [], 0
    for chunk, offset in zip(chunks, spec.get("arrival_offsets_ms", ())):
        size += len(chunk)
        if chunk:
            arrivals.append((size, offset / 1000))
    return arrivals


def assert_expected(fixture: Dict[str, Any], name: str, attributes: Dict[str, Any], ok: bool, description: Any) -> None:
    expect = fixture["expect"]
    label = fixture["name"]
    assert name == expect["name"], f"{label}: span name {name!r} != {expect['name']!r}"
    assert ok == (expect["status"] == "OK"), f"{label}: status ok={ok}"
    if "status_description" in expect:
        assert description == expect["status_description"], f"{label}: description {description!r}"
    for key, want in expect.get("attributes", {}).items():
        assert key in attributes, f"{label}: missing {key}"
        got = attributes[key]
        if isinstance(want, list):
            got = list(got)
        assert got == want, f"{label}: {key} = {got!r}, want {want!r}"
    for key in expect.get("present", []):
        assert key in attributes, f"{label}: missing {key}"
    for key in expect.get("absent", []):
        assert key not in attributes, f"{label}: unexpected {key} = {attributes.get(key)!r}"

    for key, forbidden in expect.get("not_contains", {}).items():
        if key in attributes:
            for text in forbidden:
                assert text not in str(attributes[key]), f"{label}: {key} contains {text!r}"
