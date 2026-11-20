"""Provider-owned, bounded, best-effort attachment transfer runtime (design sections 1-9).

Disabled is a strict no-op: ``create_attachment_manager`` returns the
``DisabledAttachmentManager`` null object (no thread, timer, fork hook, atexit
hook, schema or network import). An enabled manager starts one bootstrap
daemon thread at construction and its worker daemon threads lazily on the
first job. Every wait is bounded (<= 1 s slices that re-check ``stopped``).
One fork hook per process resets live managers in the child without touching
inherited locks. This is not a durable spool: a hard exit may lose uploads.
"""

import asyncio
import dataclasses
import json
import re
import threading
import time
import uuid
import weakref
from collections import deque
from typing import Mapping, Optional, Union

from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.sdk.trace import SpanProcessor

from fi_instrumentation.instrumentation.config import TraceConfig

from ._disabled import DisabledAttachmentManager, OwnedRecordingHandle
from .admission import AdmissionTicket, _Reservation
from .errors import TransferError
from .imports import CompletedRecordingRequest, EnvelopeSource, ImportHandle, ProviderFileSource, \
    ResolvedCompletedRecording
from .policy import AttachmentConfig, AttachmentPolicy, CapturePolicy, ServerPolicy, env_disabled
from .recording import PcmFormat, RecordingOptions, TrackSpec, RecordingWriter, build_manifest, segment_frames, to_ms, \
    validate_options, MAX_SEGMENT_BYTES, MAX_SEGMENT_MS
from .sources import MemorySource, OwnedRecordingFile, WavSegmentSource, parse_wav
from .stream import AttachmentStream, StreamOptions
from .types import AUDIO_MIMES, MIME_MODALITIES, PURPOSES, REFERENCE_ROLES, TOKEN, AttachmentHandle, \
    AttachmentStatus, Bytes, Completion, CorrelationHandle, FlushResult, canonical_uuid

MiB = 1024 * 1024
WAIT_SLICE = 1.0

_LIVE: "weakref.WeakSet" = weakref.WeakSet()
_FORK_LOCK = threading.Lock()
_FORK_HOOK_REGISTERED = False


def _after_fork_in_child() -> None:
    for manager in list(_LIVE):
        try:
            manager._reset_after_fork()
        except Exception:
            pass


def _register_fork_hook_once() -> None:
    """One ``os.register_at_fork(after_in_child=...)`` per process, ever (8.4)."""
    global _FORK_HOOK_REGISTERED
    import os

    if _FORK_HOOK_REGISTERED or not hasattr(os, "register_at_fork"):
        return
    with _FORK_LOCK:
        if not _FORK_HOOK_REGISTERED:
            os.register_at_fork(after_in_child=_after_fork_in_child)
            _FORK_HOOK_REGISTERED = True


class _Lane:
    """Strict per-recording order: at most one job in flight."""

    __slots__ = ("writer", "jobs", "busy", "scheduled", "__weakref__")

    def __init__(self, writer):
        self.writer, self.jobs, self.busy, self.scheduled = writer, deque(), False, False


class _Job:
    __slots__ = ("kind", "handle", "body", "payload", "spec", "epoch", "nbytes", "descriptors", "cancelled",
                 "upload", "renewals", "lane", "entry", "extra", "initialized", "index", "occurrence_id")

    def __init__(self, kind, *, handle=None, body=None, payload=None, spec=None, epoch=0, nbytes=0,
                 descriptors=1, lane=None, entry=None, extra=None, occurrence_id=None):
        self.kind, self.handle, self.body, self.payload, self.spec = kind, handle, body, payload, spec
        self.epoch, self.nbytes, self.descriptors, self.lane, self.entry = epoch, nbytes, descriptors, lane, entry
        self.extra, self.occurrence_id = extra or {}, occurrence_id
        self.cancelled, self.upload, self.renewals, self.initialized, self.index = False, None, 0, False, None


def create_attachment_manager(*, config: Optional[AttachmentConfig] = None, trace_config: Optional[TraceConfig] = None,
                              project_name: str = "", project_type: str = "observe",
                              headers: Optional[Mapping[str, str]] = None, transport=None):
    """Resolve ``enabled`` ONCE; OFF returns the null object (design 8.1)."""
    config = config or AttachmentConfig()
    if not config.enabled or env_disabled():
        return DisabledAttachmentManager(config=config, trace_config=trace_config)
    return AttachmentManager(config=config, trace_config=trace_config, project_name=project_name,
                             project_type=project_type, headers=headers, transport=transport)


class AttachmentManager:
    """One runtime per provider. No network, source reads or threads when OFF.

    A transport double implements the section 9 ``AttachmentTransport``
    protocol for component qualification; it must honor the supplied ``check``
    before every bounded I/O chunk and the supplied deadlines.
    """

    def __init__(self, *, config: Optional[AttachmentConfig] = None, trace_config: Optional[TraceConfig] = None,
                 project_name: str = "", project_type: str = "observe", headers: Optional[Mapping[str, str]] = None,
                 transport=None):
        self.config = config or AttachmentConfig()
        self.policy = AttachmentPolicy(self.config, trace_config)
        self._project_name, self._project_type = project_name, project_type
        self._transport = transport
        self._p = None
        self._enabled = bool(self.config.enabled and not env_disabled())
        initial = "disabled"
        if self._enabled:
            from . import protocol

            self._p = protocol
            if not project_name:
                initial = "scope_unresolved"
            elif transport is None:
                try:
                    self._transport = protocol.HTTPTransport(
                        self.config.endpoint or "", headers or {}, timeout=self.config.request_timeout,
                        workspace_id=self.config.workspace_id)
                    initial = "bootstrap_pending"
                except TransferError as error:
                    initial = error.reason
                except Exception:
                    initial = "endpoint_unconfigured"
            else:
                initial = "bootstrap_pending"
        self._initial_state = initial
        self._reset(initial)
        if self._enabled:
            _LIVE.add(self)
            _register_fork_hook_once()
            if initial == "bootstrap_pending":
                self._start_bootstrap()

    # ------------------------------------------------------------ lifecycle
    def _reset(self, state: str) -> None:
        self._cond = threading.Condition()
        self._ready_event = threading.Event()
        self._ready_q: deque = deque()
        self._lanes: "set" = set()
        self._active = 0
        self._queued = 0
        self._bytes = 0
        self._descriptors = 0
        self._dropped = 0
        self._epoch = 0
        self._stopping = False
        self._stopped = False
        self._threads: list = []
        self._bootstrap_thread = None
        self._renew_pending = False
        self._renew_after = 0.0
        self._renew_sequence = 0
        self._session = None
        self._state = state
        self._tickets: "weakref.WeakSet" = weakref.WeakSet()
        self._streams: "weakref.WeakSet" = weakref.WeakSet()
        self._recordings: set = set()
        self._active_jobs: dict = {}
        self._inflight: set = set()
        self._running: dict = {}
        self._shutdown_result: Optional[FlushResult] = None
        self._shutdown_done = threading.Event()
        self._shutdown_deadline: Optional[float] = None
        self._pacer = self._p.Pacer() if self._p is not None else None
        self.capture_session_id = str(uuid.uuid4())
        self.correlation_nonce = str(uuid.uuid4())
        if state != "bootstrap_pending":
            self._ready_event.set()

    def _reset_after_fork(self) -> None:
        """Child only. Never acquires an inherited lock; the parent keeps uploading."""
        writers, streams = list(self._recordings), list(self._streams)
        state = "bootstrap_pending" if self._initial_state == "bootstrap_pending" else self._initial_state
        self._reset(state)
        for writer in writers:
            writer._reset_after_fork()
        for stream in streams:
            stream._lock = threading.Lock()
            stream._done, stream.reason, stream._buffer, stream._ticket = True, "incomplete", bytearray(), None

    def _start_bootstrap(self) -> None:
        with self._cond:
            if (self._bootstrap_thread is not None or self._stopped or self._stopping
                    or self._state != "bootstrap_pending"):
                return
            worker = threading.Thread(target=self._bootstrap, daemon=True, name="fi-attachment-bootstrap")
            self._bootstrap_thread = worker
        worker.start()

    def _bootstrap_body(self) -> dict:
        builder = getattr(self._transport, "bootstrap_body", None)
        if builder is not None:
            return builder(self._project_name, self._project_type, self.capture_session_id, self.correlation_nonce)
        body = {"schema_versions": [1], "project_name": self._project_name, "project_type": self._project_type,
                "capture_session_id": self.capture_session_id, "correlation_nonce": self.correlation_nonce}
        if self.config.workspace_id:
            body["workspace_id"] = self.config.workspace_id
        return body

    def _bootstrap(self) -> None:
        p = self._p
        try:
            body = self._bootstrap_body()
            key = p.idempotency_key("bootstrap", self.capture_session_id, 0)
            session = self._attempts(lambda: self._transport.bootstrap(
                body, key=key, deadline=time.monotonic() + self.config.request_timeout), lambda: self._alive(),
                self.config.request_timeout * 3)
            with self._cond:
                if self._stopped or self._stopping:
                    return
                self._session = session
                self._state = "ready"
                self._pacer.set_rate(session.limits.upload_bytes_per_second)
        except TransferError as error:
            with self._cond:
                if not (self._stopped or self._stopping):
                    self._state = error.reason if error.reason != "incomplete" else "bootstrap_pending"
                    if self._state == "bootstrap_pending":
                        # A transport failure leaves bootstrap retryable on the next activity.
                        self._bootstrap_thread = None
        except Exception:
            with self._cond:
                if not (self._stopped or self._stopping):
                    self._state = "unsupported_version"
        finally:
            self._ready_event.set()
            with self._cond:
                self._cond.notify_all()

    def _alive(self) -> None:
        if self._stopped:
            raise TransferError("incomplete")

    def _ensure_workers(self) -> None:
        # Caller holds self._cond.
        if self._threads or self._stopped:
            return
        for index in range(self.config.workers):
            worker = threading.Thread(target=self._worker, daemon=True, name="fi-attachment-worker-%d" % index)
            self._threads.append(worker)
            worker.start()

    # -------------------------------------------------------------- status
    def _runtime_state(self) -> str:
        if self._stopped or self._stopping:
            return "incomplete"
        state = self._state
        if state == "ready" and (self._session is None or self._session.expiring()):
            return "expired"
        return state

    def _session_or_none(self):
        return self._session

    def _limits(self):
        session = self._session
        return session.limits if session is not None else self._p.Limits()

    def ready(self, timeout: float = 5) -> bool:
        if self._state == "bootstrap_pending":
            self._start_bootstrap()
            deadline = time.monotonic() + max(0.0, min(float(timeout), 30.0))
            while not self._ready_event.is_set() and time.monotonic() < deadline:
                self._ready_event.wait(min(WAIT_SLICE, max(deadline - time.monotonic(), 0)))
        return self._runtime_state() == "ready"

    async def ready_async(self, timeout: float = 5) -> bool:
        return await asyncio.to_thread(self.ready, timeout)

    def status(self) -> AttachmentStatus:
        self._sweep()
        with self._cond:
            return AttachmentStatus(
                self._runtime_state(), self._queued, self._active,
                sum(1 for t in self._tickets if not t._used),
                sum(1 for s in self._streams if s.open), len(self._recordings), self._bytes, self._dropped)

    @property
    def owned_bytes(self) -> int:
        with self._cond:
            return self._bytes

    # ------------------------------------------------------------- budget
    def _release(self, nbytes: int, descriptors: int = 1) -> None:
        with self._cond:
            self._bytes = max(0, self._bytes - nbytes)
            self._descriptors = max(0, self._descriptors - descriptors)
            self._cond.notify_all()

    def _reserve(self, nbytes: int) -> bool:
        with self._cond:
            if self._bytes + nbytes > self.config.max_owned_bytes:
                return False
            self._bytes += nbytes
            return True

    def _track_ticket(self, ticket) -> None:
        self._tickets.add(ticket)

    def _track_stream(self, stream) -> None:
        self._streams.add(stream)

    def _sweep(self) -> None:
        """Lazy expiry of tickets and idle streams at manager entry points (no timers)."""
        if self._p is None:
            return
        now = time.monotonic()
        for ticket in list(self._tickets):
            ticket._sweep()
        for stream in list(self._streams):
            stream._sweep(now)
        self._maybe_renew()

    def _maybe_renew(self) -> None:
        with self._cond:
            session = self._session
            if (session is None or self._state != "ready" or self._renew_pending or self._stopping or self._stopped
                    or time.monotonic() < self._renew_after or not session.renew_due()):
                return
            self._renew_pending = True
            self._ready_q.append(_Job("renew", descriptors=0))
            self._ensure_workers()
            self._cond.notify_all()

    # ------------------------------------------------------------ preflight
    def _cap(self, source_kind: str, mime_type: str) -> int:
        limits = self._limits()
        cap = min(limits.max_upload_bytes, 50 * MiB)
        if MIME_MODALITIES.get(mime_type) == "image":
            cap = min(cap, 20 * MiB)
        if source_kind in ("Bytes", "Blob"):
            cap = min(cap, self.config.max_owned_bytes)
        return cap

    def _admission_reason(self, capture, *, mime_type, direction, source_kind, operation, purpose,
                          import_source=False) -> Optional[str]:
        """Steps 1-8 (no source access)."""
        if not self._enabled or env_disabled():
            return "disabled"
        if self._stopped or self._stopping:
            return "incomplete"
        state = self._runtime_state()
        if state == "bootstrap_pending":
            self._start_bootstrap()
        if state != "ready":
            return state
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return "policy_denied"
        reason = self.policy.evaluate(self._session.policy, capture, direction=direction, mime_type=mime_type,
                                      source_kind=source_kind, operation=operation, purpose=purpose,
                                      import_source=import_source)
        if reason:
            return reason
        if source_kind not in ("Bytes", "Blob", "OwnedRecordingFile") and not import_source:
            return "source_unavailable"
        return None

    def preflight(self, target=None, *, mime_type: str, direction: str, max_bytes: int,
                  operation: str = "observe_consumed", source_kind: str = "Bytes", purpose: Optional[str] = None,
                  capture_policy: Optional[CapturePolicy] = None, ttl: Optional[float] = None,
                  _import_source: bool = False, _skip_cap: bool = False) -> AdmissionTicket:
        """Full deny-wins check without touching the source; reserves a slot and budget."""
        try:
            self._sweep()
            capture = capture_policy or CapturePolicy()
            reason = self._admission_reason(capture, mime_type=mime_type, direction=direction,
                                            source_kind=source_kind, operation=operation, purpose=purpose,
                                            import_source=_import_source)
            if reason:
                return AdmissionTicket(None, reason)
            span = trace.get_current_span() if target is None else target
            try:
                correlation = CorrelationHandle.from_span(span)
            except Exception:
                return AdmissionTicket(None, "correlation_unavailable")
            if not correlation.sampled:
                return AdmissionTicket(None, "unsampled")
            if type(max_bytes) is not int or max_bytes <= 0 or (not _skip_cap and max_bytes > self._cap(source_kind, mime_type)):
                return AdmissionTicket(None, "too_large")
            reserve = min(self.config.chunk_bytes, max_bytes) if source_kind == "OwnedRecordingFile" else max_bytes
            if _import_source:
                reserve = 0
            with self._cond:
                if self._stopped or self._stopping:
                    return AdmissionTicket(None, "incomplete")
                if (self._descriptors >= self.config.max_descriptors
                        or self._bytes + reserve > self.config.max_owned_bytes):
                    return AdmissionTicket(None, "queue_full")
                self._descriptors += 1
                self._bytes += reserve
            ttl = self.config.ticket_ttl if ttl is None else min(max(float(ttl), 0.0), 30.0)
            projectable = span if not isinstance(span, CorrelationHandle) and hasattr(span, "is_recording") else None
            return AdmissionTicket(self, "queued", max_bytes=max_bytes, correlation=correlation, span=projectable,
                                   mime_type=mime_type, direction=direction, operation=operation,
                                   source_kind=source_kind, purpose=purpose, capture=capture, ttl=ttl,
                                   reservation=_Reservation(self, reserve, 1))
        except Exception:
            return AdmissionTicket(None, "source_unavailable")

    def _recheck_ticket(self, ticket) -> Optional[str]:
        mime, direction, operation, source_kind, purpose, capture = ticket._spec
        return self._admission_reason(capture, mime_type=mime, direction=direction, source_kind=source_kind,
                                      operation=operation, purpose=purpose)

    # --------------------------------------------------------------- attach
    def attach(self, span_or_context=None, source=None, *, mime_type: str, direction: str,
               purpose: Optional[str] = None, conversation_id: Optional[str] = None, turn_id: Optional[str] = None,
               tool_call_id: Optional[str] = None, message_index: Optional[int] = None,
               part_index: Optional[int] = None, role: Optional[str] = None, channel: Optional[int] = None,
               capture_policy: Optional[CapturePolicy] = None) -> AttachmentHandle:
        """``preflight(max_bytes=len(source))`` followed by ``ticket.attach``. Never raises."""
        try:
            kind = type(source).__name__
            if type(source) is Bytes:
                data = source.data
                if type(data) not in (bytes, bytearray, memoryview):
                    return self._deny_handle(capture_policy, mime_type, direction, kind, purpose, "source_unavailable")
                length = data.nbytes if type(data) is memoryview else len(data)
            elif type(source) is OwnedRecordingFile:
                length = source.size
            else:
                # Unqualified sources cannot trigger even stat(), DNS, or a client call.
                return self._deny_handle(capture_policy, mime_type, direction, kind, purpose, "source_unavailable")
            operation = "import_existing" if type(source) is OwnedRecordingFile else "observe_consumed"
            ticket = self.preflight(span_or_context, mime_type=mime_type, direction=direction,
                                    max_bytes=length, operation=operation,
                                    source_kind=kind, purpose=purpose, capture_policy=capture_policy)
            return ticket.attach(source, message_index=message_index, part_index=part_index, role=role,
                                 channel=channel, conversation_id=conversation_id, turn_id=turn_id,
                                 tool_call_id=tool_call_id)
        except Exception:
            # Caller objects, malformed contexts and instrumentation errors must
            # not fail inference or reach a logger with their repr/exception.
            return AttachmentHandle("source_unavailable")

    def _deny_handle(self, capture, mime, direction, kind, purpose, fallback) -> AttachmentHandle:
        # Steps 1-7 still win (deny-wins), evaluated without the source: its kind is
        # unqualified (step 8), so source authorization is not what decides here.
        try:
            if not self._enabled or env_disabled():
                return AttachmentHandle("disabled")
            state = self._runtime_state()
            if state != "ready":
                return AttachmentHandle(state)
            if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return AttachmentHandle("policy_denied")
            server = self._session.policy
            server = dataclasses.replace(server, allowed_sources=server.allowed_sources | {"Bytes"})
            reason = self.policy.evaluate(server, capture or CapturePolicy(), direction=direction, mime_type=mime,
                                          source_kind="Bytes", purpose=purpose)
            return AttachmentHandle(reason or fallback)
        except Exception:
            return AttachmentHandle(fallback)

    @staticmethod
    def _fields(fields: dict) -> dict:
        result = {}
        for key, allowed in (("role", REFERENCE_ROLES),):
            value = fields.get(key)
            if value is not None:
                if value not in allowed:
                    raise ValueError("invalid attachment classification")
                result[key] = value
        for key in ("message_index", "part_index", "channel"):
            value = fields.get(key)
            if value is not None:
                if type(value) is not int or not 0 <= value <= (31 if key == "channel" else 65535):
                    raise ValueError("invalid attachment index")
                result[key] = value
        for key in ("conversation_id", "turn_id", "tool_call_id"):
            value = fields.get(key)
            if value is not None:
                if not isinstance(value, str) or not TOKEN.fullmatch(value):
                    raise ValueError("invalid opaque correlation")
                result[key] = value
        return result

    def _upload_request(self, correlation, *, attachment_id, occurrence_id, mime_type, direction, source_kind,
                        purpose, declared_bytes, fields=None, recording_id=None, segment_index=None) -> dict:
        request = {
            "schema_version": 1, "attachment_id": attachment_id, "occurrence_id": occurrence_id,
            "association": {"capture_session_id": self.capture_session_id,
                            "correlation_nonce": self.correlation_nonce,
                            "parent": {"trace_id": correlation.trace_uuid, "span_id": correlation.span_id}},
            "policy_version": self._session.policy.version, "deletion_generation": 0,
            "mime_type": mime_type, "modality": MIME_MODALITIES[mime_type], "direction": direction,
            "source_kind": source_kind, "declared_bytes": declared_bytes,
        }
        if purpose is not None:
            request["purpose"] = purpose
        for key in ("message_index", "part_index", "conversation_id", "turn_id", "tool_call_id"):
            if fields and key in fields:
                request[key] = fields[key]
        if recording_id is not None:
            request["recording_id"], request["segment_index"] = recording_id, segment_index
        # Section 7: role and channel never travel in an UploadRequest.
        return request

    def _commit_attach(self, ticket, source, fields) -> AttachmentHandle:
        mime, direction, operation, source_kind, purpose, capture = ticket._spec
        reason = self._recheck_ticket(ticket)
        if reason:
            ticket._drop()
            return AttachmentHandle(reason)
        kind = type(source).__name__
        if kind != source_kind or type(source) not in (Bytes, OwnedRecordingFile):
            ticket._drop()
            return AttachmentHandle("source_unavailable")
        fields = self._fields(fields)
        if type(source) is Bytes:
            data = source.data
            if type(data) not in (bytes, bytearray, memoryview):
                ticket._drop()
                return AttachmentHandle("source_unavailable")
            length = data.nbytes if type(data) is memoryview else len(data)
            if length <= 0 or length > ticket.max_bytes:
                ticket._drop()
                return AttachmentHandle("too_large")
            if type(data) is not bytes:
                # A mutable buffer is copied inside this call, only up to sync_copy_bytes.
                if length > self.config.sync_copy_bytes:
                    ticket._drop()
                    return AttachmentHandle("too_large")
                data = memoryview(data).cast("B").tobytes()
            ticket._reservation.shrink(length)
            payload = data
        else:
            length = source.size
            if length <= 0 or length > ticket.max_bytes:
                ticket._drop()
                return AttachmentHandle("too_large")
            payload = source
        handle = AttachmentHandle("queued")
        request = self._upload_request(ticket.correlation, attachment_id=handle.attachment_id,
                                       occurrence_id=handle.occurrence_id, mime_type=mime, direction=direction,
                                       source_kind=source_kind, purpose=purpose, declared_bytes=length,
                                       fields=fields)
        ref = {"attachment_id": handle.attachment_id, "occurrence_id": handle.occurrence_id,
               "direction": direction, "modality": MIME_MODALITIES[mime], "mime_type": mime}
        if purpose is not None:
            ref["purpose"] = purpose
        ref.update({k: v for k, v in fields.items() if k in ("message_index", "part_index", "role", "channel")})
        return self._enqueue_upload(ticket, handle, request, payload, ref)

    def _enqueue_upload(self, ticket, handle, request, payload, ref) -> AttachmentHandle:
        reservation = ticket._reservation
        job = _Job("upload", handle=handle, body=self._p.encode(request), payload=payload, spec=ticket._spec,
                   epoch=self._epoch, nbytes=reservation.nbytes, descriptors=1, occurrence_id=handle.occurrence_id)
        span = ticket._span
        with self._cond:
            if self._stopped or self._stopping:
                reservation.release()
                ticket._finalizer.detach()
                handle._finish("incomplete")
                return handle
            # The job now owns the ticket's descriptor and bytes.
            reservation.released = True
            ticket._finalizer.detach()
            self._tickets.discard(ticket)
            self._ready_q.append(job)
            self._queued += 1
            self._ensure_workers()
            self._cond.notify_all()
        self._project(span, ref)
        return handle

    # ---------------------------------------------------------- projection
    def _project(self, span, ref: Optional[dict]) -> None:
        """Carrier stamp (0.8): all four attributes, only on a live capture target."""
        try:
            if span is None or not span.is_recording():
                return
            attrs = getattr(span, "attributes", None) or {}
            raw = attrs.get("fi.attachment.refs", "[]")
            if not isinstance(raw, str) or len(raw.encode()) > 16384:
                return
            references = json.loads(raw)
            if not isinstance(references, list):
                return
            if ref is not None:
                if len(references) >= 64:
                    ref = None
                else:
                    references = [*references, ref]
            encoded = json.dumps(references, separators=(",", ":"))
            if len(encoded.encode()) > 16384:
                if ref is None:
                    return
                encoded = raw
            span.set_attribute("fi.attachment.schema_version", 1)
            span.set_attribute("fi.attachment.refs", encoded)
            span.set_attribute("fi.attachment.capture_session_id", self.capture_session_id)
            span.set_attribute("fi.attachment.correlation_nonce", self.correlation_nonce)
        except Exception:
            pass

    # -------------------------------------------------------------- streams
    def start_attachment_stream(self, target=None, options: Optional[StreamOptions] = None) -> AttachmentStream:
        try:
            ticket = self.preflight(target, mime_type=options.mime_type, direction=options.direction,
                                    max_bytes=options.max_bytes, operation="observe_consumed", source_kind="Bytes",
                                    purpose=options.purpose, capture_policy=options.capture_policy)
            return ticket.start_stream(options)
        except Exception:
            return AttachmentStream.denied("source_unavailable")

    def _commit_stream(self, ticket, options: StreamOptions) -> AttachmentStream:
        reason = self._recheck_ticket(ticket)
        if reason:
            ticket._drop()
            return AttachmentStream.denied(reason)
        self._fields(dict(message_index=options.message_index, part_index=options.part_index, role=options.role,
                          channel=options.channel, conversation_id=options.conversation_id,
                          turn_id=options.turn_id, tool_call_id=options.tool_call_id))
        ticket._attachment_id, ticket._occurrence_id = str(uuid.uuid4()), str(uuid.uuid4())
        self._tickets.discard(ticket)
        ticket._finalizer.detach()
        return AttachmentStream(self, ticket, options)

    def _commit_stream_finish(self, ticket, options: StreamOptions, data: bytes) -> AttachmentHandle:
        mime, direction, operation, source_kind, purpose, capture = ticket._spec
        reason = self._recheck_ticket(ticket)
        if reason:
            ticket._drop()
            return AttachmentHandle(reason)
        fields = self._fields(dict(message_index=options.message_index, part_index=options.part_index,
                                   role=options.role, channel=options.channel,
                                   conversation_id=options.conversation_id, turn_id=options.turn_id,
                                   tool_call_id=options.tool_call_id))
        ticket._reservation.shrink(len(data))
        handle = AttachmentHandle("queued", ticket._attachment_id, ticket._occurrence_id)
        request = self._upload_request(ticket.correlation, attachment_id=handle.attachment_id,
                                       occurrence_id=handle.occurrence_id, mime_type=mime, direction=direction,
                                       source_kind="Bytes", purpose=purpose, declared_bytes=len(data), fields=fields)
        ref = {"attachment_id": handle.attachment_id, "occurrence_id": handle.occurrence_id,
               "direction": direction, "modality": MIME_MODALITIES[mime], "mime_type": mime}
        if purpose is not None:
            ref["purpose"] = purpose
        ref.update({k: v for k, v in fields.items() if k in ("message_index", "part_index", "role", "channel")})
        return self._enqueue_upload(ticket, handle, request, data, ref)

    # ------------------------------------------------------------ recording
    def _recording_denials(self, options: RecordingOptions):
        session = self._session
        if session is None or self._runtime_state() != "ready":
            return None
        capture = options.capture_policy or CapturePolicy()
        directions = ([t.direction for t in options.tracks] if options.layout == "separate_tracks" else ["mixed"])
        result = []
        for direction in directions:
            result.append(self.policy.evaluate(session.policy, capture, direction=direction, mime_type="audio/wav",
                                               source_kind="Bytes", operation=options.operation,
                                               purpose=options.purpose))
        return result

    def start_recording(self, target=None, options: Optional[RecordingOptions] = None) -> RecordingWriter:
        try:
            self._sweep()
            if not self._enabled or env_disabled():
                return RecordingWriter(reason="disabled")
            invalid = validate_options(options)
            if invalid:
                return RecordingWriter(reason=invalid)
            if self._stopped or self._stopping:
                return RecordingWriter(reason="incomplete")
            state = self._runtime_state()
            pending = state == "bootstrap_pending"
            if pending:
                self._start_bootstrap()
            elif state != "ready":
                return RecordingWriter(reason=state)
            if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
                return RecordingWriter(reason="policy_denied")
            denials = None
            if not pending:
                denials = self._recording_denials(options)
                if all(denials):
                    return RecordingWriter(reason=next(d for d in denials if d))
            span = trace.get_current_span() if target is None else target
            try:
                correlation = CorrelationHandle.from_span(span)
            except Exception:
                return RecordingWriter(reason="correlation_unavailable")
            if not correlation.sampled:
                return RecordingWriter(reason="unsampled")
            slots = min(self._limits().active_recordings, 4)
            with self._cond:
                if self._descriptors >= self.config.max_descriptors or len(self._recordings) >= slots:
                    return RecordingWriter(reason="queue_full")
                self._descriptors += 1
            writer = RecordingWriter(self, options, "queued", correlation=correlation, pending=pending,
                                     track_denials=denials)
            if writer.reason != "queued":
                self._release(0, 1)
                return writer
            with self._cond:
                self._recordings.add(writer)
            if not isinstance(span, CorrelationHandle) and hasattr(span, "is_recording"):
                self._project(span, None)
            return writer
        except Exception:
            return RecordingWriter(reason="source_unavailable")

    def _close_recording(self, writer) -> None:
        with self._cond:
            if writer in self._recordings:
                self._recordings.discard(writer)
                self._descriptors = max(0, self._descriptors - 1)
            if hasattr(writer, "_lane"):
                self._lanes.discard(writer._lane)
            self._cond.notify_all()

    def _new_lane(self, writer) -> _Lane:
        lane = _Lane(writer)
        with self._cond:
            self._lanes.add(lane)
        return lane

    def _schedule_lane(self, lane: _Lane) -> None:
        # Caller holds self._cond.
        if lane.jobs and not lane.busy and not lane.scheduled:
            lane.scheduled = True
            self._ready_q.append(lane)
            self._ensure_workers()
            self._cond.notify_all()

    def _enqueue_segment(self, writer, entry) -> None:
        options = writer._options
        spec = ("audio/wav", self._segment_direction(writer, entry), options.operation, "Bytes", options.purpose,
                options.capture_policy or CapturePolicy())
        job = _Job("segment", lane=writer._lane, entry=entry, nbytes=entry.nbytes, descriptors=1, spec=spec,
                   epoch=self._epoch, occurrence_id=entry.occurrence_id)
        with self._cond:
            self._descriptors += 1
            writer._lane.jobs.append(job)
            self._queued += 1
            self._schedule_lane(writer._lane)

    def _enqueue_manifest(self, writer, body: bytes, revision: int, end: int, completed: int) -> None:
        job = _Job("manifest", lane=writer._lane, body=body, descriptors=0,
                   extra={"revision": revision, "end": end, "completed": completed})
        with self._cond:
            writer._lane.jobs.append(job)
            self._queued += 1
            self._schedule_lane(writer._lane)

    def _drain_lane(self, lane: _Lane, deadline: float) -> None:
        with self._cond:
            while (lane.jobs or lane.busy) and time.monotonic() < deadline and not self._stopped:
                self._cond.wait(min(WAIT_SLICE, max(deadline - time.monotonic(), 0.001)))

    def _cancel_lane(self, lane: _Lane, reason: str) -> None:
        with self._cond:
            dropped = list(lane.jobs)
            lane.jobs.clear()
            for job in dropped:
                self._queued -= 1
                job.cancelled = True
            active = self._active_jobs.get(id(lane))
            if active is not None:
                active.cancelled = True
            self._cond.notify_all()
        for job in dropped:
            self._finish_job(job, reason, notify_lane=False)

    def _bounded_deadline(self, deadline: float) -> float:
        bound = self._shutdown_deadline
        return min(deadline, bound) if bound is not None else deadline

    # ---------------------------------------------------------- owned files
    def attach_owned_recording(self, target, source, *, mime_type: str, direction: str, wav_tracks=None,
                               purpose: str = "conversation_recording", capture_policy=None,
                               conversation_id: Optional[str] = None) -> OwnedRecordingHandle:
        try:
            if not self._enabled or env_disabled():
                return OwnedRecordingHandle("denied", "disabled")
            if type(source) is not OwnedRecordingFile:
                return OwnedRecordingHandle("denied", "source_unavailable")
            if purpose not in ("conversation_recording", "turn_recording"):
                return OwnedRecordingHandle("denied", "policy_denied")
            single_cap = min(self._limits().max_upload_bytes, 50 * MiB)
            wav = mime_type == "audio/wav"
            if wav and (source.size > single_cap or wav_tracks):
                tier = "wav_recording"
            elif source.size <= single_cap:
                tier = "single"
            else:
                return OwnedRecordingHandle("denied", "too_large")
            if tier == "single":
                ticket = self.preflight(target, mime_type=mime_type, direction=direction, max_bytes=source.size,
                                        operation="import_existing", source_kind="OwnedRecordingFile",
                                        purpose=purpose, capture_policy=capture_policy)
                if not ticket.admitted:
                    return OwnedRecordingHandle("denied", ticket.reason)
                handle = ticket.attach(source, conversation_id=conversation_id)
                return OwnedRecordingHandle("single" if handle.admission == "queued" else "denied",
                                            handle.admission, handle=handle, manager=self)
            if wav_tracks is not None:
                # Roles are claimed only for a validated 2-channel stereo file (section 7);
                # one track describes a mono file. Anything else is refused up front.
                tracks = tuple(wav_tracks)
                layout = "stereo" if len(tracks) == 2 else "mixed"
                if len(tracks) == 1:
                    tracks = (TrackSpec((0,), "mixed", tracks[0].role if isinstance(tracks[0], TrackSpec) else "x"),)
                options = RecordingOptions(format=PcmFormat(8000), layout=layout, tracks=tracks,
                                           operation="observe_consumed")
                if len(tracks) not in (1, 2) or validate_options(options):
                    return OwnedRecordingHandle("denied", "policy_denied")
            if direction not in ("input", "output", "mixed"):
                return OwnedRecordingHandle("denied", "policy_denied")
            # Every wav_recording segment goes on the wire as ``mixed``: a stereo file
            # carries both sides in every byte, and its segments carry no track_id
            # (design 2.4, 2.6 and 7). Admission and every per-chunk re-check must
            # therefore evaluate ``mixed`` too, so a hide flag on EITHER side denies the
            # whole file. The caller's label is kept only for the unparseable-header
            # fallback to tier ``single``, whose wire direction is "as given".
            ticket = self.preflight(target, mime_type=mime_type, direction="mixed", max_bytes=source.size,
                                    operation="import_existing", source_kind="OwnedRecordingFile", purpose=purpose,
                                    capture_policy=capture_policy, _skip_cap=True)
            if not ticket.admitted:
                return OwnedRecordingHandle("denied", ticket.reason)
            reason = self._recheck_ticket(ticket)
            if reason:
                ticket._drop()
                return OwnedRecordingHandle("denied", reason)
            recording_id = str(uuid.uuid4())
            handle = OwnedRecordingHandle("wav_recording", "queued", recording_id=recording_id, manager=self)
            reservation = ticket._reservation
            job = _Job("owned_wav", handle=handle, payload=source, spec=ticket._spec, epoch=self._epoch,
                       nbytes=reservation.nbytes, descriptors=1,
                       extra={"tracks": wav_tracks, "recording_id": recording_id, "correlation": ticket.correlation,
                              "conversation_id": conversation_id, "caller_direction": direction})
            handle._job = job
            with self._cond:
                if self._stopped or self._stopping:
                    ticket._drop()
                    return OwnedRecordingHandle("denied", "incomplete")
                reservation.released = True
                ticket._finalizer.detach()
                self._tickets.discard(ticket)
                self._ready_q.append(job)
                self._queued += 1
                self._ensure_workers()
                self._cond.notify_all()
            self._project(ticket._span, None)
            return handle
        except Exception:
            return OwnedRecordingHandle("denied", "source_unavailable")

    # -------------------------------------------------------------- imports
    async def import_completed_recording(self, target, request: CompletedRecordingRequest, *, resolver,
                                         capture_policy: Optional[CapturePolicy] = None,
                                         timeout: float = 30.0) -> ImportHandle:
        try:
            if not self._enabled or env_disabled():
                return ImportHandle("disabled")
            if (not isinstance(request, CompletedRecordingRequest) or not isinstance(request.provider, str)
                    or not TOKEN.fullmatch(request.provider) or not isinstance(request.provider_ref, str)
                    or not TOKEN.fullmatch(request.provider_ref) or request.direction not in ("input", "output", "mixed")):
                return ImportHandle("policy_denied")
            capture = capture_policy or CapturePolicy()
            ticket = self.preflight(target, mime_type="audio/ogg", direction=request.direction, max_bytes=1,
                                    operation="import_existing", source_kind="ProviderFile", capture_policy=capture,
                                    _import_source=True, _skip_cap=True)
            if not ticket.admitted:
                return ImportHandle(ticket.reason)
            span = ticket._span
            self._project(span, None)
            budget = min(max(float(timeout), 0.0), 30.0)
            try:
                resolved = await asyncio.wait_for(resolver.resolve(request, deadline=time.monotonic() + budget),
                                                  budget)
            except Exception:
                ticket.release()
                return ImportHandle("source_unavailable")
            if isinstance(resolved, str):
                ticket.release()
                return ImportHandle(resolved if resolved in ("source_unavailable", "policy_denied",
                                                             "credential_scope_unsupported", "too_large")
                                    else "source_unavailable")
            reason, body = self._import_body(ticket, request, resolved)
            if reason:
                ticket.release()
                return ImportHandle(reason)
            recording = json.loads(body)["recording"]
            recording_id = recording["recording_id"]
            handle = ImportHandle("queued", recording_id, self)
            reservation = ticket._reservation
            spec = (recording["mime_type"], recording["direction"], "import_existing", recording["source_kind"],
                    None, capture)
            job = _Job("import", handle=handle, body=body, spec=spec, epoch=self._epoch, nbytes=0,
                       descriptors=1, extra={"recording_id": recording_id})
            with self._cond:
                if self._stopped or self._stopping:
                    ticket.release()
                    return ImportHandle("incomplete")
                reservation.released = True
                ticket._finalizer.detach()
                self._tickets.discard(ticket)
                self._ready_q.append(job)
                self._queued += 1
                self._ensure_workers()
                self._cond.notify_all()
            return handle
        except Exception:
            return ImportHandle("source_unavailable")

    def _import_body(self, ticket, request, resolved):
        if not isinstance(resolved, ResolvedCompletedRecording):
            return "unsupported_version", None
        source = resolved.source
        if isinstance(source, ProviderFileSource):
            if not canonical_uuid(source.connection_id) or not canonical_uuid(source.registered_output_id):
                return "unsupported_version", None
            wire, kind = {"kind": "ProviderFile", "connection_id": source.connection_id,
                          "registered_output_id": source.registered_output_id}, "ProviderFile"
        elif isinstance(source, EnvelopeSource):
            if not canonical_uuid(source.envelope_id):
                return "unsupported_version", None
            wire, kind = {"kind": "RemoteUrlEnvelope", "envelope_id": source.envelope_id}, "RemoteUrl"
        else:
            return "unsupported_version", None
        limits = self._limits()
        if (type(resolved.declared_bytes) is not int or type(resolved.declared_duration_ms) is not int
                or not 1 <= resolved.declared_bytes <= min(limits.recording_bytes, 1073741824)
                or not 1 <= resolved.declared_duration_ms <= min(limits.recording_duration_ms, 7200000)):
            return "too_large", None
        if resolved.mime_type not in AUDIO_MIMES:
            return "unsupported_version", None
        direction = resolved.direction
        if direction not in ("input", "output", "mixed"):
            return "unsupported_version", None
        if request.direction == "mixed" and direction != "mixed":
            return "policy_denied", None          # trusted ancestry wins; never narrowed
        if request.direction != direction and direction != "mixed":
            return "policy_denied", None
        _, _, operation, _, purpose, capture = ticket._spec
        reason = self._admission_reason(capture, mime_type=resolved.mime_type, direction=direction,
                                        source_kind=kind, operation="import_existing", purpose=purpose,
                                        import_source=True)
        if reason:
            return reason, None
        correlation = ticket.correlation
        recording = {
            "schema_version": 1,
            "association": {"capture_session_id": self.capture_session_id,
                            "correlation_nonce": self.correlation_nonce,
                            "parent": {"trace_id": correlation.trace_uuid, "span_id": correlation.span_id}},
            "policy_version": self._session.policy.version, "deletion_generation": 0,
            "mime_type": resolved.mime_type, "direction": direction, "source_kind": kind,
            "recording_id": str(uuid.uuid4()), "modality": "audio", "declared_bytes": resolved.declared_bytes,
            "declared_duration_ms": resolved.declared_duration_ms,
        }
        body = {"mode": "completed_recording", "recording": recording, "source": wire}
        if not self._p.wire_valid("RecordingImportRequest", body):
            return "unsupported_version", None
        return None, self._p.encode(body)

    def _import_status(self, job_id: str, timeout: float):
        try:
            session = self._session
            if session is None:
                return ("failed", "expired")
            return self._transport.import_status(session, job_id, deadline=time.monotonic() + min(max(timeout, 0), 30))
        except TransferError as error:
            return ("failed", error.reason)
        except Exception:
            return ("failed", "incomplete")

    # --------------------------------------------------------------- worker
    def _sleep(self, delay: float) -> None:
        deadline = time.monotonic() + delay
        with self._cond:
            while time.monotonic() < deadline:
                if self._stopped:
                    raise TransferError("incomplete")
                self._cond.wait(min(WAIT_SLICE, max(deadline - time.monotonic(), 0.001)))

    def _attempts(self, operation, check, budget: float):
        """At most 3 attempts (0.5): back off within the remaining budget; never retry security failures."""
        limit = time.monotonic() + budget
        for attempt in range(self._p.ATTEMPTS):
            check()
            try:
                return operation()
            except TransferError as error:
                if not error.retryable or attempt == self._p.ATTEMPTS - 1:
                    raise
                delay = self._p.backoff(attempt + 1, error)
                if time.monotonic() + delay > limit:
                    raise TransferError("throttled" if error.reason == "throttled" else error.reason) from None
                self._sleep(delay)

    def _check(self, job: _Job) -> None:
        if self._stopped or job.cancelled:
            raise TransferError("incomplete")
        denied = job.extra.get("denied")
        if denied:
            raise TransferError(denied)
        session = self._session
        if session is None:
            raise TransferError("expired")
        if job.upload is None and session.expiring():
            raise TransferError("expired")
        # Re-evaluated before every chunk and attempt: env hide flags and the
        # negotiated policy can tighten at any time (deny-wins, never cached).
        if job.spec is not None:
            mime, direction, operation, source_kind, purpose, capture = job.spec
            reason = self.policy.evaluate(session.policy, capture, direction=direction, mime_type=mime,
                                          source_kind=source_kind, operation=operation, purpose=purpose,
                                          import_source=job.kind == "import")
            if reason:
                raise TransferError(reason)
        job.epoch = self._epoch

    def _worker(self) -> None:
        while True:
            self._maybe_renew()
            with self._cond:
                while not self._ready_q and not self._stopped:
                    self._cond.wait(WAIT_SLICE)
                if self._stopped:
                    return
                unit = self._ready_q.popleft()
                if isinstance(unit, _Lane):
                    unit.scheduled = False
                    if not unit.jobs:
                        continue
                    job = unit.jobs.popleft()
                    unit.busy = True
                    self._active_jobs[id(unit)] = job
                else:
                    job = unit
                    if job.handle is not None:
                        self._running[id(job.handle)] = job
                if job.kind != "renew":
                    self._queued -= 1
                    self._active += 1
                    self._inflight.add(job)
            status = "incomplete"
            try:
                status = self._run(job)
            except TransferError as error:
                status = error.reason
            except Exception:
                status = "incomplete"
            finally:
                with self._cond:
                    if job.kind != "renew":
                        self._active -= 1
                        self._inflight.discard(job)
                    if job.handle is not None:
                        self._running.pop(id(job.handle), None)
                self._finish_job(job, status)

    def _finish_job(self, job: _Job, status: str, notify_lane: bool = True) -> None:
        """Release budget, settle handles and writer callbacks outside the manager lock."""
        with self._cond:
            if job.kind in ("upload", "segment", "import", "owned_wav") and not job.extra.get("released"):
                job.extra["released"] = True
                self._bytes = max(0, self._bytes - job.nbytes)
                self._descriptors = max(0, self._descriptors - job.descriptors)
                if status not in ("validating", "importing"):
                    self._dropped += 1
            job.payload = None
            self._cond.notify_all()
        if job.kind == "segment":
            job.lane.writer._segment_done(job.entry, status, job.upload if status == "validating" else None,
                                          job.body, job.initialized, job.index)
        elif job.kind == "manifest":
            if not job.extra.get("reported"):
                job.extra["reported"] = True
                job.lane.writer._manifest_done(status == "validating", job.extra["revision"], job.extra["end"],
                                               job.extra["completed"], fallback=job.extra.get("fallback", False))
        elif job.handle is not None:
            if job.kind == "import":
                job.handle._finish(status, job.extra.get("job_id"))
            else:
                job.handle._finish(status)
        job.upload = None
        if notify_lane and job.lane is not None:
            # The lane stays busy until the writer has recorded this result, so the
            # next segment reads the right index and finish() never sees a gap.
            with self._cond:
                job.lane.busy = False
                self._active_jobs.pop(id(job.lane), None)
                self._schedule_lane(job.lane)
                self._cond.notify_all()

    def _ctl(self) -> float:
        return time.monotonic() + self.config.request_timeout

    def _content_deadline(self) -> float:
        limit = self._limits().transfer_timeout_seconds
        return time.monotonic() + min(self.config.content_timeout, limit)

    def _run(self, job: _Job) -> str:
        if job.kind == "renew":
            self._renew_session()
            return "validating"
        if job.kind == "upload":
            return self._run_upload(job, job.payload)
        if job.kind == "segment":
            return self._run_segment(job)
        if job.kind == "manifest":
            return self._run_manifest(job)
        if job.kind == "import":
            return self._run_import(job)
        if job.kind == "owned_wav":
            return self._run_owned_wav(job)
        return "incomplete"

    def _content_source(self, payload):
        if type(payload) is OwnedRecordingFile:
            return payload._open()
        if isinstance(payload, (bytes, bytearray, memoryview)):
            return MemorySource(payload)
        return payload

    def _renew_upload(self, job: _Job, session) -> None:
        if job.renewals >= 3:
            raise TransferError("expired")
        job.renewals += 1
        job.upload = self._transport.renew_upload(session, job.upload, job.body, deadline=self._ctl())

    def _with_capability(self, job: _Job, session, operation, check, budget: float):
        """Renew before use when expiring (a), and once after a 401 (b)."""
        def attempt():
            if job.upload.expiring():
                self._renew_upload(job, session)
            try:
                return operation()
            except TransferError as error:
                if error.http_status != 401:
                    raise
                self._renew_upload(job, session)
                try:
                    return operation()
                except TransferError as second:
                    if second.http_status == 401:
                        raise TransferError("credential_scope_unsupported") from None
                    raise
        return self._attempts(attempt, check, budget)

    def _abort_upload(self, job: _Job, session) -> None:
        if job.upload is None or session is None:
            return
        try:
            self._transport.abort(session, job.upload, key=self._p.idempotency_key("abort", job.upload.upload_id),
                                  deadline=self._ctl())
        except Exception:
            pass

    def _transfer(self, job: _Job, session, source, check, before_complete=None) -> str:
        p = self._p
        body = job.body
        job.upload = self._attempts(lambda: self._transport.initialize(
            session, body, key=p.idempotency_key("upload", job.occurrence_id), deadline=self._ctl(), check=check),
            check, self.config.request_timeout * 3)
        job.initialized = True
        content_budget = self.config.content_timeout
        self._with_capability(job, session, lambda: self._transport.put_content(
            session, job.upload, source, deadline=self._content_deadline(), check=check, pacer=self._pacer,
            chunk_bytes=self.config.chunk_bytes), check, content_budget * 3)
        if before_complete is not None:
            before_complete()
        return self._with_capability(job, session, lambda: self._transport.complete(
            session, job.upload, key=p.idempotency_key("complete", job.upload.upload_id), deadline=self._ctl(),
            check=check), check, self.config.request_timeout * 3)

    def _run_upload(self, job: _Job, payload) -> str:
        source = None
        session = self._session
        try:
            check = lambda: self._check(job)
            check()
            source = self._content_source(payload)
            check()
            return self._transfer(job, session, source, check)
        except TransferError as error:
            if job.upload is not None:
                self._abort_upload(job, session)
            raise error
        except Exception:
            if job.upload is not None:
                self._abort_upload(job, session)
            raise
        finally:
            if source is not None and hasattr(source, "close"):
                try:
                    source.close()
                except Exception:
                    pass

    def _run_segment(self, job: _Job) -> str:
        writer = job.lane.writer
        entry = job.entry
        session = self._session
        check = lambda: self._check(job)
        try:
            check()
            if writer._closing_partial:
                raise TransferError("incomplete")
            index = writer._next_index()
            job.index = index
            request = self._upload_request(writer._correlation, attachment_id=entry.attachment_id,
                                           occurrence_id=entry.occurrence_id, mime_type="audio/wav",
                                           direction=self._segment_direction(writer, entry),
                                           source_kind="Bytes", purpose=writer._options.purpose,
                                           declared_bytes=len(entry.source),
                                           fields={k: v for k, v in (("conversation_id", writer._options.conversation_id),
                                                                     ("turn_id", writer._options.turn_id)) if v},
                                           recording_id=writer.recording_id, segment_index=index)
            job.body = self._p.encode(request)
            before = None
            if writer._fallback:
                before = lambda: self._fallback_manifest(writer, job, session, check)
            return self._transfer(job, session, entry.source, check, before)
        except TransferError:
            if job.upload is not None:
                self._abort_upload(job, session)
            raise

    @staticmethod
    def _segment_direction(writer, entry) -> str:
        if entry.track_id is None:
            return "mixed"
        for track in writer._tracks:
            if track["track_id"] == entry.track_id:
                return track["direction"]
        return "mixed"

    def _fallback_manifest(self, writer, job, session, check) -> None:
        """BR-7 fallback: init -> content -> manifest PUT (including this segment) -> complete."""
        with writer._lock:
            entry = job.entry
            entry.state, entry.index, entry.upload, entry.body = "completed", job.index, job.upload, job.body
            writer._completed += 1
            end = writer._settled_end()
            body = writer._manifest_body(end, final=False)
            writer._completed -= 1
            entry.state, entry.upload = "pending", None
            writer._revision += 1
            revision = writer._revision
        if body is None:
            return
        try:
            self._put_with_entry(writer, session, job, body, revision, check)
            writer._manifest_done(True, revision, end, writer._completed + 1)
        except TransferError:
            with writer._lock:
                writer._closing_partial = True
            raise TransferError("incomplete") from None

    def _put_with_entry(self, writer, session, job, body, revision, check) -> None:
        p = self._p
        self._with_capability(job, session, lambda: self._transport.put_manifest(
            session, job.upload.capability, writer.recording_id, body,
            key=p.idempotency_key("manifest", writer.recording_id, revision),
            deadline=time.monotonic() + self.config.manifest_timeout, check=check), check,
            self.config.manifest_timeout * 3)

    def _run_manifest(self, job: _Job) -> str:
        writer = job.lane.writer
        session = self._session
        candidates = writer._capability_entries()
        if not candidates:
            return "incomplete"
        ok = self._put_manifest_with(writer, session, job.body, job.extra["revision"], candidates,
                                     time.monotonic() + self.config.manifest_timeout * 3, lambda: self._check(job))
        if ok is None:
            job.extra["fallback"] = True
            return "incomplete"
        return "validating" if ok else "incomplete"

    def _put_manifest_with(self, writer, session, body, revision, candidates, deadline, check):
        """Latest completed segment's capability first (3.3); renew it; fall back to older ones.

        Returns True on success, False on failure, None when BR-7's fallback order applies.
        """
        p = self._p
        key = p.idempotency_key("manifest", writer.recording_id, revision)
        rejected = False
        for entry in reversed(candidates):
            holder = _Job("manifest", body=entry.body)
            holder.upload = entry.upload
            attempts = [0]

            def put():
                attempts[0] += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransferError("incomplete")
                try:
                    self._transport.put_manifest(session, holder.upload.capability, writer.recording_id, body,
                                                 key=key, deadline=time.monotonic() + min(
                                                     self.config.manifest_timeout, remaining), check=check)
                except TransferError as error:
                    # BR-5: a replayed rev whose stored body is identical replays as success.
                    if error.reason == "conflict" and error.http_status == 409 and attempts[0] > 1:
                        return None
                    raise
            try:
                if holder.upload.expiring():
                    self._renew_upload(holder, session)
                self._with_capability(holder, session, put, check, max(deadline - time.monotonic(), 0.001))
                entry.upload = holder.upload
                return True
            except TransferError as error:
                entry.upload = holder.upload
                if error.reason == "expired":
                    continue
                if error.http_status in (401, 403, 409) or error.reason in ("credential_scope_unsupported",
                                                                           "policy_denied", "conflict"):
                    rejected = True
                return None if rejected else False
        return False

    def _put_manifest_now(self, writer, body: bytes, revision: int, candidates, deadline: float) -> bool:
        session = self._session
        if session is None:
            return False

        def check():
            if time.monotonic() > deadline:
                raise TransferError("incomplete")
        try:
            return bool(self._put_manifest_with(writer, session, body, revision, candidates, deadline, check))
        except TransferError:
            return False
        except Exception:
            return False

    def _run_import(self, job: _Job) -> str:
        p = self._p
        session = self._session
        check = lambda: self._check(job)
        recording_id = job.extra["recording_id"]
        result = self._attempts(lambda: self._transport.submit_import(
            session, job.body, key=p.idempotency_key("import", recording_id), deadline=self._ctl(), check=check),
            check, self.config.request_timeout * 3)
        job.extra["job_id"] = result.job_id
        return "importing"

    def _run_owned_wav(self, job: _Job) -> str:
        """Tier ``wav_recording``: segments synthesized from lease ranges; never materialized."""
        p = self._p
        session = self._session
        check = lambda: self._check(job)
        extra = job.extra
        lease = None
        try:
            check()
            lease = job.payload._open()
            header = lease.read(0, min(4096, len(lease)))
            info = parse_wav(header, len(lease))
            limits = self._limits()
            single_cap = min(limits.max_upload_bytes, 50 * MiB)
            if info is None:
                lease.close()
                lease = None
                if job.payload.size <= single_cap:
                    job.handle.tier = "single"
                    return self._owned_single(job)
                return "too_large"
            duration_ms = -(-info.frames * 1000 // info.rate)
            if info.data_bytes > min(limits.recording_bytes, 1073741824) or duration_ms > min(
                    limits.recording_duration_ms, 7200000):
                return "too_large"
            tracks_spec = extra["tracks"]
            if info.channels == 1:
                layout, tracks = "mixed", [{"track_id": str(uuid.uuid4()), "channels": [0], "direction": "mixed",
                                            "role": tracks_spec[0].role if tracks_spec and len(tracks_spec) == 1
                                            else "unknown"}]
            elif info.channels == 2 and tracks_spec and len(tracks_spec) == 2:
                layout = "stereo"
                tracks = [{"track_id": str(uuid.uuid4()), "channels": list(t.channels), "direction": t.direction,
                           "role": t.role} for t in tracks_spec]
            else:
                layout, tracks = "mixed", [{"track_id": str(uuid.uuid4()), "channels": list(range(info.channels)),
                                            "direction": "mixed", "role": "unknown"}]
            seg_ms = min(limits.segment_duration_ms, MAX_SEGMENT_MS)
            seg_bytes = min(limits.segment_bytes, limits.max_upload_bytes, MAX_SEGMENT_BYTES)
            frames_per = segment_frames(info.rate, info.channels * 2, seg_ms, seg_bytes)
            if not frames_per:
                return "too_large"
            segments, gaps, completed = [], [], []
            start = 0
            while start < info.frames:
                count = min(frames_per, info.frames - start)
                end = start + count
                final = end == info.frames
                start_ms = to_ms(start, info.rate)
                duration = to_ms(end, info.rate, final) - start_ms
                source = WavSegmentSource(lease, info, start, count)
                part = _Job("segment", spec=job.spec, epoch=job.epoch, occurrence_id=str(uuid.uuid4()))
                part.cancelled = job.cancelled
                index = len(completed)
                request = self._upload_request(
                    extra["correlation"], attachment_id=str(uuid.uuid4()), occurrence_id=part.occurrence_id,
                    mime_type="audio/wav", direction="mixed",
                    source_kind="OwnedRecordingFile", purpose=job.spec[4], declared_bytes=len(source),
                    fields={"conversation_id": extra["conversation_id"]} if extra["conversation_id"] else None,
                    recording_id=extra["recording_id"], segment_index=index)
                part.body = p.encode(request)
                try:
                    self._transfer(part, session, source, lambda: (self._check(job), self._check(part)))
                    entry = type("E", (), {})()
                    entry.upload, entry.body = part.upload, part.body
                    completed.append(entry)
                    segments.append({"occurrence_id": part.occurrence_id, "index": index, "start_ms": start_ms,
                                     "duration_ms": duration})
                except TransferError as error:
                    if part.upload is not None:
                        self._abort_upload(part, session)
                    if error.reason == "source_unavailable" or job.cancelled or self._stopped:
                        gaps.append({"start_ms": start_ms, "duration_ms": to_ms(info.frames, info.rate, True) - start_ms,
                                     "reason": "incomplete"})
                        break
                    gaps.append({"start_ms": start_ms, "duration_ms": duration, "reason": "incomplete"})
                start = end
            if not completed:
                return "incomplete"
            manifest = build_manifest(extra["recording_id"], info.rate, layout, tracks, segments, gaps, [],
                                      duration_ms, True)
            body = p.encode(manifest)
            fake_writer = type("W", (), {"recording_id": extra["recording_id"]})()
            ok = self._put_manifest_with(fake_writer, session, body, 1, completed,
                                         time.monotonic() + self.config.manifest_timeout * 3, check)
            if ok and not gaps:
                return "validating"
            return "incomplete"
        finally:
            if lease is not None:
                lease.close()

    def _owned_single(self, job: _Job) -> str:
        extra = job.extra
        request = self._upload_request(
            extra["correlation"], attachment_id=str(uuid.uuid4()), occurrence_id=str(uuid.uuid4()),
            mime_type="audio/wav", direction=extra["caller_direction"], source_kind="OwnedRecordingFile",
            purpose=job.spec[4],
            declared_bytes=job.payload.size,
            fields={"conversation_id": extra["conversation_id"]} if extra["conversation_id"] else None)
        single = _Job("upload", body=self._p.encode(request), spec=job.spec, epoch=job.epoch,
                      occurrence_id=request["occurrence_id"])
        single.cancelled = job.cancelled
        return self._run_upload(single, job.payload)

    def _renew_session(self) -> None:
        p = self._p
        try:
            session = self._session
            if session is None:
                return
            with self._cond:
                self._renew_sequence += 1
                sequence = self._renew_sequence
            remaining = max(session.expires_at - time.time() - p.SKEW_SECONDS, 0.001)
            renewed = self._attempts(lambda: self._transport.renew_session(
                session, sequence=sequence, deadline=self._ctl()), self._alive,
                min(remaining, self.config.request_timeout * 3))
            tighter = session.policy.tighter_than(renewed.policy)
            with self._cond:
                self._session = renewed
            if tighter:
                self.update_policy(renewed.policy)
        except TransferError as error:
            if error.reason == "scope_unresolved":
                self._invalidate("scope_unresolved")
        except Exception:
            pass
        finally:
            with self._cond:
                self._renew_pending = False
                # A failed renewal is retried at the next activity, at most once per second.
                self._renew_after = time.monotonic() + 1.0

    def _invalidate(self, reason: str) -> None:
        with self._cond:
            self._state = reason
            self._session = None
            self._epoch += 1
            dropped = [unit for unit in self._ready_q if isinstance(unit, _Job) and unit.kind != "renew"]
            self._ready_q = deque(unit for unit in self._ready_q if not (isinstance(unit, _Job) and unit.kind != "renew"))
            self._queued -= len(dropped)
            self._cond.notify_all()
        for job in dropped:
            self._finish_job(job, reason)

    # ------------------------------------------------------- policy/flush
    def update_policy(self, server: ServerPolicy) -> None:
        """Re-evaluate admitted work. Tightening discards; loosening affects only new admissions.

        Only the control-plane owner should call this with a negotiated policy.
        In-flight I/O re-checks before the next bounded chunk; bytes already
        sent cannot be recalled locally.
        """
        if not self._enabled:
            return
        dropped = []
        with self._cond:
            self._epoch += 1
            if self._session is not None:
                old = self._session
                self._session = dataclasses.replace(old, policy=server)
            kept = deque()
            for unit in self._ready_q:
                if isinstance(unit, _Job) and unit.spec is not None and unit.kind != "renew":
                    mime, direction, operation, source_kind, purpose, capture = unit.spec
                    reason = self.policy.evaluate(server, capture, direction=direction, mime_type=mime,
                                                  source_kind=source_kind, operation=operation, purpose=purpose,
                                                  import_source=unit.kind == "import")
                    if reason:
                        dropped.append((unit, reason))
                        self._queued -= 1
                        continue
                    unit.epoch = self._epoch
                kept.append(unit)
            self._ready_q = kept
            # Nothing is resurrected: in-flight and lane work denied by ANY
            # tightening stays denied even if a later policy loosens again.
            pending = list(self._inflight) + [job for lane in self._lanes for job in lane.jobs]
            for job in pending:
                if job.spec is not None and job.kind != "renew":
                    mime, direction, operation, source_kind, purpose, capture = job.spec
                    reason = self.policy.evaluate(server, capture, direction=direction, mime_type=mime,
                                                  source_kind=source_kind, operation=operation, purpose=purpose,
                                                  import_source=job.kind == "import")
                else:
                    reason = None
                if reason and not job.extra.get("denied"):
                    job.extra["denied"] = reason
            self._cond.notify_all()
        for job, reason in dropped:
            self._finish_job(job, reason)
        for ticket in list(self._tickets):
            if ticket.admitted and not ticket._used:
                reason = self._recheck_ticket(ticket)
                if reason:
                    ticket._revoke(reason)

    def _lane_denial(self, writer, server) -> Optional[str]:
        options = writer._options
        capture = options.capture_policy or CapturePolicy()
        directions = ([t.direction for t in options.tracks] if options.layout == "separate_tracks" else ["mixed"])
        reasons = [self.policy.evaluate(server, capture, direction=d, mime_type="audio/wav", source_kind="Bytes",
                                        operation=options.operation, purpose=options.purpose) for d in directions]
        return reasons[0] if all(reasons) else None

    def cancel(self, handle) -> None:
        """Drop a queued job, or make a running one stop at its next chunk boundary."""
        target = None
        with self._cond:
            for unit in list(self._ready_q):
                if isinstance(unit, _Job) and unit.handle is handle:
                    self._ready_q.remove(unit)
                    self._queued -= 1
                    target = unit
                    break
            running = self._running.get(id(handle))
            if running is not None:
                running.cancelled = True
            self._cond.notify_all()
        if target is not None:
            target.cancelled = True
            self._finish_job(target, "incomplete")

    def _unfinished(self) -> int:
        return (self._queued + self._active + sum(1 for s in self._streams if s.open) + len(self._recordings))

    def flush(self, timeout: float = 5) -> FlushResult:
        if self._p is None:
            return FlushResult(0, self._dropped)
        self._sweep()
        deadline = time.monotonic() + max(0.0, min(float(timeout), 30.0))
        with self._cond:
            while (self._queued or self._active) and time.monotonic() < deadline:
                self._cond.wait(min(WAIT_SLICE, max(deadline - time.monotonic(), 0.001)))
            return FlushResult(self._unfinished(), self._dropped)

    async def flush_async(self, timeout: float = 5) -> FlushResult:
        return await asyncio.to_thread(self.flush, timeout)

    def shutdown(self, timeout: Optional[float] = None) -> FlushResult:
        """Bounded and idempotent: returns within ``timeout + 250 ms``."""
        budget = self.config.shutdown_timeout if timeout is None else max(0.0, min(float(timeout), 30.0))
        start = time.monotonic()
        deadline = start + budget
        with self._cond:
            if self._stopping or self._stopped:
                first = False
            else:
                first = True
                self._stopping = True
                self._shutdown_deadline = deadline
                self._cond.notify_all()
        if not first:
            self._shutdown_done.wait(max(0.0, min(budget, 30.0)))
            with self._cond:
                return self._shutdown_result or FlushResult(self._unfinished(), self._dropped)
        try:
            for writer in list(self._recordings):
                remaining = deadline - time.monotonic()
                try:
                    writer.finish(max(remaining, 0.0))
                except Exception:
                    pass
            for stream in list(self._streams):
                stream.abort("incomplete")
            for ticket in list(self._tickets):
                ticket.release()
            self.flush(max(deadline - time.monotonic(), 0.0))
            dropped = []
            with self._cond:
                self._stopped = True
                self._state = "incomplete"
                while self._ready_q:
                    unit = self._ready_q.popleft()
                    if isinstance(unit, _Lane):
                        dropped.extend(unit.jobs)
                        self._queued -= len(unit.jobs)
                        unit.jobs.clear()
                    elif unit.kind != "renew":
                        dropped.append(unit)
                        self._queued -= 1
                for job in self._active_jobs.values():
                    job.cancelled = True
                self._cond.notify_all()
            for job in dropped:
                job.cancelled = True
                self._finish_job(job, "incomplete", notify_lane=False)
            current = threading.current_thread()
            for worker in list(self._threads) + ([self._bootstrap_thread] if self._bootstrap_thread else []):
                if worker is not current:
                    worker.join(max(0.0, deadline - time.monotonic()))
            with self._cond:
                self._shutdown_result = FlushResult(self._unfinished(), self._dropped)
                return self._shutdown_result
        finally:
            self._shutdown_done.set()


class AttachmentSpanProcessor(SpanProcessor):
    """Provider wiring only: flush/shutdown. No span is stamped at start (0.8)."""

    def __init__(self, manager):
        self.manager = manager

    def on_start(self, span, parent_context=None):
        pass

    def on_end(self, span):
        pass

    def force_flush(self, timeout_millis: int = 5000) -> bool:
        return self.manager.flush(min(timeout_millis / 1000, 5)).unfinished == 0

    def shutdown(self):
        # The FI provider already gave force_flush its 5-second deadline.
        self.manager.shutdown(0)
