"""Versioned fi.attachments.v1 adapter. No storage/source URLs are followed.

Only a separately configured HTTPS control origin is used. This adapter uses no
ambient proxies, cookies, netrc, redirects, or instrumentation. Server validation
and occurrence binding remain prerequisites for playback; a completed transfer
is only `validating`.
"""

import http.client
import json
import re
import ssl
import time
import uuid
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping, Optional
from urllib.parse import urlsplit

from fi_instrumentation.instrumentation.config import suppress_tracing

from .policy import ServerPolicy


class TransferError(Exception):
    def __init__(self, reason: str, retryable: bool = False, retry_after: float = 0):
        # Never preserve a server body, URL, token, or originating exception.
        safe_reasons = {"disabled", "bootstrap_pending", "policy_denied", "credential_scope_unsupported",
                        "source_unavailable", "unsupported_version", "endpoint_unconfigured", "scope_unresolved",
                        "redaction_unavailable", "recording_disabled", "expired", "deleted", "incomplete",
                        "too_large", "throttled", "conflict", "validation_failed"}
        reason = reason if reason in safe_reasons else "incomplete"
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable
        self.retry_after = min(max(retry_after, 0), 2)


@dataclass(frozen=True)
class Session:
    project_id: str
    capture_session_id: str
    policy: ServerPolicy
    expires_at: float
    max_upload_bytes: int


@dataclass(frozen=True)
class Upload:
    upload_id: str
    expires_at: float
    capability: str = field(default="", repr=False)


def _uuid(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value) or not uuid.UUID(value).int:
        raise TransferError("unsupported_version")
    return value


def _expiry(value) -> float:
    if not isinstance(value, str) or len(value) > 40:
        raise TransferError("unsupported_version")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError()
        timestamp = date.timestamp()
    except (ValueError, OverflowError):
        raise TransferError("unsupported_version") from None
    if timestamp <= time.time():
        raise TransferError("expired")
    return timestamp


@lru_cache(maxsize=6)
def _validator(name: str):
    schema = json.loads(Path(__file__).with_name("_protocol_schema.json").read_text())
    return Draft202012Validator({**schema, "$ref": "#/$defs/" + name}, format_checker=FormatChecker())


def _validate(name: str, value: dict) -> None:
    if not _validator(name).is_valid(value):
        raise TransferError("unsupported_version")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransferError("unsupported_version")
        result[key] = value
    return result


class HTTPTransport:
    """Trusted control-plane transport, not a generic media fetch client."""

    def __init__(self, endpoint: str, headers: Mapping[str, str], *, timeout: float = 2,
                 ssl_context: Optional[ssl.SSLContext] = None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise TransferError("endpoint_unconfigured")
        self._host, self._port = parsed.hostname, parsed.port or 443
        self._timeout, self._ssl_context = timeout, ssl_context
        self._headers = {k.lower(): v for k, v in headers.items()
                         if k.lower() in ("authorization", "x-api-key", "x-secret-key")}
        if not self._headers:
            raise TransferError("credential_scope_unsupported")

    def _request(self, method: str, path: str, *, body=None, payload=None,
                 capability: str = "", check: Callable[[], None] = lambda: None,
                 chunk_bytes: int = 256 * 1024):
        check()
        connection = http.client.HTTPSConnection(self._host, self._port,
                                                timeout=self._timeout, context=self._ssl_context)
        headers = dict(self._headers)
        if capability:
            headers = {"authorization": "Bearer " + capability}
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers["Content-Type"] = "application/json" if payload is None else "application/octet-stream"
        headers["Content-Length"] = str(len(data) if payload is None else len(payload))
        deadline = time.monotonic() + self._timeout
        try:
            with suppress_tracing():
                connection.putrequest(method, path)
                for key, value in headers.items():
                    connection.putheader(key, value)
                connection.endheaders()
                # FileLease has an independent bounded pread reader; it never
                # advances the application's file position or materializes a file.
                source = data if payload is None else payload
                for offset in range(0, len(source), chunk_bytes):
                    check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TransferError("incomplete", True)
                    if connection.sock:
                        connection.sock.settimeout(min(remaining, self._timeout))
                    if isinstance(source, bytes):
                        chunk = memoryview(source)[offset:offset + chunk_bytes]
                    else:
                        chunk = source.read(offset, min(chunk_bytes, len(source) - offset))
                        if len(chunk) != min(chunk_bytes, len(source) - offset):
                            raise TransferError("source_unavailable")
                    check()
                    connection.send(chunk)
                    del chunk
                if not isinstance(source, bytes):
                    source.check()
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransferError("incomplete", True)
                if connection.sock:
                    connection.sock.settimeout(remaining)
                response = connection.getresponse()
                if response.status not in (200, 201, 202, 204):
                    retry = response.status in (408, 429, 500, 502, 503, 504)
                    reason = {401: "credential_scope_unsupported", 403: "policy_denied",
                              404: "source_unavailable", 409: "conflict", 410: "expired",
                              413: "too_large", 429: "throttled"}.get(response.status, "incomplete")
                    retry_after = response.getheader("Retry-After", "0")
                    try:
                        retry_after = float(retry_after)
                    except ValueError:
                        retry_after = 0
                    raise TransferError(reason, retry, retry_after)
                # Bound responses and wall time even for a trickling server.
                parts, size = [], 0
                while True:
                    check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TransferError("incomplete", True)
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    part = response.read1(min(4096, 65537 - size))
                    if not part:
                        break
                    parts.append(part)
                    size += len(part)
                    if size > 65536:
                        raise TransferError("unsupported_version")
                value = json.loads(b"".join(parts), object_pairs_hook=_object) if size else {}
                if not isinstance(value, dict):
                    raise TransferError("unsupported_version")
                return value
        except TransferError:
            raise
        except (OSError, http.client.HTTPException):
            raise TransferError("incomplete", True) from None
        except Exception:
            raise TransferError("unsupported_version") from None
        finally:
            connection.close()

    def bootstrap(self, project_name: str, project_type: str, capture_session_id: str,
                  correlation_nonce: str) -> Session:
        result = self._request("POST", "/tracer/v1/attachment-sessions", body={
            "schema_versions": [1], "project_name": project_name, "project_type": project_type,
            "capture_session_id": capture_session_id, "correlation_nonce": correlation_nonce,
        })
        _validate("BootstrapResponse", result)
        try:
            if result["schema_version"] != 1 or result["transfer_modes"] != ["authenticated_stream"] or result["capture_session_id"] != capture_session_id:
                raise TransferError("unsupported_version")
            scope, policy, limits = result["scope"], result["policy"], result["limits"]
            for name in ("organization_id", "workspace_id", "project_id"):
                _uuid(scope[name])
            for name in ("enabled", "require_media_redaction", "require_metadata_removal",
                         "allow_unredacted_media_with_text_pii", "record_audio", "record_video"):
                if type(policy[name]) is not bool:
                    raise TransferError("unsupported_version")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", policy["version"]):
                raise TransferError("unsupported_version")
            for name in ("allowed_sources", "allowed_modalities"):
                if not isinstance(policy[name], list) or len(policy[name]) > 6 or not all(isinstance(v, str) for v in policy[name]):
                    raise TransferError("unsupported_version")
            for name, value in limits.items():
                if type(value) is not int or not 0 < value <= 1073741824:
                    raise TransferError("unsupported_version")
            maximum = limits["max_upload_bytes"]
            if maximum > 50 * 1024 * 1024:
                raise TransferError("unsupported_version")
            server = ServerPolicy(
                enabled=policy["enabled"] and "audio" in policy["allowed_modalities"],
                credential_write=True, allowed_sources=frozenset(policy["allowed_sources"]),
                allowed_directions=frozenset(("input", "output", "mixed")),
                require_media_redaction=policy["require_media_redaction"],
                require_metadata_removal=policy["require_metadata_removal"],
                allow_unredacted_media_with_text_pii=policy["allow_unredacted_media_with_text_pii"],
                record_audio=policy["record_audio"], version=policy["version"],
            )
            return Session(scope["project_id"], capture_session_id, server,
                           _expiry(result["expires_at"]), maximum)
        except TransferError:
            raise
        except Exception:
            raise TransferError("unsupported_version") from None

    @staticmethod
    def _base(session: Session) -> str:
        return "/tracer/v1/projects/" + _uuid(session.project_id) + "/attachments/uploads"

    def initialize(self, session: Session, request: dict, check: Callable[[], None]) -> Upload:
        _validate("UploadRequest", request)
        result = self._request("POST", self._base(session), body=request, check=check)
        _validate("UploadResponse", result)
        try:
            if (result["attachment_id"] != request["attachment_id"] or
                result["occurrence_id"] != request["occurrence_id"] or
                result["reserved_bytes"] != request["declared_bytes"] or
                result["state"] != "pending_upload" or result["transfer_mode"] != "authenticated_stream"):
                raise TransferError("unsupported_version")
            return Upload(_uuid(result["upload_id"]), _expiry(result["expires_at"]), result["write_capability"])
        except TransferError:
            raise
        except Exception:
            raise TransferError("unsupported_version") from None

    def transfer(self, session: Session, upload: Upload, payload: bytes,
                 check: Callable[[], None], chunk_bytes: int) -> None:
        self._request("PUT", self._base(session) + "/" + upload.upload_id + "/content",
                      payload=payload, capability=upload.capability, check=check, chunk_bytes=chunk_bytes)

    def complete(self, session: Session, upload: Upload, check: Callable[[], None]) -> None:
        result = self._request("POST", self._base(session) + "/" + upload.upload_id + "/complete",
                                  body={"policy_version": session.policy.version,
                                        "deletion_generation": session.policy.deletion_generation},
                                  capability=upload.capability, check=check)
        _validate("StatusResponse", result)
        if result.get("state") not in ("validating", "ready") or result.get("upload_id") != upload.upload_id:
            raise TransferError("unsupported_version")

    def abort(self, session: Session, upload: Upload) -> None:
        self._request("POST", self._base(session) + "/" + upload.upload_id + "/abort",
                      body={}, capability=upload.capability)
