"""The ONE versioned fi.attachments.v1 wire adapter (design section 9).

Every HTTP shape, path, header, idempotency key, status mapping and response
validation lives here. Managers, writers and imports call only this interface;
test doubles implement it. Only a separately configured HTTPS control origin is
used: no ambient proxies, cookies, netrc, redirects or instrumentation. A
completed transfer is only ``validating``; readiness is a server decision.

Importing this module imports ``jsonschema`` lazily (on first validation) and
``http.client``/``ssl``; only an ENABLED manager's transport construction does.
"""

import http.client
import json
import random
import re
import ssl
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from fi_instrumentation.instrumentation.config import suppress_tracing

from .errors import TransferError
from .policy import ServerPolicy
from .types import SAFE_REASONS, canonical_uuid

SKEW_SECONDS = 5.0
ATTEMPTS = 3
_SUCCESS_BODY = 64 * 1024
_ERROR_BODY = 4 * 1024
_CREDENTIAL_HEADERS = ("authorization", "x-api-key", "x-secret-key", "x-workspace-id")

# HTTP status -> (default reason, retryable, Error.code refinements allowed). Section 0.5.
_STATUS = {
    400: ("unsupported_version", False, frozenset(("unsupported_version",))),
    401: ("credential_scope_unsupported", False, frozenset(("credential_scope_unsupported", "expired"))),
    403: ("policy_denied", False, frozenset(("policy_denied", "redaction_unavailable", "recording_disabled",
                                             "correlation_unavailable", "credential_scope_unsupported"))),
    404: ("not_found", False, frozenset(("not_found", "scope_unresolved"))),
    409: ("conflict", False, frozenset(("conflict", "deleted", "expired", "policy_denied"))),
    410: ("expired", False, frozenset(("expired", "deleted"))),
    413: ("too_large", False, frozenset(("too_large",))),
    429: ("throttled", True, frozenset()),
}
for _code in (408, 500, 502, 503, 504):
    _STATUS[_code] = ("incomplete", True, frozenset())


def map_status(status: int, error_body: bytes = b"", retry_after: Optional[str] = None) -> TransferError:
    """Section 0.5: one table for every route, with in-class ``Error.code`` refinement."""
    reason, retryable, refinements = _STATUS.get(status, ("incomplete", False, frozenset()))
    if refinements and error_body:
        try:
            value = strict_json(error_body)
            if (isinstance(value, dict) and set(value) == {"code", "request_id"}
                    and canonical_uuid(value["request_id"]) and value["code"] in refinements):
                reason = value["code"]
        except Exception:
            pass
    delay = 0.0
    if status == 429:
        delay = 1.0
        if retry_after is not None:
            text = retry_after.strip()
            # Integer delta-seconds only; an HTTP-date is treated as 1 s.
            delay = min(float(int(text)), 5.0) if text.isdigit() else 1.0
    return TransferError(reason, retryable, delay, status)


def strict_json(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TransferError("unsupported_version")
            result[key] = value
        return result

    def constant(_value):
        raise TransferError("unsupported_version")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def encode(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=False).encode()


def hexid(value: str) -> str:
    return uuid.UUID(value).hex


def idempotency_key(route: str, identifier: str, sequence: Optional[int] = None) -> str:
    """Section 0.6 key derivations (F01 Token)."""
    prefix = {"bootstrap": "bs", "upload": "up", "complete": "cp", "abort": "ab",
              "manifest": "mf", "import": "im"}[route]
    key = prefix + "-" + hexid(identifier)
    return key if sequence is None else key + "-" + str(int(sequence))


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads(Path(__file__).with_name("_protocol_schema.json").read_text())


@lru_cache(maxsize=32)
def _validator(name: str):
    from jsonschema import Draft202012Validator, FormatChecker

    return Draft202012Validator({**_schema(), "$ref": "#/$defs/" + name}, format_checker=FormatChecker())


def is_valid(name: str, value) -> bool:
    return _validator(name).is_valid(value)


def wire_valid(name: str, value) -> bool:
    """Schema plus the cross-field rules mirrored from F01 ``validate.py`` (SDK shapes)."""
    try:
        if not is_valid(name, value):
            return False
        if name == "SpanAttributes":
            raw = value["fi.attachment.refs"]
            return len(raw.encode("utf-8")) <= 16384 and wire_valid("References", strict_json(raw.encode()))
        if name == "References":
            ids = [entry["occurrence_id"] for entry in value]
            return len(ids) == len(set(ids)) and all(wire_valid("Reference", entry) for entry in value)
        if name in ("Reference", "UploadRequest"):
            modality, mime = value["modality"], value["mime_type"]
            allowed = {"audio": mime.startswith("audio/"), "image": mime.startswith("image/"),
                       "video": mime.startswith("video/"), "document": mime == "application/pdf",
                       "file": mime == "application/octet-stream"}
            if not allowed[modality]:
                return False
        if name == "UploadRequest":
            if value["modality"] == "image" and value["declared_bytes"] > 20971520:
                return False
            if "recording_id" in value and value["declared_bytes"] > 8388608:
                return False
        if name == "RecordingImportRequest":
            expected = "RemoteUrl" if value["source"]["kind"] == "RemoteUrlEnvelope" else "ProviderFile"
            return value["recording"]["source_kind"] == expected and wire_valid(
                "CompletedRecordingImport", value["recording"])
        if name == "RecordingManifest":
            validate_manifest(value)
        return True
    except TransferError:
        return False
    except Exception:
        return False


def validate(name: str, value) -> None:
    if not is_valid(name, value):
        raise TransferError("unsupported_version")


def validate_wire(name: str, value) -> None:
    if not wire_valid(name, value):
        raise TransferError("unsupported_version")


def validate_manifest(manifest: dict) -> None:
    """Schema plus the cross-field rules mirrored from F01 ``validate.py``."""
    validate("RecordingManifest", manifest)
    if len(encode(manifest)) > 1048576:
        raise TransferError("too_large")
    if manifest["capture_complete"] and manifest["gaps"]:
        raise TransferError("validation_failed")
    track_ids = [track["track_id"] for track in manifest["tracks"]]
    channels = sorted(c for track in manifest["tracks"] for c in track["channels"])
    if len(track_ids) != len(set(track_ids)) or channels != list(range(manifest["channels"])):
        raise TransferError("validation_failed")
    if manifest["layout"] == "mixed" and any(t["direction"] != "mixed" for t in manifest["tracks"]):
        raise TransferError("validation_failed")
    if manifest["layout"] == "stereo" and manifest["channels"] != 2:
        raise TransferError("validation_failed")
    seen, previous = set(), -1
    for index, segment in enumerate(manifest["segments"]):
        if segment["index"] != index or segment["start_ms"] < previous or segment["occurrence_id"] in seen:
            raise TransferError("validation_failed")
        seen.add(segment["occurrence_id"])
        previous = segment["start_ms"]
    timelines = {t: [] for t in track_ids} if manifest["layout"] == "separate_tracks" else {None: []}
    for interval in manifest["segments"] + manifest["gaps"]:
        if interval.get("track_id") not in timelines:
            raise TransferError("validation_failed")
        timelines[interval.get("track_id")].append(interval)
    for intervals in timelines.values():
        end = 0
        for interval in sorted(intervals, key=lambda item: item["start_ms"]):
            if interval["start_ms"] != end:
                raise TransferError("validation_failed")
            end += interval["duration_ms"]
        if end != manifest["duration_ms"]:
            raise TransferError("validation_failed")
    for interval in manifest["segments"] + manifest["gaps"] + manifest["playout"]:
        if interval["start_ms"] + interval["duration_ms"] > manifest["duration_ms"]:
            raise TransferError("validation_failed")


@dataclass(frozen=True)
class Limits:
    max_upload_bytes: int = 8 * 1024 * 1024
    segment_bytes: int = 8 * 1024 * 1024
    segment_duration_ms: int = 30000
    recording_bytes: int = 1073741824
    recording_duration_ms: int = 7200000
    active_transfers: int = 16
    active_recordings: int = 4
    reserved_bytes: int = 1073741824
    validation_jobs: int = 1024
    initializations_per_minute: int = 120
    links_per_minute: int = 1000
    upload_bytes_per_second: int = 1073741824
    playback_bytes_per_second: int = 1073741824
    transfer_timeout_seconds: int = 600
    grant_ttl_seconds: int = 60
    revocation_check_seconds: int = 5


@dataclass(frozen=True)
class Session:
    project_id: str
    capture_session_id: str
    policy: ServerPolicy
    expires_at: float
    limits: Limits = field(default_factory=Limits)
    scope: Tuple[str, str, str] = ("", "", "")
    issued_at: float = 0.0
    request: Optional[bytes] = field(default=None, repr=False)

    @property
    def max_upload_bytes(self) -> int:
        return self.limits.max_upload_bytes

    def expiring(self, now: Optional[float] = None) -> bool:
        return self.expires_at - SKEW_SECONDS <= (time.time() if now is None else now)

    def renew_due(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        lifetime = max(self.expires_at - (self.issued_at or now), 0)
        return self.expires_at - now <= max(60.0, 0.2 * lifetime)


@dataclass(frozen=True)
class Upload:
    upload_id: str
    expires_at: float
    capability: str = field(default="", repr=False)
    reserved_bytes: int = 0

    def expiring(self) -> bool:
        return self.expires_at - SKEW_SECONDS <= time.time()


@dataclass(frozen=True)
class ImportJob:
    job_id: str
    recording_id: str


class Pacer:
    """One token bucket per manager at ``limits.upload_bytes_per_second``."""

    def __init__(self, rate: int = 1073741824):
        self._lock = threading.Lock()
        self.rate = max(int(rate), 1)
        self._tokens = float(self.rate)
        self._stamp = time.monotonic()

    def set_rate(self, rate: int) -> None:
        with self._lock:
            self.rate = max(int(rate), 1)
            self._tokens = min(self._tokens, float(self.rate))

    def acquire(self, size: int, deadline: float, check: Callable[[], None]) -> None:
        while True:
            check()
            with self._lock:
                now = time.monotonic()
                self._tokens = min(float(self.rate), self._tokens + (now - self._stamp) * self.rate)
                self._stamp = now
                if self._tokens >= min(size, self.rate):
                    self._tokens -= size
                    return
                wait = (min(size, self.rate) - self._tokens) / self.rate
            if now + wait > deadline:
                raise TransferError("incomplete", True)
            time.sleep(min(wait, 1.0))


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
    if timestamp - SKEW_SECONDS <= time.time():
        raise TransferError("expired")
    return timestamp


def _uuid(value) -> str:
    if canonical_uuid(value) is None:
        raise TransferError("unsupported_version")
    return value


def safe_headers(headers: Mapping[str, str]) -> dict:
    result = {}
    for key, value in (headers or {}).items():
        name = str(key).lower()
        if name in _CREDENTIAL_HEADERS:
            if not isinstance(value, str) or "\r" in value or "\n" in value or len(value) > 8192:
                raise TransferError("credential_scope_unsupported")
            result[name] = value
    return result


class HTTPTransport:
    """Trusted control-plane transport, not a generic media fetch client."""

    def __init__(self, endpoint: str, headers: Mapping[str, str], *, timeout: float = 2,
                 ssl_context: Optional[ssl.SSLContext] = None, workspace_id: Optional[str] = None):
        parsed = urlsplit(endpoint or "")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise TransferError("endpoint_unconfigured")
        self._host, self._port = parsed.hostname, parsed.port or 443
        self._timeout, self._ssl_context = float(timeout), ssl_context
        self._headers = safe_headers(headers)
        if not any(k in self._headers for k in ("authorization", "x-api-key", "x-secret-key")):
            raise TransferError("credential_scope_unsupported")
        explicit = workspace_id or self._headers.get("x-workspace-id")
        if explicit is not None and canonical_uuid(explicit) is None:
            raise TransferError("scope_unresolved")
        self.workspace_id = explicit

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, *, expect: int, deadline: float, body: Optional[bytes] = None,
                 source=None, length: int = 0, capability: str = "", key: Optional[str] = None,
                 check: Callable[[], None] = lambda: None, pacer: Optional[Pacer] = None,
                 chunk_bytes: int = 256 * 1024):
        check()
        if deadline - time.monotonic() <= 0:
            raise TransferError("incomplete", True)
        headers = {"authorization": "Bearer " + capability} if capability else dict(self._headers)
        if capability and (not re.fullmatch(r"[a-zA-Z0-9_.-]{16,4096}", capability)):
            raise TransferError("credential_scope_unsupported")
        headers["Cache-Control"] = "no-store"
        if key is not None:
            headers["Idempotency-Key"] = key
        if source is not None:
            headers["Content-Type"] = "application/octet-stream"
            headers["Content-Length"] = str(length)
        elif body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPSConnection(
            self._host, self._port, timeout=max(min(self._timeout, deadline - time.monotonic()), 0.001),
            context=self._ssl_context)

        def arm(stall: float) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransferError("incomplete", True)
            if connection.sock:
                connection.sock.settimeout(min(remaining, stall))

        try:
            with suppress_tracing():
                connection.putrequest(method, path, skip_accept_encoding=True)
                for name, value in headers.items():
                    connection.putheader(name, value)
                connection.endheaders()
                if body is not None:
                    arm(self._timeout)
                    connection.send(body)
                if source is not None:
                    for offset in range(0, length, chunk_bytes):
                        check()
                        size = min(chunk_bytes, length - offset)
                        if pacer is not None:
                            pacer.acquire(size, deadline, check)
                        chunk = source.read(offset, size)
                        if len(chunk) != size:
                            raise TransferError("source_unavailable")
                        check()
                        arm(self._timeout)
                        connection.send(chunk)
                        del chunk
                    source.check()
                check()
                arm(max(self._timeout, deadline - time.monotonic()))
                response = connection.getresponse()
                if response.status != expect:
                    retry_after = response.getheader("Retry-After")
                    raw = self._read(response, _ERROR_BODY, deadline, check, strict=False)
                    raise map_status(response.status, raw, retry_after)
                raw = self._read(response, _SUCCESS_BODY, deadline, check, strict=True)
                if expect == 204:
                    return None
                value = strict_json(raw) if raw else None
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

    def _read(self, response, limit: int, deadline: float, check, *, strict: bool) -> bytes:
        parts, size = [], 0
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransferError("incomplete", True)
            sock = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(sock, "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            part = response.read1(min(4096, limit + 1 - size))
            if not part:
                return b"".join(parts)
            parts.append(part)
            size += len(part)
            if size > limit:
                if strict:
                    raise TransferError("unsupported_version")
                return b""

    # ---------------------------------------------------------------- routes
    def bootstrap_body(self, project_name: str, project_type: str, capture_session_id: str,
                       correlation_nonce: str) -> dict:
        body = {"schema_versions": [1], "project_name": project_name, "project_type": project_type,
                "capture_session_id": capture_session_id, "correlation_nonce": correlation_nonce}
        if self.workspace_id:
            body["workspace_id"] = self.workspace_id
        return body

    def bootstrap(self, body: dict, *, key: str, deadline: float) -> Session:
        validate("BootstrapRequest", body)
        raw = encode(body)
        result = self._request("POST", "/tracer/v1/attachment-sessions", expect=200, deadline=deadline,
                               body=raw, key=key)
        return self._session(result, body, raw)

    def renew_session(self, current: Session, *, sequence: int, deadline: float) -> Session:
        if current.request is None:
            raise TransferError("expired")
        body = strict_json(current.request)
        session = self.bootstrap(body, key=idempotency_key("bootstrap", current.capture_session_id, sequence),
                                 deadline=deadline)
        if session.scope != current.scope or session.project_id != current.project_id:
            raise TransferError("scope_unresolved")
        if session.expires_at <= current.expires_at:
            raise TransferError("expired")
        return session

    def _session(self, result, body: dict, raw: bytes) -> Session:
        validate("BootstrapResponse", result)
        if result["capture_session_id"] != body["capture_session_id"]:
            raise TransferError("unsupported_version")
        scope, policy, limits = result["scope"], result["policy"], result["limits"]
        if body.get("workspace_id") and scope["workspace_id"] != body["workspace_id"]:
            raise TransferError("scope_unresolved")
        server = ServerPolicy(
            enabled=policy["enabled"], credential_write=True,
            allowed_sources=frozenset(policy["allowed_sources"]),
            allowed_modalities=frozenset(policy["allowed_modalities"]),
            require_media_redaction=policy["require_media_redaction"],
            require_metadata_removal=policy["require_metadata_removal"],
            allow_unredacted_media_with_text_pii=policy["allow_unredacted_media_with_text_pii"],
            record_audio=policy["record_audio"], record_video=policy["record_video"],
            version=policy["version"], deletion_generation=0,
        )
        return Session(scope["project_id"], body["capture_session_id"], server, _expiry(result["expires_at"]),
                       Limits(**limits), (scope["organization_id"], scope["workspace_id"], scope["project_id"]),
                       time.time(), raw)

    @staticmethod
    def _base(session: Session) -> str:
        return "/tracer/v1/projects/" + _uuid(session.project_id) + "/attachments"

    def initialize(self, session: Session, body: bytes, *, key: str, deadline: float, check) -> Upload:
        request = strict_json(body)
        validate_wire("UploadRequest", request)
        result = self._request("POST", self._base(session) + "/uploads", expect=201, deadline=deadline,
                               body=body, key=key, check=check)
        validate("UploadResponse", result)
        if (result["attachment_id"] != request["attachment_id"] or result["occurrence_id"] != request["occurrence_id"]
                or result["reserved_bytes"] != request["declared_bytes"]):
            raise TransferError("unsupported_version")
        return Upload(result["upload_id"], _expiry(result["expires_at"]), result["write_capability"],
                      result["reserved_bytes"])

    def renew_upload(self, session: Session, upload: Upload, body: bytes, *, deadline: float) -> Upload:
        request = strict_json(body)
        try:
            renewed = self.initialize(session, body, key=idempotency_key("upload", request["occurrence_id"]),
                                      deadline=deadline, check=lambda: None)
        except TransferError as error:
            if error.reason in ("conflict", "expired", "deleted"):
                raise TransferError("expired") from None
            raise
        if (renewed.upload_id != upload.upload_id or renewed.reserved_bytes != upload.reserved_bytes
                or renewed.capability == upload.capability or renewed.expires_at <= upload.expires_at):
            raise TransferError("expired")
        return renewed

    def put_content(self, session: Session, upload: Upload, source, *, deadline: float, check, pacer: Pacer,
                    chunk_bytes: int = 256 * 1024) -> None:
        length = len(source)
        if length != upload.reserved_bytes:
            raise TransferError("source_unavailable")
        result = self._request("PUT", self._base(session) + "/uploads/" + upload.upload_id + "/content",
                               expect=200, deadline=deadline, source=source, length=length,
                               capability=upload.capability, check=check, pacer=pacer, chunk_bytes=chunk_bytes)
        validate("StatusResponse", result)
        if result["upload_id"] != upload.upload_id or result["state"] != "pending_upload":
            raise TransferError("unsupported_version")

    def _settle_body(self, session: Session) -> bytes:
        return encode({"policy_version": session.policy.version, "deletion_generation": 0})

    def complete(self, session: Session, upload: Upload, *, key: str, deadline: float, check) -> str:
        result = self._request("POST", self._base(session) + "/uploads/" + upload.upload_id + "/complete",
                               expect=202, deadline=deadline, body=self._settle_body(session), key=key,
                               capability=upload.capability, check=check)
        validate("StatusResponse", result)
        # Accepted is not ready: the SDK reports validating in every order.
        if result["upload_id"] != upload.upload_id or result["state"] not in ("validating", "ready"):
            raise TransferError("unsupported_version")
        return "validating"

    def abort(self, session: Session, upload: Upload, *, key: str, deadline: float) -> None:
        result = self._request("POST", self._base(session) + "/uploads/" + upload.upload_id + "/abort",
                               expect=200, deadline=deadline, body=self._settle_body(session), key=key,
                               capability=upload.capability)
        validate("StatusResponse", result)

    def upload_status(self, session: Session, upload: Upload, *, deadline: float) -> Tuple[str, str]:
        result = self._request("GET", self._base(session) + "/uploads/" + upload.upload_id + "/status",
                               expect=200, deadline=deadline, capability=upload.capability)
        validate("StatusResponse", result)
        return result["state"], result["reason"]

    def put_manifest(self, session: Session, capability: str, recording_id: str, body: bytes, *, key: str,
                     deadline: float, check) -> None:
        validate_manifest(strict_json(body))
        self._request("PUT", self._base(session) + "/recordings/" + _uuid(recording_id) + "/manifest",
                      expect=204, deadline=deadline, body=body, key=key, capability=capability, check=check)

    def submit_import(self, session: Session, body: bytes, *, key: str, deadline: float, check) -> ImportJob:
        request = strict_json(body)
        validate_wire("RecordingImportRequest", request)
        result = self._request("POST", self._base(session) + "/imports", expect=202, deadline=deadline,
                               body=body, key=key, check=check)
        validate("RecordingImportResponse", result)
        if result["recording_id"] != request["recording"]["recording_id"]:
            raise TransferError("unsupported_version")
        return ImportJob(result["job_id"], result["recording_id"])

    def import_status(self, session: Session, job_id: str, *, deadline: float) -> Tuple[str, str]:
        result = self._request("GET", self._base(session) + "/imports/" + _uuid(job_id) + "/status",
                               expect=200, deadline=deadline)
        validate("ImportStatusResponse", result)
        return result["state"], result["reason"]


def backoff(attempt: int, error: TransferError) -> float:
    """Delay before attempt n (n = 1, 2): max(retry_after, U(0.5, 1.0) * 100 ms * 2^n)."""
    return max(error.retry_after, random.uniform(0.5, 1.0) * 0.1 * (2 ** attempt))
