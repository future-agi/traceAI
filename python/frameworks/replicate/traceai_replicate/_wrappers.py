"""wrapt wrappers for the replicate client.

Span model (TH-8320):

* ``replicate.run`` - ``Client.run`` / ``async_run``. The nested create and the
  client's own poll loop run inside it; they add no span.
* ``replicate.stream`` - ``Client.stream`` / ``async_stream``, open until the
  stream ends, is closed, or is dropped.
* ``replicate.predictions.create`` - every create route. When the returned
  prediction is not terminal yet, the span stays open on that prediction so a
  later ``wait`` / ``cancel`` ends it (one span, not two). If neither is
  called, it ends with the create-time status and the create-time end
  timestamp at the first traced call after the prediction object is
  released or after it has been held for ``max_pending_seconds`` (default
  600), when the tracer provider is flushed or shut down, at
  ``uninstrument()``, or at interpreter exit.
* ``replicate.prediction.wait`` / ``replicate.predictions.cancel`` - wait or
  cancel on a prediction that has no open create span.

Instrumentation code is isolated: a failure while building attributes is
logged at DEBUG and never replaces the vendor's result or exception.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import copy
import logging
import threading
import time
import traceback
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple, TypeVar

import wrapt
from fi_instrumentation import REDACTED_VALUE, TraceConfig
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


def _stacktrace(error: BaseException, hide: bool) -> str:
    try:
        if hide:
            frames = "".join(traceback.format_tb(error.__traceback__))
            return "Traceback (most recent call last):\n{0}{1}: {2}\n".format(
                frames, type(error).__name__, REDACTED_VALUE
            )
        return "".join(traceback.format_exception(type(error), error, error.__traceback__))
    except Exception:
        return ""


class _Call:
    """One traced user call. Owns one span and ends it exactly once."""

    def __init__(
        self,
        span: Any,
        config: TraceConfig,
        client: Any,
        request: Dict[str, Any],
        stream: bool,
    ) -> None:
        self.span = span
        self.config = config
        self.client = client
        self.request = request
        self.stream = stream
        self.prediction: Any = None
        self.snapshot: Dict[str, Any] = {}
        self.create_end: Optional[int] = None
        self.on_finish: Optional[Callable[["_Call"], None]] = None
        self._finished = False
        self._lock = threading.Lock()

    @property
    def finished(self) -> bool:
        return self._finished

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
        """End a create span that no wait/cancel continued, as of create time."""
        self.finish(use_snapshot=True, end_time=self.create_end)

    def release(self, cancelled: bool) -> None:
        """For ``__del__``: queue the finish; never run span processors here.

        ``cancelled`` is an abandoned stream/iterator, which ends as of now;
        otherwise a held create span, which ends as of create time.
        """
        if not self._finished:
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
    ) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
        try:
            self._write(prediction, output, collector, error, cancelled, use_snapshot)
        except Exception:
            logger.debug("traceai-replicate: could not complete the span", exc_info=True)
        try:
            self.span.end(end_time=end_time)
        except Exception:
            logger.debug("traceai-replicate: could not end the span", exc_info=True)
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
    ) -> None:
        tokens = _guard(lambda: api_tokens(self.client), ())
        details = _guard(lambda: self._details(prediction, use_snapshot), {})
        if self.stream:
            # An SSE stream does not report a terminal status; the create-time
            # status would be misleading, so stream spans carry none.
            details.pop(PREDICTION_STATUS, None)
        produced = {} if error is not None or cancelled else _guard(
            lambda: self._output(prediction, output, collector, use_snapshot), {}
        )

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

    def _failure_text(self, prediction: Any, tokens: Any) -> str:
        # The error field can carry model output; it follows the output rule:
        # redacted (not dropped) when outputs are hidden (PRD J3.2, AC-05).
        if self.config.hide_outputs:
            return REDACTED_VALUE
        message = getattr(prediction, "error", None)
        return redact(message, tokens) if isinstance(message, str) and message else "failed"

    def _record_error(self, error: BaseException, prediction: Any, tokens: Any) -> None:
        content = _is_model_error(error) or (self.stream and type(error) is RuntimeError)
        hide = bool(self.config.hide_outputs and content)
        if getattr(prediction, "status", None) == "failed" or _is_model_error(error):
            failed = prediction if prediction is not None else getattr(error, "prediction", None)
            description = self._failure_text(failed, tokens)
        elif hide:
            description = REDACTED_VALUE
        else:
            description = redact("{0}: {1}".format(type(error).__name__, _text(error)), tokens)
        self.span.record_exception(
            error,
            attributes={
                "exception.message": REDACTED_VALUE if hide else redact(_text(error), tokens),
                "exception.stacktrace": redact(_stacktrace(error, hide), tokens),
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


def drain_released() -> None:
    """End the spans of traced objects released since the last drain."""
    while True:
        try:
            call, cancelled, released_at = _RELEASED.popleft()
        except IndexError:
            return
        if cancelled:
            _guard(lambda: call.finish(cancelled=True, end_time=released_at), None)
        else:
            _guard(call.expire, None)


class PendingRegistry:
    """Create spans still open on a prediction, by prediction id.

    A span is held for at most ``max_pending_seconds``. Expiry is lazy: every
    registry operation (and every traced call, through :meth:`touch`) first
    ends, as of create time, the held spans older than that. Entries are kept
    in the order they were held, so a check stops at the first young one.
    """

    def __init__(self, max_pending_seconds: float = DEFAULT_MAX_PENDING_SECONDS) -> None:
        self._calls: Dict[str, Tuple[_Call, float]] = {}
        self._max_age = max_pending_seconds
        # Re-entrant as a safeguard. No span ends while it is held: expiry runs
        # outside it, and __del__ only queues (see _RELEASED).
        self._lock = threading.RLock()

    def add(self, prediction_id: str, call: _Call) -> None:
        call.on_finish = lambda finished: self.discard(prediction_id, finished)
        with self._lock:
            previous = self._calls.pop(prediction_id, None)
            self._calls[prediction_id] = (call, _monotonic())
        if previous is not None and previous[0] is not call:
            previous[0].expire()
        self.touch()

    def get(self, prediction_id: Any) -> Optional[_Call]:
        self.touch()
        if not isinstance(prediction_id, str):
            return None
        with self._lock:
            entry = self._calls.get(prediction_id)
        return entry[0] if entry is not None else None

    def discard(self, prediction_id: str, call: _Call) -> None:
        with self._lock:
            entry = self._calls.get(prediction_id)
            if entry is not None and entry[0] is call:
                del self._calls[prediction_id]

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

    def expire_all(self) -> None:
        drain_released()
        with self._lock:
            calls = [call for call, _ in self._calls.values()]
            self._calls.clear()
        for call in calls:
            call.expire()


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


class TracedIterator(wrapt.ObjectProxy):  # type: ignore[misc]
    """A stream / run output generator whose span ends with the iteration.

    Completed: OK. ``close()`` before the end, or dropping it: ERROR
    ``cancelled`` with ``replicate.cancelled`` (a drop is queued, see
    ``_RELEASED``, and keeps the drop time). An exception: ERROR. The span
    ends exactly once; ``close()`` closes the vendor generator (and its HTTP
    response) first and ends the span after, even if that close raises.
    """

    def __init__(self, iterator: Any, call: _Call, collector: OutputCollector) -> None:
        super().__init__(iterator)
        self._self_call = call
        self._self_collector = collector

    def __iter__(self) -> "TracedIterator":
        return self

    def __next__(self) -> Any:
        call = self._self_call
        try:
            with call.activate():
                item = next(self.__wrapped__)
        except StopIteration:
            call.finish(collector=self._self_collector)
            raise
        except BaseException as error:
            call.finish(error=error)
            raise
        _guard(lambda: self._self_collector.add(item), None)
        return item

    def close(self) -> None:
        call = self._self_call
        try:
            with call.activate():
                self.__wrapped__.close()
        finally:
            call.finish(cancelled=True)

    def __del__(self) -> None:
        call = _call_of(self)
        if call is not None:
            _guard(lambda: call.release(cancelled=True), None)


class TracedAsyncIterator(wrapt.ObjectProxy):  # type: ignore[misc]
    """The async twin of :class:`TracedIterator`; ``aclose()`` ends the span."""

    def __init__(self, iterator: Any, call: _Call, collector: OutputCollector) -> None:
        super().__init__(iterator)
        self._self_call = call
        self._self_collector = collector

    def __aiter__(self) -> "TracedAsyncIterator":
        return self

    async def __anext__(self) -> Any:
        call = self._self_call
        try:
            with call.activate():
                item = await self.__wrapped__.__anext__()
        except StopAsyncIteration:
            call.finish(collector=self._self_collector)
            raise
        except asyncio.CancelledError:
            call.finish(cancelled=True)
            raise
        except BaseException as error:
            call.finish(error=error)
            raise
        _guard(lambda: self._self_collector.add(item), None)
        return item

    async def aclose(self) -> None:
        call = self._self_call
        try:
            with call.activate():
                await self.__wrapped__.aclose()
        finally:
            call.finish(cancelled=True)

    def __del__(self) -> None:
        call = _call_of(self)
        if call is not None:
            _guard(lambda: call.release(cancelled=True), None)


def _finish_or_wrap(call: _Call, result: Any, count_files: bool) -> Any:
    """End the span now, or hand it to the iterator the client returned."""
    try:
        if is_sync_iterator(result):
            return TracedIterator(result, call, OutputCollector(count_files))
        if is_async_iterator(result):
            return TracedAsyncIterator(result, call, OutputCollector(count_files))
    except Exception:
        logger.debug("traceai-replicate: could not wrap the returned iterator", exc_info=True)
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
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any], stream: bool = False
    ) -> Optional[_Call]:
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
            span = self._tracer.start_span(self._span_name, attributes=initial)
            return _Call(span, self._config, client, request, stream)
        except Exception:
            logger.debug("traceai-replicate: could not start a span", exc_info=True)
            return None

    def _touch(self) -> None:
        """On entry to a traced call: end held spans that are past the cap."""
        _guard(self._registry.touch, None)

    def _pending(self, prediction_id: Any) -> Optional[_Call]:
        return _guard(lambda: self._registry.get(prediction_id), None)

    def _created(self, call: _Call, prediction: Any) -> Any:
        """Finish a create span, or keep it open on the returned prediction."""
        try:
            call.observe(prediction)
            status = getattr(prediction, "status", None)
            prediction_id = getattr(prediction, "id", None)
            if status in TERMINAL_STATUSES or not isinstance(prediction_id, str) or not prediction_id:
                call.finish(prediction=prediction)
                return prediction
            call.snapshot = _guard(lambda: prediction_attributes(prediction), {})
            call.create_end = time.time_ns()
            self._registry.add(prediction_id, call)
            return PendingPrediction(prediction, call)
        except Exception:
            logger.debug("traceai-replicate: could not hold the create span", exc_info=True)
            call.finish(prediction=prediction)
            return prediction


class RunWrapper(_Wrapper):
    """``Client.run``, and ``Client.stream`` with ``stream=True``."""

    def __init__(self, *args: Any, stream: bool = False) -> None:
        super().__init__(*args)
        self._stream = stream

    def __call__(self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]) -> Any:
        if _ACTIVE.get() is not None:
            return wrapped(*args, **kwargs)
        self._touch()
        call = self._start(wrapped, instance, args, kwargs, stream=self._stream)
        if call is None:
            return wrapped(*args, **kwargs)
        try:
            with call.activate():
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            call.finish(error=error)
            raise
        return _finish_or_wrap(call, result, count_files=self._stream)


class AsyncRunWrapper(RunWrapper):
    """``Client.async_run``, and ``Client.async_stream`` with ``stream=True``."""

    async def __call__(  # type: ignore[override]
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]
    ) -> Any:
        if _ACTIVE.get() is not None:
            return await wrapped(*args, **kwargs)
        self._touch()
        call = self._start(wrapped, instance, args, kwargs, stream=self._stream)
        if call is None:
            return await wrapped(*args, **kwargs)
        try:
            with call.activate():
                result = await wrapped(*args, **kwargs)
        except asyncio.CancelledError:
            call.finish(cancelled=True)
            raise
        except BaseException as error:
            call.finish(error=error)
            raise
        return _finish_or_wrap(call, result, count_files=self._stream)


class CreateWrapper(_Wrapper):
    """Every prediction create route (version, official model, deployment)."""

    def __call__(self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]) -> Any:
        outer = _ACTIVE.get()
        if outer is not None:
            prediction = wrapped(*args, **kwargs)
            _guard(lambda: outer.observe(prediction), None)
            return prediction
        self._touch()
        call = self._start(wrapped, instance, args, kwargs)
        if call is None:
            return wrapped(*args, **kwargs)
        try:
            with call.activate():
                prediction = wrapped(*args, **kwargs)
        except BaseException as error:
            call.finish(error=error)
            raise
        return self._created(call, prediction)


class AsyncCreateWrapper(_Wrapper):
    async def __call__(
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]
    ) -> Any:
        outer = _ACTIVE.get()
        if outer is not None:
            prediction = await wrapped(*args, **kwargs)
            _guard(lambda: outer.observe(prediction), None)
            return prediction
        self._touch()
        call = self._start(wrapped, instance, args, kwargs)
        if call is None:
            return await wrapped(*args, **kwargs)
        try:
            with call.activate():
                prediction = await wrapped(*args, **kwargs)
        except asyncio.CancelledError:
            call.finish(cancelled=True)
            raise
        except BaseException as error:
            call.finish(error=error)
            raise
        return self._created(call, prediction)


class _LifecycleWrapper(_Wrapper):
    """``Prediction.wait`` / ``cancel`` and ``Predictions.cancel(id)``.

    An open create span for the same prediction is continued and ended; there
    is no second span. Otherwise one span covers the call. ``wait`` on a
    prediction that is already terminal makes no request and adds no span.
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
        if _ACTIVE.get() is not None:
            return wrapped(*args, **kwargs)
        self._touch()
        call = self._pending(_guard(lambda: self._prediction_id(instance, args, kwargs), None))
        if call is None:
            if self._untraced(instance):
                return wrapped(*args, **kwargs)
            call = self._start(wrapped, instance, args, kwargs)
            if call is None:
                return wrapped(*args, **kwargs)
        try:
            with call.activate():
                result = wrapped(*args, **kwargs)
        except BaseException as error:
            call.finish(error=error, prediction=None if self._by_id else instance)
            raise
        call.finish(prediction=self._after(instance, result))
        return result


class AsyncLifecycleWrapper(_LifecycleWrapper):
    async def __call__(
        self, wrapped: Any, instance: Any, args: tuple, kwargs: Mapping[str, Any]
    ) -> Any:
        if _ACTIVE.get() is not None:
            return await wrapped(*args, **kwargs)
        self._touch()
        call = self._pending(_guard(lambda: self._prediction_id(instance, args, kwargs), None))
        if call is None:
            if self._untraced(instance):
                return await wrapped(*args, **kwargs)
            call = self._start(wrapped, instance, args, kwargs)
            if call is None:
                return await wrapped(*args, **kwargs)
        try:
            with call.activate():
                result = await wrapped(*args, **kwargs)
        except asyncio.CancelledError:
            call.finish(cancelled=True, prediction=None if self._by_id else instance)
            raise
        except BaseException as error:
            call.finish(error=error, prediction=None if self._by_id else instance)
            raise
        call.finish(prediction=self._after(instance, result))
        return result
