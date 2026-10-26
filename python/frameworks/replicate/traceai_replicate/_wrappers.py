"""wrapt wrappers for the replicate client.

Span model (TH-8320):

* ``replicate.run`` - ``Client.run`` / ``async_run``. The nested create and the
  client's own poll loop run inside it; they add no span.
* ``replicate.stream`` - ``Client.stream`` / ``async_stream``. The client's
  stream is a lazy generator, so the span starts at the first ``next()``
  (in the context of the ``stream()`` call); a stream never iterated sends no
  request and has no span. It is open until the stream completes (its
  ``done`` event, or the end of iteration), is closed before that, is
  dropped, or is still open at a provider shutdown or interpreter exit (then
  it ends as cancelled, with the output received so far); a ``close()`` or a
  drop after ``done`` is not a cancellation. The span is ended by the process
  that started it (a stream
  first iterated in a forked child is the child's). The
  SSE stream carries no status or metrics. By default the span ends at
  ``done`` with what the stream gave (no status, ``predict_time`` or token
  counts). With ``stream_reload_seconds`` > 0 a completed stream's prediction
  is read once more (``GET /v1/predictions/{id}``: free, no new prediction)
  for them, off the caller's path; see :mod:`traceai_replicate._stream_read`.
* ``replicate.predictions.create`` - every create route. When the returned
  prediction is not terminal yet, the span stays open on that prediction so a
  later ``wait`` / ``cancel`` ends it (one span, not two). If neither is
  called, it ends with the create-time status and the create-time end
  timestamp at the first traced call after the prediction object is
  released or after it has been held for ``max_pending_seconds`` (default
  600), when the tracer provider is flushed or shut down, at
  ``uninstrument()``, or at interpreter exit. A ``wait`` / ``cancel`` that
  continues the span claims it: while that call runs, neither the cap, a
  flush nor a release ends it; only a provider shutdown or interpreter exit
  does, as a best effort (see :meth:`PendingRegistry.claim`).
* ``replicate.prediction.wait`` / ``replicate.predictions.cancel`` - wait or
  cancel on a prediction that has no open create span.

A call the caller stops (Ctrl-C, a signal handler's ``sys.exit()``, a
cancelled asyncio task or trio scope) ends as cancelled
(``replicate.cancelled``), wherever that lands. A call still running when the
provider shuts down or the interpreter exits (for example under the
SIGTERM/SIGINT handler ``register()`` installs) is ended then, as cancelled
and with what it knows, so it is exported before the processors stop.

A call made while the caller has suppressed instrumentation
(``_SUPPRESS_INSTRUMENTATION_KEY``) passes through untraced.

Instrumentation code is isolated: a failure while building attributes is
logged at DEBUG and never replaces the vendor's result or exception.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import copy
import logging
import os
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, TypeVar

import wrapt
from fi_instrumentation import REDACTED_VALUE, TraceConfig
from fi_instrumentation.instrumentation.pii_redaction import redact_pii_in_string
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Status, StatusCode

from traceai_replicate._attributes import (
    CANCELLED,
    INPUT_MIME_TYPE,
    INPUT_VALUE,
    PREDICTION_STATUS,
    PROVIDER,
    REPLICATE,
    REQUEST_MODEL,
    REQUEST_PARAMETERS,
    SPAN_KIND,
    TERMINAL_STATUSES,
    OutputCollector,
    api_tokens,
    is_async_iterator,
    is_sync_iterator,
    output_attributes,
    prediction_attributes,
    redact,
    replicate_client,
    request_attributes,
    span_kind,
)
from traceai_replicate._stream_read import (
    StreamReads,
    complete_stream,
    settle_stream_reads,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

RUN = "replicate.run"
STREAM = "replicate.stream"
CREATE = "replicate.predictions.create"
WAIT = "replicate.prediction.wait"
CANCEL = "replicate.predictions.cancel"

# The longest a create span is held open waiting for wait()/cancel().
DEFAULT_MAX_PENDING_SECONDS = 600.0
_monotonic = time.monotonic
_SUPPRESS_INSTRUMENTATION_KEY = getattr(
    context_api, "_SUPPRESS_INSTRUMENTATION_KEY", "suppress_instrumentation"
)

# The traced call whose vendor code is running in this context. A wrapped
# method reached from inside it (run -> predictions.create -> Prediction.wait,
# Prediction.cancel -> Predictions.cancel) runs untraced, so one user call is
# one span; a nested create reports its prediction to the outer call.
_ACTIVE: "contextvars.ContextVar[Optional[_Call]]" = contextvars.ContextVar(
    "traceai_replicate_active", default=None
)
# Content and parameters are written when the span ends, after the client has
# built its httpx client, so a token the SDK read from the environment is
# known and can be redacted.
_DEFERRED_KEYS = (INPUT_VALUE, INPUT_MIME_TYPE, REQUEST_PARAMETERS)
_UNSET = object()
_T = TypeVar("_T")


def _caller_suppressed() -> bool:
    """The caller suppressed instrumentation (FITracer would start no span)."""
    try:
        return bool(context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY))
    except Exception:
        return False


def _guard(function: Callable[[], _T], default: _T) -> _T:
    try:
        return function()
    except Exception:
        logger.debug("traceai-replicate: instrumentation step failed", exc_info=True)
        return default


def _text(error: BaseException) -> str:
    try:
        return str(error)
    except Exception:
        return "<unprintable {0}>".format(type(error).__name__)


def _is_model_error(error: BaseException) -> bool:
    return type(error).__name__ == "ModelError" and type(error).__module__.startswith("replicate")


def _exception_type(error: BaseException) -> str:
    """``module.QualName`` (bare for builtins), as the OpenTelemetry SDK writes it."""
    module = type(error).__module__
    qualname = type(error).__qualname__
    return qualname if not module or module == "builtins" else "{0}.{1}".format(module, qualname)


class _Call:
    """One traced user call. Owns one span and ends it exactly once."""

    def __init__(
        self,
        span: Any,
        config: TraceConfig,
        client: Any,
        request: Dict[str, Any],
        stream: bool,
        begin: Optional[Callable[[], Any]] = None,
    ) -> None:
        # A stream's span starts at its first next() (see begin()); until then
        # this is the invalid span, on which every write is a no-op.
        self.span = span
        self._begin = begin
        self.config = config
        self.client = client
        self.request = request
        self.stream = stream
        self.prediction: Any = None
        self.snapshot: Dict[str, Any] = {}
        self.create_end: Optional[int] = None
        self.on_finish: Optional[Callable[["_Call"], None]] = None
        # What a stream or run iterator has yielded so far (set when the
        # iterator is wrapped): a span that ends before the iteration
        # completes (closed, dropped, cancelled, an exception, a shutdown)
        # keeps that output, as one that completes does.
        self.collector: Optional[OutputCollector] = None
        # Set (under the registry lock) when a wait()/cancel() continues this
        # held create span; from then on that call ends it (see expire()).
        self.claimed = False
        # Set when a stream completed (``done``, or the end of iteration): from
        # then on complete_stream() owns the end of the span, so a close() or a
        # drop that follows is not an abandoned stream.
        self.completed = False
        # Acquired once, by the finish() that ends the span, and never
        # released: a non-blocking acquire is an atomic test-and-set. No
        # finish() ever waits on it, so one run from a signal handler
        # (register()'s shutdown) that interrupted this thread inside another
        # finish() returns instead of deadlocking.
        self._done = threading.Lock()
        # A forked child inherits this object; only the process that started
        # the span ends it (a stream's span starts at its first next(), see begin()).
        self._pid = os.getpid()

    @property
    def finished(self) -> bool:
        return self._done.locked()

    @property
    def started(self) -> bool:
        """The span has started (a stream's starts at its first ``next()``)."""
        return self._begin is None

    def begin(self) -> None:
        """Start a stream's span, once (on its first ``next()``), in this process."""
        begin = self._begin
        if begin is not None:
            self._begin = None
            self._pid = os.getpid()
            span = _guard(begin, None)
            if span is not None:
                self.span = span
                _guard(lambda: _OPEN.add(self), None)

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Make the span current (HTTP client spans nest under it) and mark
        this call active (nested wrapped methods stay untraced)."""
        token = _ACTIVE.set(self)
        try:
            with trace_api.use_span(
                self.span, end_on_exit=False, record_exception=False, set_status_on_exception=False
            ):
                yield
        finally:
            _ACTIVE.reset(token)

    def observe(self, prediction: Any) -> None:
        if self.prediction is None and prediction is not None:
            self.prediction = prediction

    def expire(self) -> None:
        """End a create span that no wait/cancel continued, as of create time.

        A claimed span is not held any more: the wait()/cancel() that claimed
        it ends it with its result, so expiring it (a release drained while
        that call runs) does nothing.
        """
        if not self.claimed:
            self.finish(use_snapshot=True, end_time=self.create_end)

    def interrupt(self) -> None:
        """Shutdown or exit while the wait()/cancel() that claimed this span
        still runs: end it now, as cancelled (as any call still running then
        is, see :func:`end_open_iterators`), with the prediction's last
        polled status and no output. That call ends nothing when it returns
        later."""
        self.finish(cancelled=True)

    def release(self, cancelled: bool) -> None:
        """For ``__del__``: queue the finish; never run span processors here.

        ``cancelled`` is an abandoned stream/iterator, which ends as of now;
        otherwise a held create span, which ends as of create time.
        """
        if not self.finished and not self.completed:
            _RELEASED.append((self, cancelled, time.time_ns()))

    def finish(
        self,
        *,
        prediction: Any = None,
        output: Any = _UNSET,
        collector: Optional[OutputCollector] = None,
        error: Optional[BaseException] = None,
        cancelled: bool = False,
        use_snapshot: bool = False,
        end_time: Optional[int] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not self._done.acquire(blocking=False):
            return  # ended, or being ended, by another finish()
        if self._pid != os.getpid():
            return  # inherited by a forked child: the parent process ends it
        _OPEN.discard(self)
        try:
            self._write(prediction, output, collector, error, cancelled, use_snapshot, extra)
        except Exception:
            logger.debug("traceai-replicate: could not complete the span", exc_info=True)
        finally:
            # Also when a KeyboardInterrupt lands while the attributes are
            # written: this finish() owns the end, and no later one ends it.
            try:
                self.span.end(end_time=end_time)
            except Exception:
                logger.debug("traceai-replicate: could not end the span", exc_info=True)
            self.collector = None  # the output is on the span now
            callback = self.on_finish
            if callback is not None:
                _guard(lambda: callback(self), None)

    # -- attribute writing -----------------------------------------------------------

    def _details(self, prediction: Any, use_snapshot: bool) -> Dict[str, Any]:
        if use_snapshot:
            return dict(self.snapshot)
        source = prediction if prediction is not None else self.prediction
        return prediction_attributes(source) if source is not None else {}

    def _output(
        self, prediction: Any, output: Any, collector: Optional[OutputCollector], use_snapshot: bool
    ) -> Dict[str, Any]:
        if collector is not None:
            return collector.attributes()
        if output is not _UNSET:
            return output_attributes(output)
        if prediction is not None and not use_snapshot:
            return output_attributes(getattr(prediction, "output", None))
        return {}

    def _write(
        self,
        prediction: Any,
        output: Any,
        collector: Optional[OutputCollector],
        error: Optional[BaseException],
        cancelled: bool,
        use_snapshot: bool,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> None:
        tokens = _guard(lambda: api_tokens(self.client), ())
        details = _guard(lambda: self._details(prediction, use_snapshot), {})
        if self.stream and details.get(PREDICTION_STATUS) not in TERMINAL_STATUSES:
            # An SSE stream does not report a status. Without the read after
            # ``done`` the only one known is the create-time status, which
            # would mislead, so the span carries none (unless the stream
            # itself said, in ``extra``: a ``done`` reason ``canceled``).
            details.pop(PREDICTION_STATUS, None)
        if extra:
            details.update(extra)
        if collector is None:
            collector = self.collector
        if (error is not None or cancelled) and collector is None:
            produced: Dict[str, Any] = {}  # a call that stopped: no output of its own
        else:
            # A stream or iterator that stopped before it completed keeps
            # what it had yielded (what the caller received), as the
            # OpenTelemetry GenAI conventions ask for partial streamed output;
            # TraceConfig still hides it.
            produced = _guard(lambda: self._output(prediction, output, collector, use_snapshot), {})

        attributes = dict(self.request)
        for key, value in details.items():
            if key == REQUEST_MODEL and REQUEST_MODEL in attributes:
                continue  # the model the caller asked for wins
            attributes[key] = value
        attributes.update(produced)
        attributes[SPAN_KIND] = span_kind(produced)
        for key, value in attributes.items():
            _guard(lambda: self.span.set_attribute(key, redact(value, tokens)), None)

        status = details.get(PREDICTION_STATUS)
        source = prediction if prediction is not None else self.prediction
        if cancelled:
            self.span.set_attribute(CANCELLED, True)
            self.span.set_status(Status(StatusCode.ERROR, "cancelled"))
        elif error is not None:
            self._record_error(error, source, tokens)
        elif status == "failed":
            self.span.set_status(Status(StatusCode.ERROR, self._failure_text(source, tokens)))
        else:
            self.span.set_status(Status(StatusCode.OK))

    def _clean(self, text: str, tokens: Any) -> str:
        """The API token removed, then PII when ``pii_redaction`` is on.

        FiSpan masks attributes only; the status and the exception event are
        written here.
        """
        text = redact(text, tokens)
        return redact_pii_in_string(text) if self.config.pii_redaction else text

    def _failure_text(self, prediction: Any, tokens: Any) -> str:
        # The error field can carry model output; it follows the output rule:
        # redacted (not dropped) when outputs are hidden (PRD J3.2, AC-05).
        if self.config.hide_outputs:
            return REDACTED_VALUE
        message = getattr(prediction, "error", None)
        return self._clean(message, tokens) if isinstance(message, str) and message else "failed"

    def _record_error(self, error: BaseException, prediction: Any, tokens: Any) -> None:
        content = _is_model_error(error) or (self.stream and type(error) is RuntimeError)
        hide = bool(self.config.hide_outputs and content)
        if getattr(prediction, "status", None) == "failed" or _is_model_error(error):
            failed = prediction if prediction is not None else getattr(error, "prediction", None)
            description = self._failure_text(failed, tokens)
        elif hide:
            description = REDACTED_VALUE
        else:
            message = _text(error)
            name = type(error).__name__
            description = self._clean("{0}: {1}".format(name, message) if message else name, tokens)
        # Not span.record_exception(): it always adds exception.stacktrace, and
        # a Python traceback carries local file paths and the user name, which
        # traceAI never exports. The event keeps the type and the message.
        self.span.add_event(
            "exception",
            attributes={
                "exception.type": _exception_type(error),
                "exception.message": REDACTED_VALUE if hide else self._clean(_text(error), tokens),
                "exception.escaped": "False",
            },
        )
        self.span.set_status(Status(StatusCode.ERROR, description))


# Finishes queued by ``__del__``. The cyclic GC can run ``__del__`` on any
# thread at any allocation, even while that thread holds a lock on the export
# path (an inline SimpleSpanProcessor export holds the HTTP pool's lock), so
# ``__del__`` only appends here (``deque.append`` is atomic) and the span ends
# later, at a safe point: the next traced call, a registry operation, the
# provider's force_flush/shutdown, uninstrument() or exit.
_RELEASED: "collections.deque[Tuple[_Call, bool, int]]" = collections.deque()
# Registries alive in this process; a forked child gets fresh locks and empty
# registries (the spans they held are the parent's to end).
_REGISTRIES: "weakref.WeakSet[PendingRegistry]" = weakref.WeakSet()
# The calls whose span is open and not held by a registry: a traced call
# whose vendor code is still running (run, async_run, a create, a wait or a
# cancel), and the streams and run iterators handed to the caller and not
# finished. One still open at a provider shutdown or interpreter exit (a
# call interrupted by register()'s SIGTERM/SIGINT handler, one running on
# another thread, a referenced iterator neither completed nor dropped) ends
# then, as cancelled (see end_open_iterators).
_OPEN: "weakref.WeakSet[_Call]" = weakref.WeakSet()


def _after_fork_in_child() -> None:
    _RELEASED.clear()
    _OPEN.clear()
    for registry in list(_REGISTRIES):
        _guard(registry._reset_after_fork, None)


def _open_calls() -> List[_Call]:
    """The calls in ``_OPEN``. The weak references are copied with one
    C-level ``list()`` of the underlying set, which no other Python thread
    can interleave with; a garbage-collector callback that removes one
    during the copy makes it retry."""
    for _ in range(10):
        try:
            references = list(_OPEN.data)
            break
        except RuntimeError:
            continue
    else:  # pragma: no cover
        return []
    calls = []
    for reference in references:
        call = reference()
        if call is not None:
            calls.append(call)
    return calls


def end_open_iterators() -> None:
    """Provider shutdown and interpreter exit: end, as cancelled (as a drop
    would), the span of every call that is still running and of every stream
    or run iterator whose iteration has started and is still open, so it is
    exported before the processors stop. A call or iterator ended this way
    keeps what it had (the prediction it polled, the output yielded so far).
    A stream not iterated yet has no span and is left alone; a completed one
    is its read's (see complete_stream). The call or the iteration goes on
    as before when it resumes; its span has ended."""
    drain_released()
    for call in _open_calls():
        if call.started and not call.completed and not call.finished:
            _guard(lambda: call.finish(cancelled=True), None)


def _running(call: _Call) -> None:
    """While the vendor code of a traced call runs, a shutdown or an exit
    ends its span (see end_open_iterators)."""
    if call.started:
        _guard(lambda: _OPEN.add(call), None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def drain_released() -> None:
    """End the spans of traced objects released since the last drain."""
    while True:
        try:
            call, cancelled, released_at = _RELEASED.popleft()
        except IndexError:
            return
        if call.completed:
            continue  # complete_stream() ends it
        if cancelled:
            _guard(lambda: call.finish(cancelled=True, end_time=released_at), None)
        else:
            _guard(call.expire, None)


class PendingRegistry:
    """Create spans still open on a prediction.

    A *held* span waits, by prediction id, for a wait()/cancel(). It is held
    for at most ``max_pending_seconds``. Expiry is lazy: every registry
    operation (and every traced call, through :meth:`touch`) first ends, as
    of create time, the held spans older than that. Held entries are kept in
    the order they were held, so a check stops at the first young one.

    A *claimed* span is one a running wait()/cancel() continues (see
    :meth:`claim`). It is not held: neither the cap nor ``force_flush`` (nor
    a release of the prediction) ends it; that call does, when it returns or
    raises. Only :meth:`end_all` (provider shutdown, interpreter exit) ends
    it earlier.
    """

    def __init__(self, max_pending_seconds: float = DEFAULT_MAX_PENDING_SECONDS) -> None:
        self._calls: Dict[str, Tuple[_Call, float]] = {}
        self._claimed: Dict[_Call, str] = {}
        self._max_age = max_pending_seconds
        # Re-entrant as a safeguard. No span ends while it is held: expiry runs
        # outside it, and __del__ only queues (see _RELEASED).
        self._lock = threading.RLock()
        _REGISTRIES.add(self)

    def _reset_after_fork(self) -> None:
        self._lock = threading.RLock()
        self._calls = {}
        self._claimed = {}

    def add(self, prediction_id: str, call: _Call) -> None:
        call.on_finish = lambda finished: self.discard(prediction_id, finished)
        with self._lock:
            previous = self._calls.pop(prediction_id, None)
            self._calls[prediction_id] = (call, _monotonic())
        if previous is not None and previous[0] is not call:
            previous[0].expire()
        self.touch()

    def claim(self, prediction_id: Any) -> Optional[_Call]:
        """Hand the span held for ``prediction_id`` to the wait()/cancel()
        that continues it, or return None if none is held.

        The entry leaves the held set under the lock, so a cap check or a
        flush on another thread or task cannot end the span while that call
        runs, and a second wait()/cancel() on the same prediction does not
        get it (it opens its own span). The call is still tracked, so
        provider shutdown and interpreter exit end it as a best effort.
        """
        self.touch()
        if not isinstance(prediction_id, str):
            return None
        with self._lock:
            entry = self._calls.pop(prediction_id, None)
            if entry is None:
                return None
            call = entry[0]
            call.claimed = True
            self._claimed[call] = prediction_id
        return call

    def discard(self, prediction_id: str, call: _Call) -> None:
        with self._lock:
            entry = self._calls.get(prediction_id)
            if entry is not None and entry[0] is call:
                del self._calls[prediction_id]
            self._claimed.pop(call, None)

    def touch(self) -> None:
        """End released spans, then the held spans older than the cap."""
        drain_released()
        deadline = _monotonic() - self._max_age
        stale: List[_Call] = []
        with self._lock:
            # A fresh iterator per step: nothing iterates while an entry goes.
            while self._calls:
                prediction_id, (call, held_since) = next(iter(self._calls.items()))
                if held_since > deadline:
                    break
                del self._calls[prediction_id]
                stale.append(call)
        for call in stale:
            call.expire()

    def expire_all(
        self, settle: bool = True, until: Optional[float] = None, outcome: Optional[str] = None
    ) -> bool:
        """End released spans, then (with ``settle``) the spans of completed
        streams whose read is still running, waiting for those reads no later
        than ``until`` and ending the rest as ``outcome`` (see
        :func:`settle_stream_reads`), then every held span (``force_flush``,
        ``uninstrument()``). A claimed span is left to its running call.
        False when ``until`` cut a read short."""
        drain_released()
        if not settle:
            in_time = True
        elif outcome is None:
            in_time = settle_stream_reads(until)
        else:
            in_time = settle_stream_reads(until, outcome)
        with self._lock:
            calls = [call for call, _ in self._calls.values()]
            self._calls.clear()
        for call in calls:
            call.expire()
        return in_time

    def end_all(self, until: Optional[float] = None, outcome: Optional[str] = None) -> bool:
        """Provider shutdown and interpreter exit: :meth:`expire_all`, then
        end the claimed spans whose wait()/cancel() is still running, as of
        now (see :meth:`_Call.interrupt`), so they are not lost when the
        processors stop."""
        in_time = self.expire_all(until=until, outcome=outcome)
        with self._lock:
            running = list(self._claimed)
            self._claimed.clear()
        for call in running:
            _guard(call.interrupt, None)
        return in_time


def _call_of(proxy: Any) -> Optional[_Call]:
    # A proxy whose __init__ failed has no call; __del__ must not raise.
    try:
        return proxy._self_call
    except Exception:
        return None


class PendingPrediction(wrapt.ObjectProxy):  # type: ignore[misc]
    """The Prediction a traced create returned while its span awaits wait/cancel.

    An ObjectProxy, so ``isinstance(p, Prediction)`` and every field and method
    are the vendor's. Copying or pickling it yields a plain Prediction.
    Releasing it queues the end of a span that wait/cancel never continued
    (see ``_RELEASED``).
    """

    def __init__(self, prediction: Any, call: _Call) -> None:
        super().__init__(prediction)
        self._self_call = call

    # Defined on the proxy so that ``create(...).wait()`` keeps the proxy (and
    # with it the open span) alive for the whole call: an attribute looked up
    # through to the vendor object would let a temporary proxy be released,
    # ending the span at create time, before wait() even starts.
    def wait(self, *args: Any, **kwargs: Any) -> Any:
        return self.__wrapped__.wait(*args, **kwargs)

    async def async_wait(self, *args: Any, **kwargs: Any) -> Any:
        return await self.__wrapped__.async_wait(*args, **kwargs)

    def cancel(self, *args: Any, **kwargs: Any) -> Any:
        return self.__wrapped__.cancel(*args, **kwargs)

    async def async_cancel(self, *args: Any, **kwargs: Any) -> Any:
        return await self.__wrapped__.async_cancel(*args, **kwargs)

    def __reduce_ex__(self, protocol: int) -> Any:
        return self.__wrapped__.__reduce_ex__(protocol)

    def __reduce__(self) -> Any:
        return self.__wrapped__.__reduce__()

    def __copy__(self) -> Any:
        return copy.copy(self.__wrapped__)

    def __deepcopy__(self, memo: Dict[int, Any]) -> Any:
        return copy.deepcopy(self.__wrapped__, memo)

    def __del__(self) -> None:
        call = _call_of(self)
        if call is not None:
            _guard(lambda: call.release(cancelled=False), None)


# Raised into the caller's own call or iteration by the caller's side: Ctrl-C
# (KeyboardInterrupt), a signal handler's sys.exit() (SystemExit), a close
# delivered with throw(GeneratorExit), and the cancellation of the asyncio
# task (CancelledError) or trio scope (trio.Cancelled, also what anyio raises
# under trio) running it. Recorded as the caller stopping the call, as when
# Ctrl-C lands in a loop body and the stream is then dropped or still open at
# exit (replicate.cancelled), not as an error of the call.
_CALLER_STOPS = (KeyboardInterrupt, SystemExit, GeneratorExit, asyncio.CancelledError)


def _caller_stopped(error: BaseException) -> bool:
    if isinstance(error, _CALLER_STOPS):
        return True
    trio = sys.modules.get("trio")  # only when the caller runs on trio
    cancelled = getattr(trio, "Cancelled", None) if trio is not None else None
    return isinstance(cancelled, type) and isinstance(error, cancelled)


def _stopped(call: _Call, error: BaseException, **details: Any) -> None:
    """End a call that raised: as cancelled when the caller stopped it,
    otherwise with the error."""
    if _caller_stopped(error):
        call.finish(cancelled=True, **details)
    else:
        call.finish(error=error, **details)


def _busy_elsewhere(iterator: Any, error: BaseException) -> bool:
    """The vendor generator refused a close because another thread (or
    task) is inside its ``next()`` / ``__anext__()`` at that moment; the
    stream goes on there, and so does its span."""
    if isinstance(error, ValueError) and "already executing" in _text(error):
        return True
    if isinstance(error, RuntimeError) and "already running" in _text(error):
        return True
    return bool(_guard(lambda: getattr(iterator, "gi_running", False), False))


def _is_done(item: Any) -> bool:
    """The ``done`` server-sent event that ends a completed replicate stream."""
    event = getattr(item, "event", None)
    return event is not None and hasattr(item, "data") and getattr(event, "value", event) == "done"


class TracedIterator(wrapt.ObjectProxy):  # type: ignore[misc]
    """A stream / run output generator whose span ends with the iteration.

    A stream's span starts at the first ``next()``. Complete (a stream's
    ``done`` event, or the end of iteration): OK, ended by
    :func:`complete_stream` (at once, by default; with the opt-in read, when
    it returns, without holding up the caller); a ``close()``, a
    drop or an exception after ``done`` leaves it so. ``close()`` before that,
    dropping it, or a caller stop inside ``next()`` (``KeyboardInterrupt``,
    ``SystemExit``, ``GeneratorExit`` from ``throw()``; Ctrl-C lands there or
    in the loop body, and both are the caller stopping): ERROR ``cancelled``
    with ``replicate.cancelled`` (a drop is queued, see ``_RELEASED``, and
    keeps the drop time); a stream closed or dropped before its first
    ``next()`` made no request and has no span. Another exception before it:
    ERROR. Either way the span keeps the output yielded so far. The span
    ends exactly once; ``close()`` closes the vendor generator (and its HTTP
    response) first and ends the span after, even if that close raises,
    except when the generator refused the close because another thread is
    inside its ``next()``: the stream goes on there, and so does its span.
    ``send()`` and ``throw()`` step the stream as ``next()`` does.
    """

    def __init__(
        self, iterator: Any, call: _Call, collector: OutputCollector, reads: Optional[StreamReads] = None
    ) -> None:
        super().__init__(iterator)
        self._self_call = call
        self._self_collector = collector
        self._self_reads = reads

    def __iter__(self) -> "TracedIterator":
        return self

    def __next__(self) -> Any:
        return self._step(next, self.__wrapped__)

    def send(self, value: Any) -> Any:
        return self._step(self.__wrapped__.send, value)

    def throw(self, *args: Any) -> Any:
        return self._step(self.__wrapped__.throw, *args)

    def _step(self, advance: Callable[..., Any], *args: Any) -> Any:
        call = self._self_call
        call.begin()
        try:
            with call.activate():
                item = advance(*args)
        except StopIteration:
            complete_stream(call, self._self_collector, self._self_reads)
            raise
        except BaseException as error:
            if not call.completed:
                _stopped(call, error)
            raise
        _guard(lambda: self._self_collector.add(item), None)
        if call.stream and _guard(lambda: _is_done(item), False):
            complete_stream(call, self._self_collector, self._self_reads, done=item)
        return item

    def close(self) -> None:
        call = self._self_call
        try:
            with call.activate():
                self.__wrapped__.close()
        except BaseException as error:
            if not call.completed and not _busy_elsewhere(self.__wrapped__, error):
                call.finish(cancelled=True)
            raise
        if not call.completed:
            call.finish(cancelled=True)

    def __del__(self) -> None:
        call = _call_of(self)
        if call is not None:
            _guard(lambda: call.release(cancelled=True), None)


class TracedAsyncIterator(wrapt.ObjectProxy):  # type: ignore[misc]
    """The async twin of :class:`TracedIterator`; ``aclose()`` ends the span,
    and ``asend()`` / ``athrow()`` step the stream as ``__anext__()`` does."""

    def __init__(
        self, iterator: Any, call: _Call, collector: OutputCollector, reads: Optional[StreamReads] = None
    ) -> None:
        super().__init__(iterator)
        self._self_call = call
        self._self_collector = collector
        self._self_reads = reads

    def __aiter__(self) -> "TracedAsyncIterator":
        return self

    async def __anext__(self) -> Any:
        return await self._step(self.__wrapped__.__anext__)

    async def asend(self, value: Any) -> Any:
        return await self._step(self.__wrapped__.asend, value)

    async def athrow(self, *args: Any) -> Any:
        return await self._step(self.__wrapped__.athrow, *args)

    async def _step(self, advance: Callable[..., Any], *args: Any) -> Any:
        call = self._self_call
        call.begin()
        try:
            with call.activate():
                item = await advance(*args)
        except StopAsyncIteration:
            complete_stream(call, self._self_collector, self._self_reads, is_async=True)
            raise
        except BaseException as error:
            if not call.completed:
                _stopped(call, error)
            raise
        _guard(lambda: self._self_collector.add(item), None)
        if call.stream and _guard(lambda: _is_done(item), False):
            complete_stream(call, self._self_collector, self._self_reads, is_async=True, done=item)
        return item

    async def aclose(self) -> None:
        call = self._self_call
        try:
            with call.activate():
                await self.__wrapped__.aclose()
        except BaseException as error:
            if not call.completed and not _busy_elsewhere(self.__wrapped__, error):
                call.finish(cancelled=True)
            raise
        if not call.completed:
            call.finish(cancelled=True)

    def __del__(self) -> None:
        call = _call_of(self)
        if call is not None:
            _guard(lambda: call.release(cancelled=True), None)


def _finish_or_wrap(
    call: _Call, result: Any, count_files: bool, reads: Optional[StreamReads] = None
) -> Any:
    """End the span now, or hand it to the iterator the client returned."""
    try:
        wrapped: Any = None
        collector = OutputCollector(count_files)
        if is_sync_iterator(result):
            wrapped = TracedIterator(result, call, collector, reads)
        elif is_async_iterator(result):
            wrapped = TracedAsyncIterator(result, call, collector, reads)
        if wrapped is not None:
            call.collector = collector
            if call.started:  # a run() iterator: its span is open already (a stream's opens at next())
                _OPEN.add(call)
            return wrapped
    except Exception:
        logger.debug("traceai-replicate: could not wrap the returned iterator", exc_info=True)
    call.begin()
    call.finish(output=result)
    return result


class _Wrapper:
    def __init__(
        self,
        tracer: Any,
        config: TraceConfig,
        registry: PendingRegistry,
        span_name: str,
        operation: str,
    ) -> None:
        self._tracer = tracer
        self._config = config
        self._registry = registry
        self._span_name = span_name
        self._operation = operation

    def _start(
        self,
        wrapped: Any,
        instance: Any,
        args: tuple,
        kwargs: Mapping[str, Any],
        stream: bool = False,
        lazy: bool = False,
    ) -> Optional[_Call]:
        """A call and its span; with ``lazy`` the span starts at
        :meth:`_Call.begin`, in the context current now."""
        try:
            client = _guard(lambda: replicate_client(instance), None)
            request = _guard(
                lambda: request_attributes(self._operation, wrapped, instance, args, kwargs),
                {PROVIDER: REPLICATE},
            )
            tokens = _guard(lambda: api_tokens(client), ())
            initial = {
                key: redact(value, tokens)
                for key, value in request.items()
                if key not in _DEFERRED_KEYS
            }
            if not lazy:
                span = self._tracer.start_span(self._span_name, attributes=initial)
                return _Call(span, self._config, client, request, stream)
            tracer, name, context = self._tracer, self._span_name, context_api.get_current()

            def begin() -> Any:
                token = context_api.attach(context)
                try:
                    return tracer.start_span(name, attributes=initial)
                finally:
                    context_api.detach(token)

            return _Call(trace_api.INVALID_SPAN, self._config, client, request, stream, begin=begin)
        except Exception:
            logger.debug("traceai-replicate: could not start a span", exc_info=True)
            return None

    def _touch(self) -> None:
        """On entry to a traced call: end held spans that are past the cap."""
        _guard(self._registry.touch, None)

    def _claim(self, prediction_id: Any) -> Optional[_Call]:
        return _guard(lambda: self._registry.claim(prediction_id), None)

    def _created(self, call: _Call, prediction: Any) -> Any:
        """Finish a create span, or keep it open on the returned prediction."""
        try:
            call.observe(prediction)
            if call.finished:
                return prediction  # a shutdown or exit ended it while the create ran
            status = getattr(prediction, "status", None)
            prediction_id = getattr(prediction, "id", None)
            if status in TERMINAL_STATUSES or not isinstance(prediction_id, str) or not prediction_id:
                call.finish(prediction=prediction)
                return prediction
            call.snapshot = _guard(lambda: prediction_attributes(prediction), {})
            call.create_end = time.time_ns()
            self._registry.add(prediction_id, call)
            # Held from here on: the registry ends it (as of create time) at a
            # flush, a shutdown or an exit, not as a call still running.
            _guard(lambda: _OPEN.discard(call), None)
            return PendingPrediction(prediction, call)
        except Exception:
            logger.debug("traceai-replicate: could not hold the create span", exc_info=True)
            call.finish(prediction=prediction)
            return prediction


class RunWrapper(_Wrapper):
    """``Client.run``, and ``Client.stream`` with ``stream=True``."""

    def __init__(self, *args: Any, stream: bool = False, reads: Optional[StreamReads] = None) -> None:
        super().__init__(*args)
        self._stream = stream
        # Only a stream reads its prediction after the end: a run() iterator
        # polls the same Prediction object to the end itself.
        self._reads = reads if stream else None

    def __call__(self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]) -> Any:
        if _ACTIVE.get() is not None or _caller_suppressed():
            return wrapped(*args, **kwargs)
        self._touch()
        call = self._start(wrapped, instance, args, kwargs, stream=self._stream, lazy=self._stream)
        if call is None:
            return wrapped(*args, **kwargs)
        _running(call)  # a stream's call starts at its first next(), and is tracked from there
        try:
            with call.activate():
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            call.begin()
            _stopped(call, error)
            raise
        return _finish_or_wrap(call, result, count_files=self._stream, reads=self._reads)


class AsyncRunWrapper(RunWrapper):
    """``Client.async_run``, and ``Client.async_stream`` with ``stream=True``."""

    async def __call__(  # type: ignore[override]
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]
    ) -> Any:
        if _ACTIVE.get() is not None or _caller_suppressed():
            return await wrapped(*args, **kwargs)
        self._touch()
        call = self._start(wrapped, instance, args, kwargs, stream=self._stream, lazy=self._stream)
        if call is None:
            return await wrapped(*args, **kwargs)
        _running(call)
        try:
            with call.activate():
                result = await wrapped(*args, **kwargs)
        except BaseException as error:
            call.begin()
            _stopped(call, error)
            raise
        return _finish_or_wrap(call, result, count_files=self._stream, reads=self._reads)


class CreateWrapper(_Wrapper):
    """Every prediction create route (version, official model, deployment)."""

    def __call__(self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]) -> Any:
        if _caller_suppressed():
            return wrapped(*args, **kwargs)
        outer = _ACTIVE.get()
        if outer is not None:
            prediction = wrapped(*args, **kwargs)
            _guard(lambda: outer.observe(prediction), None)
            return prediction
        self._touch()
        call = self._start(wrapped, instance, args, kwargs)
        if call is None:
            return wrapped(*args, **kwargs)
        _running(call)
        try:
            with call.activate():
                prediction = wrapped(*args, **kwargs)
        except BaseException as error:
            _stopped(call, error)
            raise
        return self._created(call, prediction)


class AsyncCreateWrapper(_Wrapper):
    async def __call__(
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]
    ) -> Any:
        if _caller_suppressed():
            return await wrapped(*args, **kwargs)
        outer = _ACTIVE.get()
        if outer is not None:
            prediction = await wrapped(*args, **kwargs)
            _guard(lambda: outer.observe(prediction), None)
            return prediction
        self._touch()
        call = self._start(wrapped, instance, args, kwargs)
        if call is None:
            return await wrapped(*args, **kwargs)
        _running(call)
        try:
            with call.activate():
                prediction = await wrapped(*args, **kwargs)
        except BaseException as error:
            _stopped(call, error)
            raise
        return self._created(call, prediction)


class _LifecycleWrapper(_Wrapper):
    """``Prediction.wait`` / ``cancel`` and ``Predictions.cancel(id)``.

    An open create span for the same prediction is claimed, continued and
    ended when the call returns or raises; there is no second span. While
    the call runs, the cap and a flush leave that span alone. Otherwise one
    span covers the call. ``wait`` on a prediction that is already terminal
    makes no request and adds no span.
    """

    def __init__(self, *args: Any, by_id: bool = False, skip_terminal: bool = False) -> None:
        super().__init__(*args)
        self._by_id = by_id
        self._skip_terminal = skip_terminal

    def _prediction_id(self, instance: Any, args: tuple, kwargs: Mapping[str, Any]) -> Any:
        if self._by_id:
            return args[0] if args else kwargs.get("id")
        return getattr(instance, "id", None)

    def _after(self, instance: Any, result: Any) -> Any:
        # Prediction.wait/cancel update the instance; Predictions.cancel returns one.
        return result if self._by_id else instance

    def _untraced(self, instance: Any) -> bool:
        return self._skip_terminal and _guard(
            lambda: getattr(instance, "status", None) in TERMINAL_STATUSES, False
        )


class LifecycleWrapper(_LifecycleWrapper):
    def __call__(self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]) -> Any:
        if _ACTIVE.get() is not None or _caller_suppressed():
            return wrapped(*args, **kwargs)
        self._touch()
        call = self._claim(_guard(lambda: self._prediction_id(instance, args, kwargs), None))
        if call is None:
            if self._untraced(instance):
                return wrapped(*args, **kwargs)
            call = self._start(wrapped, instance, args, kwargs)
            if call is None:
                return wrapped(*args, **kwargs)
        _running(call)
        try:
            with call.activate():
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            _stopped(call, error, prediction=None if self._by_id else instance)
            raise
        call.finish(prediction=self._after(instance, result))
        return result


class AsyncLifecycleWrapper(_LifecycleWrapper):
    async def __call__(
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]
    ) -> Any:
        if _ACTIVE.get() is not None or _caller_suppressed():
            return await wrapped(*args, **kwargs)
        self._touch()
        call = self._claim(_guard(lambda: self._prediction_id(instance, args, kwargs), None))
        if call is None:
            if self._untraced(instance):
                return await wrapped(*args, **kwargs)
            call = self._start(wrapped, instance, args, kwargs)
            if call is None:
                return await wrapped(*args, **kwargs)
        _running(call)
        try:
            with call.activate():
                result = await wrapped(*args, **kwargs)
        except BaseException as error:
            _stopped(call, error, prediction=None if self._by_id else instance)
            raise
        call.finish(prediction=self._after(instance, result))
        return result
