"""Provider-owned, bounded, best-effort background audio transfer runtime."""

import asyncio
import json
import os
import random
import re
import threading
import time
import uuid
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import Mapping, Optional, Union

from opentelemetry import trace
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, get_value
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Span, SpanContext

from fi_instrumentation.instrumentation.config import TraceConfig

from .policy import AttachmentConfig, AttachmentPolicy, CapturePolicy, ServerPolicy
from .protocol import HTTPTransport, Session, TransferError, Upload
from .types import AttachmentHandle, Bytes, CorrelationHandle, FlushResult, LocalFile, ProviderFile, RemoteUrl
from .sources import OwnedRecordingFile


@dataclass(eq=False)
class _Job:
    handle: AttachmentHandle
    payload: object = field(repr=False)
    request: dict
    capture: CapturePolicy
    epoch: int
    budget: int
    cancelled: bool = False
    upload: Optional[Upload] = None


class AttachmentManager:
    """One runtime per provider. No network, source reads, or threads when OFF.

    A transport double can implement bootstrap/initialize/transfer/complete/abort
    for component qualification. It must honor the supplied cancellation check
    before every bounded I/O chunk. This is not a durable spool.
    """

    def __init__(self, *, config: Optional[AttachmentConfig] = None, trace_config: Optional[TraceConfig] = None,
                 project_name: str = "", project_type: str = "observe", headers: Optional[Mapping[str, str]] = None,
                 transport=None):
        self.config = config or AttachmentConfig()
        self.policy = AttachmentPolicy(self.config, trace_config)
        self._project_name, self._project_type = project_name, project_type
        self._transport = transport
        self._initial_status = "disabled"
        if self.config.enabled:
            if not project_name:
                self._initial_status = "scope_unresolved"
            elif transport is None:
                try:
                    self._transport = HTTPTransport(self.config.endpoint or "", headers or {},
                                                    timeout=self.config.request_timeout)
                    self._initial_status = "bootstrap_pending"
                except Exception as error:
                    self._initial_status = error.reason if isinstance(error, TransferError) else "endpoint_unconfigured"
            else:
                self._initial_status = "bootstrap_pending"
        self._reset()
        if hasattr(os, "register_at_fork"):
            ref = weakref.ref(self)
            def child_reset():
                manager = ref()
                if manager is not None:
                    manager._reset()
            os.register_at_fork(after_in_child=child_reset)
        self._start()

    def _reset(self) -> None:
        # Never acquire inherited thread locks after fork. Child captures get a
        # fresh session/nonce; parent descriptors and payloads are not replayed.
        self._condition = threading.Condition()
        self._queue = deque()
        self._jobs = {}
        self._bytes = 0
        self._dropped = 0
        self._epoch = 0
        self._stopped = False
        self._threads = []
        self._session: Optional[Session] = None
        self._ready = threading.Event()
        self._status = self._initial_status
        self.capture_session_id = str(uuid.uuid4())
        self.correlation_nonce = str(uuid.uuid4())

    def _start(self) -> None:
        with self._condition:
            if self._threads or self._stopped or self._status != "bootstrap_pending":
                return
            for target in [self._bootstrap] + [self._worker] * self.config.workers:
                worker = threading.Thread(target=target, daemon=True, name="fi-attachment")
                self._threads.append(worker)
                worker.start()

    def _bootstrap(self) -> None:
        try:
            session = self._retry(lambda: self._transport.bootstrap(
                self._project_name, self._project_type, self.capture_session_id, self.correlation_nonce
            ))
            with self._condition:
                if self._stopped:
                    return
                self._session = session
                self._status = "ready"
        except Exception as error:
            with self._condition:
                self._status = error.reason if isinstance(error, TransferError) else "source_unavailable"
        finally:
            self._ready.set()

    def ready(self, timeout: float = 5) -> bool:
        self._start()
        if self._status == "bootstrap_pending":
            self._ready.wait(max(0, min(timeout, 30)))
        return self._status == "ready" and self._session is not None and self._session.expires_at > time.time()

    async def ready_async(self, timeout: float = 5) -> bool:
        return await asyncio.to_thread(self.ready, timeout)

    @property
    def status(self) -> str:
        if self._session is not None and self._session.expires_at <= time.time():
            return "expired"
        return self._status

    @property
    def owned_bytes(self) -> int:
        with self._condition:
            return self._bytes

    def attach(self, span_or_context: Optional[Union[Span, SpanContext, CorrelationHandle]],
               source: Union[Bytes, OwnedRecordingFile, LocalFile, ProviderFile, RemoteUrl],
               *, mime_type: str, direction: str, purpose: Optional[str] = None,
               conversation_id: Optional[str] = None, turn_id: Optional[str] = None,
               tool_call_id: Optional[str] = None, message_index: Optional[int] = None,
               part_index: Optional[int] = None, role: Optional[str] = None, channel: Optional[int] = None,
               capture_policy: Optional[CapturePolicy] = None) -> AttachmentHandle:
        """Try admission without I/O. Rejections never retain or inspect bytes."""
        try:
            return self._attach(span_or_context, source, mime_type=mime_type, direction=direction,
                                purpose=purpose, conversation_id=conversation_id, turn_id=turn_id,
                                tool_call_id=tool_call_id, message_index=message_index,
                                part_index=part_index, role=role, channel=channel,
                                capture_policy=capture_policy or CapturePolicy())
        except Exception:
            # Caller objects, malformed contexts, and instrumentation errors must
            # not fail inference or reach a logger with their repr/exception.
            return AttachmentHandle("source_unavailable")

    def _attach(self, span_or_context, source, **options) -> AttachmentHandle:
        if self._status == "bootstrap_pending":
            self._start()
        capture = options.pop("capture_policy")
        kind = type(source).__name__
        reason = self.policy.evaluate(self._session.policy if self._session else None, capture,
                                      direction=options["direction"], mime_type=options["mime_type"], source_kind=kind)
        if reason:
            if reason == "bootstrap_pending" and self._status != "bootstrap_pending":
                reason = self._status
            return AttachmentHandle(reason)
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            return AttachmentHandle("policy_denied")
        if self._stopped:
            return AttachmentHandle("incomplete")
        if self._session.expires_at <= time.time():
            return AttachmentHandle("expired")
        # Unqualified sources cannot trigger even stat(), DNS, or a client call.
        if type(source) not in (Bytes, OwnedRecordingFile):
            return AttachmentHandle("source_unavailable")
        span = trace.get_current_span() if span_or_context is None else span_or_context
        try:
            correlation = CorrelationHandle.from_span(span)
        except Exception:
            return AttachmentHandle("correlation_unavailable")
        if not correlation.sampled:
            return AttachmentHandle("unsampled")
        metadata = self._metadata(options)
        owned_file = type(source) is OwnedRecordingFile
        data = source if owned_file else source.data
        if not owned_file and type(data) not in (bytes, bytearray, memoryview):
            return AttachmentHandle("source_unavailable")
        # Export the mutable buffer only after policy admission. A memoryview
        # pins its size, preventing a concurrent resize between budget and copy.
        if type(data) is bytearray:
            data = memoryview(data)
        length = source.size if owned_file else (data.nbytes if type(data) is memoryview else len(data))
        if type(length) is not int or length <= 0 or length > min(self.config.max_attachment_bytes, self._session.max_upload_bytes):
            return AttachmentHandle("too_large")
        # A mutable buffer may only be copied in bounded callback-sized chunks.
        if not owned_file and type(data) is not bytes and length > self.config.chunk_bytes:
            return AttachmentHandle("too_large")
        budget = self.config.chunk_bytes if owned_file else length
        self._condition.acquire()
        try:
            if self._stopped:
                return AttachmentHandle("incomplete")
            reason = self.policy.evaluate(self._session.policy, capture, direction=options["direction"],
                                          mime_type=options["mime_type"], source_kind=kind)
            if reason:
                return AttachmentHandle(reason)
            if len(self._jobs) >= self.config.max_descriptors or self._bytes + budget > self.config.max_owned_bytes:
                return AttachmentHandle("queue_full")
            handle = AttachmentHandle("queued")
            request = {
                "schema_version": 1, "attachment_id": handle.attachment_id,
                "occurrence_id": handle.occurrence_id,
                "association": {"capture_session_id": self.capture_session_id,
                                "correlation_nonce": self.correlation_nonce,
                                "parent": {"trace_id": correlation.trace_uuid, "span_id": correlation.span_id}},
                "policy_version": self._session.policy.version,
                "deletion_generation": self._session.policy.deletion_generation,
                "source_kind": kind, "modality": "audio", "declared_bytes": length,
                **{k: v for k, v in metadata.items() if k not in ("role", "channel")},
            }
            payload = data if owned_file or type(data) is bytes else bytes(data)
            job = _Job(handle, payload, request, capture, self._epoch, budget)
            self._bytes += budget
            self._jobs[handle.occurrence_id] = job
            self._queue.append(job)
            self._project_reference(span, handle, metadata)
            self._condition.notify_all()
            return handle
        finally:
            self._condition.release()

    @staticmethod
    def _metadata(options: dict) -> dict:
        result = {"mime_type": options["mime_type"], "direction": options["direction"]}
        enums = {"purpose": ("conversation_recording", "turn_recording", "model_input", "model_output", "tool_result", "diagnostic"),
                 "role": ("user", "assistant", "tool", "system", "unknown")}
        for key, allowed in enums.items():
            if options[key] is not None:
                if options[key] not in allowed:
                    raise ValueError("invalid attachment classification")
                result[key] = options[key]
        for key in ("message_index", "part_index", "channel"):
            value = options[key]
            if value is not None:
                if type(value) is not int or not 0 <= value <= (31 if key == "channel" else 65535):
                    raise ValueError("invalid attachment index")
                result[key] = value
        for key in ("conversation_id", "turn_id", "tool_call_id"):
            value = options[key]
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", value):
                    raise ValueError("invalid opaque correlation")
                result[key] = value
        return result

    def _project_reference(self, span, handle, metadata) -> None:
        # Only live spans are projected. Authoritative occurrence binding is out
        # of band; upload completion must never mutate an ended span.
        try:
            if not span.is_recording():
                return
            attrs = getattr(span, "attributes", None) or {}
            raw = attrs.get("fi.attachment.refs", "[]")
            if not isinstance(raw, str) or len(raw) > 16384:
                return
            references = json.loads(raw)
            if not isinstance(references, list) or len(references) >= 64:
                return
            ref = {k: v for k, v in metadata.items() if k not in ("conversation_id", "turn_id", "tool_call_id")}
            ref.update(attachment_id=handle.attachment_id, occurrence_id=handle.occurrence_id, modality="audio")
            encoded = json.dumps([*references, ref], separators=(",", ":"))
            if len(encoded.encode()) <= 16384:
                span.set_attribute("fi.attachment.schema_version", 1)
                span.set_attribute("fi.attachment.capture_session_id", self.capture_session_id)
                span.set_attribute("fi.attachment.correlation_nonce", self.correlation_nonce)
                span.set_attribute("fi.attachment.refs", encoded)
        except Exception:
            pass

    def _check(self, job: _Job) -> None:
        if self._stopped or job.cancelled:
            raise TransferError("incomplete")
        if job.epoch != self._epoch:
            raise TransferError("policy_denied")
        session = self._session
        if session is None or session.expires_at <= time.time() or job.upload is not None and job.upload.expires_at <= time.time():
            raise TransferError("expired")
        reason = self.policy.evaluate(session.policy, job.capture, direction=job.request["direction"],
                                      mime_type=job.request["mime_type"], source_kind=job.request["source_kind"])
        if reason:
            raise TransferError(reason)

    def _retry(self, operation, check=lambda: None):
        for attempt in range(3):
            check()
            try:
                return operation()
            except TransferError as error:
                if not error.retryable or attempt == 2:
                    raise
                delay = max(error.retry_after, random.uniform(0.02, 0.08) * (2 ** attempt))
                with self._condition:
                    if self._stopped:
                        raise TransferError("incomplete") from None
                    self._condition.wait(timeout=delay)

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopped:
                    self._condition.wait()
                if self._stopped:
                    return
                job = self._queue.popleft()
            status = "incomplete"
            lease = None
            try:
                check = lambda: self._check(job)
                check()
                session = self._session
                payload = job.payload
                if type(payload) is OwnedRecordingFile:
                    lease = payload._open()
                    payload = lease
                check()
                job.upload = self._retry(lambda: self._transport.initialize(session, job.request, check), check)
                self._retry(lambda: self._transport.transfer(session, job.upload, payload, check, self.config.chunk_bytes), check)
                if lease is not None:
                    lease.check()
                self._retry(lambda: self._transport.complete(session, job.upload, check), check)
                status = "validating"
            except Exception as error:
                status = error.reason if isinstance(error, TransferError) else "incomplete"
                if job.upload is not None:
                    try:
                        self._transport.abort(self._session, job.upload)
                    except Exception:
                        pass
            finally:
                payload = None
                if lease is not None:
                    lease.close()
                with self._condition:
                    self._release(job, status)
                    self._condition.notify_all()

    def _release(self, job: _Job, status: str) -> None:
        if self._jobs.pop(job.handle.occurrence_id, None) is not None:
            self._bytes -= job.budget
            job.payload = b""
            job.request = {}
            job.upload = None
            if status != "validating":
                self._dropped += 1
            job.handle._finish(status)

    def update_policy(self, server: ServerPolicy) -> None:
        """Invalidate all admitted work, including an active transfer/retry.

        Only the control-plane owner should call this with a negotiated policy.
        No queued data survives re-enablement. In-flight I/O checks before the
        next bounded chunk; already sent bytes cannot be recalled locally.
        """
        with self._condition:
            self._epoch += 1
            if self._session is not None:
                old = self._session
                self._session = Session(old.project_id, old.capture_session_id, server, old.expires_at, old.max_upload_bytes)
            while self._queue:
                self._release(self._queue.popleft(), "policy_denied")
            self._condition.notify_all()

    def cancel(self, handle: AttachmentHandle) -> None:
        with self._condition:
            job = self._jobs.get(handle.occurrence_id)
            if job:
                job.cancelled = True
                if job in self._queue:
                    self._queue.remove(job)
                    self._release(job, "incomplete")
            self._condition.notify_all()

    def flush(self, timeout: float = 5) -> FlushResult:
        deadline = time.monotonic() + max(0, min(timeout, 30))
        with self._condition:
            while self._jobs and time.monotonic() < deadline:
                self._condition.wait(timeout=deadline - time.monotonic())
            return FlushResult(len(self._jobs), self._dropped)

    async def flush_async(self, timeout: float = 5) -> FlushResult:
        return await asyncio.to_thread(self.flush, timeout)

    def shutdown(self, timeout: Optional[float] = None) -> FlushResult:
        with self._condition:
            if self._stopped:
                return FlushResult(len(self._jobs), self._dropped)
        deadline = time.monotonic() + (self.config.shutdown_timeout if timeout is None else max(0, min(timeout, 30)))
        self.flush(max(0, deadline - time.monotonic()))
        with self._condition:
            self._stopped = True
            self._status = "incomplete"
            while self._queue:
                self._release(self._queue.popleft(), "incomplete")
            self._condition.notify_all()
        for worker in self._threads:
            if worker is not threading.current_thread():
                worker.join(max(0, deadline - time.monotonic()))
        with self._condition:
            return FlushResult(len(self._jobs), self._dropped)


class AttachmentSpanProcessor(SpanProcessor):
    """Publish the non-secret writer correlation carrier before span export."""

    def __init__(self, manager: AttachmentManager):
        self.manager = manager

    def on_start(self, span, parent_context=None):
        if self.manager.config.enabled and span.is_recording():
            span.set_attribute("fi.attachment.capture_session_id", self.manager.capture_session_id)
            span.set_attribute("fi.attachment.correlation_nonce", self.manager.correlation_nonce)

    def on_end(self, span):
        pass

    def force_flush(self, timeout_millis: int = 5000) -> bool:
        return self.manager.flush(min(timeout_millis / 1000, 5)).unfinished == 0

    def shutdown(self):
        # The FI provider already gave force_flush its 5-second deadline.
        self.manager.shutdown(0)
