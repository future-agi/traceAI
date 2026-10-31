"""One in-flight AI call seen at the HTTP layer, and the span it becomes when it completes."""

import logging
import threading
import time
from typing import Any, Dict, List, Mapping, Optional

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, Status, StatusCode

from fi_instrumentation.auto.wire import _core, _privacy

logger = logging.getLogger(__name__)

# Set while a wire capture is in flight, so a client that wraps another client is captured once.
_WIRE_ACTIVE_KEY = context_api.create_key("fi-wire-capture-active")
# Library-layer spans of these kinds already record the call; the wire layer stays out of the way.
_HTTP_SUPPRESS_KEYS = tuple(
    key
    for key in (
        getattr(context_api, "_SUPPRESS_HTTP_INSTRUMENTATION_KEY", None),
        "suppress_http_instrumentation",
    )
    if key is not None
)


def _skip_current_context() -> bool:
    from fi_instrumentation.instrumentation._tracers import _MODEL_COVERED_KEY, _MODEL_KINDS

    if context_api.get_value(_MODEL_COVERED_KEY):
        return True
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY) or context_api.get_value(_WIRE_ACTIVE_KEY):
        return True
    for key in _HTTP_SUPPRESS_KEYS:
        if context_api.get_value(key):
            return True
    span = trace_api.get_current_span()
    if getattr(span, "_traceai_model_covered", False):
        return True
    if not span.is_recording():
        return False
    # FiSpan keeps gen_ai.span.kind aside until the span ends; plain SDK spans hold it directly.
    kind = (getattr(span, "_self_important_attributes", None) or {}).get("gen_ai.span.kind")
    if kind is None:
        kind = (getattr(span, "attributes", None) or {}).get("gen_ai.span.kind")
    return kind in _MODEL_KINDS


def mark_active() -> object:
    """Attach the in-flight marker; pass the token to ``context_api.detach``."""
    return context_api.attach(context_api.set_value(_WIRE_ACTIVE_KEY, True))


class Recorder:
    """Creates captures for matching requests and turns finished ones into spans."""

    def __init__(self, tracer: Any, max_body_bytes: int = _core.DEFAULT_MAX_BODY_BYTES) -> None:
        from fi_instrumentation._secrets import seed_environment

        seed_environment()
        self.tracer = tracer
        self.max_body_bytes = max_body_bytes
        self.enabled = True

    def observe_credentials(self, headers: Mapping[str, str], url: str = "") -> None:
        if self.enabled:
            _privacy.observe_request(headers, url)

    def begin(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
        fields: Optional[Dict[str, str]] = None,
        files: Optional[List[_core.FilePart]] = None,
        body_size: Optional[int] = None,
    ) -> Optional["Capture"]:
        """Return a capture when this request is AI traffic that should be recorded, else None.

        Never raises: a wire-layer bug must not reach the application.
        """
        started = (time.time_ns(), time.perf_counter())
        try:
            if not self.enabled:
                return None
            self.observe_credentials(headers, url)
            if _skip_current_context():
                return None
            found = _core.match(method, url)
            if found is None:
                return None
            # Parse in-memory multipart without retaining file bytes, even above the body cap.
            multipart = _core._header(dict(headers), "content-type") == "multipart/form-data"
            encoding = _core._header(dict(headers), "content-encoding")
            if multipart and body is not None and fields is None and encoding in ("", "identity"):
                fields, files = _core.parse_multipart(body, _core._raw_header(dict(headers), "content-type"))
                body_size = len(body)
                body = None
            truncated = body is not None and len(body) > self.max_body_bytes
            # Compressed bodies must reach the core before applying the decoded size cap.
            if encoding not in ("", "identity"):
                truncated = False
            request = _core.RequestData(
                method=method,
                url=url,
                headers={str(k): str(v) for k, v in headers.items()},
                body=None if truncated else body,
                fields=fields,
                files=list(files or ()),
                body_size=body_size if body_size is not None else (len(body) if body is not None else None),
                truncated=truncated,
                max_body_bytes=self.max_body_bytes,
            )
            if truncated and _core.is_binary_content_type(request.content_type):
                # Binary uploads (audio) only need their size; keep matching them.
                request.body = b""
                request.truncated = False
            checked = _core.check_request(found, request)
            if checked is None:
                return None
            capture = Capture(self, found, request, checked[0])
            capture._start_ns, capture._start_perf = started
            return capture
        except Exception:
            logger.debug("traceAI wire layer: request inspection failed", exc_info=True)
            return None


class Capture:
    def __init__(self, recorder: Recorder, found: _core.Match, request: _core.RequestData, parsed: _core.Parsed) -> None:
        self._recorder = recorder
        self._tracer = recorder.tracer
        self._max_body_bytes = recorder.max_body_bytes
        self._match = found
        self._request = request
        self._parsed = parsed
        self._parent = context_api.get_current()
        self._start_ns = time.time_ns()
        self._start_perf = time.perf_counter()
        self._response = _core.ResponseData(max_body_bytes=recorder.max_body_bytes)
        self._chunks: List[bytes] = []
        self._buffered = 0
        self._binary = False
        self._done = False
        self._last_byte_ns = None
        self._end_ns = None
        self._lock = threading.Lock()

    def on_headers(self, status: int, headers: Mapping[str, str], decoded: bool = False) -> None:
        try:
            self._response.decoded = decoded
            self._response.status = status
            self._response.headers = {str(k): str(v) for k, v in headers.items()}
            self._binary = _core.is_binary_content_type(self._response.content_type)
        except Exception:
            logger.debug("traceAI wire layer: header capture failed", exc_info=True)

    def on_chunk(self, chunk: Any) -> None:
        if not chunk or self._done:
            return
        observed_ns, observed_perf = time.time_ns(), time.perf_counter()
        self._last_byte_ns = observed_ns
        try:
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            if self._response.first_chunk_seconds is None:
                self._response.first_chunk_seconds = observed_perf - self._start_perf
            self._response.size += len(chunk)
            if self._binary or self._response.truncated:
                return
            if self._buffered + len(chunk) > self._max_body_bytes:
                self._response.truncated = True
                self._chunks.clear()
                return
            self._chunks.append(bytes(chunk))
            self._buffered += len(chunk)
            # Keep arrival evidence off the parsing clock and independently
            # bounded even when a peer fragments a body into one-byte reads.
            if len(self._response.chunk_arrivals) < 16384:
                self._response.chunk_arrivals.append((self._buffered, observed_perf - self._start_perf))
            else:
                self._response.timing_truncated = True
        except Exception:
            logger.debug("traceAI wire layer: chunk capture failed", exc_info=True)

    def fail(self, error: BaseException) -> None:
        if self._done:
            return
        self._end_ns = time.time_ns()
        self._response.error = error
        self.finish()

    def finish(self, incomplete: bool = False) -> None:
        ended = self._end_ns or self._last_byte_ns or time.time_ns()
        with self._lock:
            if self._done:
                return
            self._done = True
            self._end_ns = ended
            # Reaching the declared wire length is EOF even if a consumer closes
            # without asking the iterator for one more chunk. Decoded lengths cannot
            # be compared to a compressed Content-Length.
            length = _core._header(self._response.headers, "content-length")
            encoded = _core._header(self._response.headers, "content-encoding")
            complete_length = (length.isdigit() and self._response.size == int(length)
                               and (not encoded or encoded == "identity" or not self._response.decoded))
            self._response.incomplete = incomplete and not complete_length
        try:
            if not self._binary and not self._response.truncated:
                self._response.body = b"".join(self._chunks)
            self._chunks = []
            result = _core.build(self._match, self._request, self._response, self._parsed)
            if result is not None:
                self._emit(result)
        except Exception:
            logger.debug("traceAI wire layer: span build failed", exc_info=True)

    def _emit(self, result: _core.SpanResult) -> None:
        if not self._recorder.enabled:
            return
        # Start the span inside the request-time context so its parent and the context
        # attributes (session, user, metadata) are the ones active when the call was made.
        from fi_instrumentation.instrumentation.context_attributes import CONTEXT_ATTRIBUTES

        credentials = _privacy.secrets({})
        parent = self._parent
        for key in CONTEXT_ATTRIBUTES:
            value = context_api.get_value(key, parent)
            if value is not None:
                parent = context_api.set_value(key, _privacy.scrub(value, credentials), parent)
        # An in-flight capture keeps its original provider/context, but never its old
        # privacy policy. Protect before end so application exporters see safe data too.
        from fi_instrumentation import TraceConfig

        config = getattr(self._recorder.tracer, "_self_config", None) or TraceConfig()
        result.attributes = _privacy.mask_attributes(
            result.attributes, config, credentials, self._match.endpoint.operation)
        token = context_api.attach(parent)
        try:
            # A7: project before the SDK's bounded container admits anything, reserving
            # the slots the tracer fills afterwards from the request-time context.
            from fi_instrumentation._attribute_limits import project_attributes, span_attribute_limit
            from fi_instrumentation.instrumentation.context_attributes import get_attributes_from_context

            context_keys = {key for key, _ in get_attributes_from_context()} - set(result.attributes)
            result.attributes, omitted = project_attributes(
                result.attributes, span_attribute_limit(self._tracer), self._match.endpoint.operation,
                reserved=len(context_keys))
            span = self._tracer.start_span(
                result.name, kind=SpanKind.CLIENT, start_time=self._start_ns, attributes=result.attributes
            )
        finally:
            context_api.detach(token)
        if omitted:
            # Keep OTel's all-attribute drop count truthful too (content has its own count).
            sdk_attributes = getattr(getattr(span, "__wrapped__", span), "_attributes", None)
            if hasattr(sdk_attributes, "dropped"):
                sdk_attributes.dropped += omitted
        if result.ok:
            span.set_status(Status(StatusCode.OK))
        else:
            span.set_status(Status(StatusCode.ERROR, result.description))
            if self._response.error is not None:
                # Never hand the raw exception/traceback to an exporter: messages and stack
                # locals can contain credentials. The class and sanitized message suffice.
                span.add_event("exception", attributes={
                    "exception.type": type(self._response.error).__name__,
                    "exception.message": result.description or "",
                })
        raw = getattr(span, "__wrapped__", span)
        try:
            _privacy.protect_live_span(raw, config)
        except Exception:
            # Do not leave an unsafe active span for a processor to export later.
            from opentelemetry.sdk.trace import SpanProcessor

            raw._span_processor = SpanProcessor()
            logger.debug("traceAI wire layer: export privacy failed; dropping span", exc_info=True)
        span.end(end_time=self._end_ns)
