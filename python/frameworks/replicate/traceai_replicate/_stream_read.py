"""The read after a completed stream, off the caller's path (TH-8320).

Replicate's SSE stream reports no status, metrics or token counts; the
prediction does. When a stream completes (its ``done`` event, or the end of
iteration) and its span is recording, :func:`complete_stream` fixes the span's
end time and hands the stream to background workers with one
``queue.SimpleQueue.put``: no thread starts and nothing waits on the caller's
thread or event loop (the very first completed stream in a process starts the
finisher thread once). The workers read ``GET /v1/predictions/{id}`` once (twice
when the API still answers a non-final status) within ``stream_reload_seconds``
and end the span, as of the stream's end, with what the read found.

Where the read goes:

* ``own``: the client built its transport itself (no ``transport=`` or
  ``mounts=`` passed to ``replicate.Client``). The read goes through a
  connection pool this package owns (an ``httpx.AsyncClient`` built with the
  client's proxy options, at most ``_POOL_CONNECTIONS`` connections), on the
  package's own event loop thread, with the caller's base URL and headers. It
  never touches the caller's pools and never builds an httpx client on the
  caller's ``Client``. The budget is an absolute deadline: at the deadline the
  task is cancelled (an anyio cancel scope, see :func:`_within`), which closes
  its connection. Name resolution runs on at most ``_MAX_RESOLVERS`` daemon
  threads, so a hung resolver cannot pile up threads or hold up interpreter
  exit.
* ``sync`` / ``async``: the caller passed its own transport, which is the only
  thing that knows how to reach the API. When that transport can send
  synchronously (``handle_request``), the read runs on one of at most
  ``_MAX_CALLER_READS`` daemon reader threads, for a sync or an async stream;
  each phase gets at most the time left, but a server that trickles bytes can
  keep that thread (and one of the caller's connections) until the
  transport's own timeouts end it. An async-only transport is used from the
  caller's own event loop, as a task (its connections belong to that loop),
  cancelled at the deadline. Replicate's ``RetryTransport`` layers on top are
  skipped; a transport of the caller's that retries by itself is used as it is.

Every read counts as live from admission until its I/O has actually returned
(a read past its deadline still counts until then): at most ``_MAX_READS``
``own`` reads and ``_MAX_CALLER_READS`` caller-transport reads are live at
once (fewer for a caller pool that is small, see :func:`_caller_limit`), and a
stream that completes beyond that ends at once without one
(``replicate.stream.read`` = ``skipped``).

The finisher thread ends spans: when a read returns, and at each read's
deadline at the latest, so a span exporter that runs inline never blocks the
event loop that does the I/O, nor the caller's loop.
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import heapq
import ipaddress
import itertools
import json
import logging
import os
import queue
import re
import socket
import threading
import time
import types
import zlib
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

import httpx
from opentelemetry import context as context_api

try:  # httpcore's asyncio backend is anyio, so httpx's async side always has it
    import anyio
except ImportError:  # pragma: no cover
    anyio = None  # type: ignore[assignment]

from traceai_replicate._attributes import (
    PREDICTION_STATUS,
    STREAM_DONE_REASON,
    STREAM_READ,
    STREAM_TRUNCATED,
    TERMINAL_STATUSES,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# Wall-time budget for the read after a stream, counted from the stream's end;
# 0 turns the read off. It bounds how long a flush, a shutdown and interpreter
# exit wait for reads in flight, so it is capped.
DEFAULT_STREAM_RELOAD_SECONDS = 2.0
MAX_STREAM_RELOAD_SECONDS = 10.0

_READ_ATTEMPTS = 2
# A language model's prediction is tens of KB; bodies are counted as received
# (compressed bytes, then decompressed bytes), never inflated past this.
_MAX_READ_BYTES = 2 * 1024 * 1024
# Live reads through the package's own pool, and the size of that pool.
_MAX_READS = 64
_POOL_CONNECTIONS = 16
_POOL_KEEPALIVE = 4
# Live reads through a transport the caller passed to replicate.Client.
_MAX_CALLER_READS = 4
# A caller pool of N connections lends the read at most N // _CALLER_POOL_SHARE.
_CALLER_POOL_SHARE = 8
# Name lookups in flight on the package's resolver threads.
_MAX_RESOLVERS = 2
# Package-owned httpx clients kept, one per set of proxy options.
_MAX_OWN_CLIENTS = 4
# How long a wait for reads lasts past the last deadline, for the finisher.
_SETTLE_GRACE = 0.25
_PREDICTION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# What the span uses from the read; its input, output and logs are not kept.
_PREDICTION_FIELDS = ("id", "status", "version", "model", "metrics", "error")
# replicate.Client options that change where a request is sent (proxies, and
# the TLS settings of a proxy transport); the package's own client copies them.
_ROUTING_OPTIONS = ("proxy", "proxies", "trust_env", "verify", "cert", "http1", "http2")
_SYNC_HTTP = "_Client__client"
_ASYNC_HTTP = "_Client__async_client"

FINISHER_NAME = "traceai-replicate-stream-finish"
LOOP_NAME = "traceai-replicate-stream-read"
SYNC_READER_NAME = "traceai-replicate-stream-read-sync"
RESOLVER_NAME = "traceai-replicate-resolve"

# replicate.stream.read values: why a stream span has no final status.
SKIPPED = "skipped"  # no read was made (cap, closed client, provider cannot wait)
TIMEOUT = "timeout"  # the read did not return in the time it had
FAILED = "failed"  # refused (not 200), unreadable or over 2 MiB
UNFINISHED = "unfinished"  # the API still answered a non-final status

_monotonic = time.monotonic
_T = TypeVar("_T")


def _guard(function: Callable[[], _T], default: _T) -> _T:
    try:
        return function()
    except Exception:
        logger.debug("traceai-replicate: stream read step failed", exc_info=True)
        return default


# -- instrumentation suppressed for the read -----------------------------------------------


def _suppression_keys() -> Tuple[Any, ...]:
    keys: List[Any] = []
    for name in ("_SUPPRESS_INSTRUMENTATION_KEY", "_SUPPRESS_HTTP_INSTRUMENTATION_KEY"):
        key = getattr(context_api, name, None)
        if key is not None:
            keys.append(key)
    keys.append("suppress_instrumentation")  # the plain key older instrumentations read
    return tuple(keys)


_SUPPRESSION_KEYS = _suppression_keys()


def _suppressed() -> Any:
    """An empty OpenTelemetry context with instrumentation (and HTTP instrumentation) suppressed."""
    current = context_api.Context()
    for key in _SUPPRESSION_KEYS:
        current = context_api.set_value(key, True, current)
    return current


# -- per-instrumentor settings ----------------------------------------------------------


class StreamReads:
    """What one ``instrument()`` decided about the read after a stream."""

    __slots__ = ("budget", "hooked", "shut_down")

    def __init__(self, budget: float) -> None:
        self.budget = budget
        # The provider's force_flush/shutdown wait for reads in flight. Without
        # that (no provider hook could be installed, or after uninstrument())
        # a read could end its span after the processors stopped, so none is made.
        self.hooked = False
        # The provider was shut down: its spans are no longer exported.
        self.shut_down = False


# -- shared worker state (reset in a forked child) -------------------------------------------


class _State:
    def __init__(self) -> None:
        # Re-entrant: a signal handler (register()'s shutdown) can settle reads
        # on a thread that is inside complete_stream().
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.pending: Set["_Read"] = set()
        self.inbox: "queue.SimpleQueue[Tuple[str, _Read]]" = queue.SimpleQueue()
        self.finisher: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.own_live = 0
        self.caller_live = 0
        self.caller_tasks: Dict[Any, "_Read"] = {}  # read tasks on a caller's loop
        self.own_tasks: Set[Any] = set()
        self.own_clients: "collections.OrderedDict[Tuple[Any, ...], Any]" = collections.OrderedDict()
        self.sync_jobs: "queue.SimpleQueue[_Read]" = queue.SimpleQueue()
        self.sync_threads = 0
        self.sync_idle = 0  # reader threads waiting for a job and not yet promised one
        self.resolve_jobs: "queue.SimpleQueue[Any]" = queue.SimpleQueue()
        self.resolving = 0
        self.resolver_threads = 0
        self.seq = itertools.count()


_STATE = _State()
# Set on the package's own threads: a flush from one of them cannot wait for reads.
_WORKER = threading.local()


def _after_fork_in_child() -> None:
    """A forked child has none of the parent's worker threads. It starts its
    own when it needs them; the reads the parent had in flight are the
    parent's to end (their spans are skipped here, see ``_Call.finish``)."""
    global _STATE
    _STATE = _State()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def live_reads() -> int:
    """Reads whose I/O has not returned yet (for tests and diagnostics)."""
    state = _STATE
    with state.lock:
        return state.own_live + state.caller_live


def pending_spans() -> int:
    """Stream spans still waiting for their read."""
    state = _STATE
    with state.lock:
        return len(state.pending)


def _failure(error: BaseException) -> str:
    """A read that raised: ``timeout`` for a transport timeout, else ``failed``."""
    return TIMEOUT if isinstance(error, httpx.TimeoutException) else FAILED


def _post(state: _State, message: Tuple[str, "_Read"]) -> None:
    state.inbox.put(message)


# -- the response body ---------------------------------------------------------------------


class _Body:
    """A response body read up to _MAX_READ_BYTES, counted as it arrives.

    The read asks for ``Accept-Encoding: identity``; a server that compresses
    anyway is decompressed in bounded steps, so a small compressed body can
    never inflate past the cap in one go.
    """

    __slots__ = ("data", "_raw", "_inflate")

    def __init__(self, encoding: Optional[str]) -> None:
        name = (encoding or "identity").strip().lower()
        if name in ("", "identity"):
            self._inflate = None
        elif name in ("gzip", "x-gzip"):
            self._inflate = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif name == "deflate":
            self._inflate = zlib.decompressobj()
        else:
            raise ValueError("unsupported content encoding")
        self.data = bytearray()
        self._raw = 0

    def add(self, chunk: bytes) -> bool:
        """False once the body is over the cap."""
        self._raw += len(chunk)
        if self._raw > _MAX_READ_BYTES:
            return False
        if self._inflate is None:
            self.data.extend(chunk)
        else:
            self.data.extend(self._inflate.decompress(chunk, _MAX_READ_BYTES + 1 - len(self.data)))
            if self._inflate.unconsumed_tail:
                return False
        return len(self.data) <= _MAX_READ_BYTES

    def prediction(self) -> Optional[Dict[str, Any]]:
        data = json.loads(bytes(self.data))
        return data if isinstance(data, dict) else None


def _loaded_body(response: Any) -> Optional[Dict[str, Any]]:
    """A response a transport built with its content already read."""
    content = response.content
    if len(content) > _MAX_READ_BYTES:
        return None
    data = json.loads(content)
    return data if isinstance(data, dict) else None


# -- transports ---------------------------------------------------------------------------


def _is_retry_transport(transport: Any) -> bool:
    return any(
        kind.__name__ == "RetryTransport" and kind.__module__.startswith("replicate")
        for kind in type(transport).__mro__
    ) and hasattr(transport, "_wrapped_transport")


def _unwrap_retries(transport: Any) -> Any:
    """``transport`` without the replicate RetryTransport layers on top of it.

    replicate wraps every client transport in its RetryTransport (up to 10
    attempts on 429/503/504, sleeping for any numeric Retry-After, uncapped);
    the read after a stream must not wait like that. A retrying transport
    *inside* the caller's own transport is the caller's and is used as it is.
    """
    while _is_retry_transport(transport):
        transport = transport._wrapped_transport
    return transport


def _caller_limit(transport: Any) -> int:
    """How many reads may borrow the caller's transport at once.

    For an httpx transport whose pool size is known, at most one in
    _CALLER_POOL_SHARE of its connections (none for a pool of fewer than
    that), so a read cannot starve the caller's own requests.
    """
    if isinstance(transport, (httpx.HTTPTransport, httpx.AsyncHTTPTransport)):
        size = getattr(getattr(transport, "_pool", None), "_max_connections", None)
        if isinstance(size, int):
            return min(_MAX_CALLER_READS, size // _CALLER_POOL_SHARE)
    return _MAX_CALLER_READS


def _sends_sync(transport: Any) -> bool:
    """The transport implements ``handle_request`` (httpx's base class only raises)."""
    handle = getattr(type(transport), "handle_request", None)
    return handle is not None and handle is not getattr(httpx.BaseTransport, "handle_request", None)


def _read_url(http: Any, prediction_id: str) -> httpx.URL:
    """``{base_url}v1/predictions/{id}``, joined as the client would join it."""
    base = http.base_url
    prefix = base.raw_path if base.raw_path.endswith(b"/") else base.raw_path + b"/"
    return base.copy_with(raw_path=prefix + b"v1/predictions/" + prediction_id.encode("ascii"))


def _routing(client: Any) -> Tuple[Tuple[str, Any], ...]:
    options = getattr(client, "_client_kwargs", None) or {}
    return tuple((name, options[name]) for name in _ROUTING_OPTIONS if name in options)


# -- one read ------------------------------------------------------------------------------


class _Read:
    """A completed stream whose span waits for the read after it."""

    __slots__ = (
        "state",
        "call",
        "collector",
        "ended",
        "deadline",
        "prediction_id",
        "client",
        "http",
        "url",
        "transport",
        "route",
        "extra",
        "poll_interval",
        "found",
        "last_status",
        "outcome",
        "loop",
        "attempts",
        "seq",
        "_slot",
        "_over",
    )

    def __init__(
        self,
        state: _State,
        call: Any,
        collector: Any,
        ended: int,
        deadline: float,
        prediction_id: str,
        client: Any,
        http: Any,
        url: httpx.URL,
        transport: Any,
        route: str,
        extra: Dict[str, Any],
    ) -> None:
        self.state = state
        self.call = call
        self.collector = collector
        self.ended = ended
        self.deadline = deadline
        self.prediction_id = prediction_id
        self.client = client
        self.http = http
        self.url = url
        self.transport = transport  # the caller's (sync and async routes)
        self.route = route
        self.extra = extra
        self.poll_interval = _poll_interval(client)
        self.found: Any = None
        self.last_status: Optional[str] = None
        self.outcome: Optional[str] = None
        self.loop: Any = None  # the caller's loop, for an "async" read
        self.attempts = 0  # GETs made (or started) so far
        self.seq = next(state.seq)
        self._slot = False
        self._over = False

    # -- admission and release ---------------------------------------------------------

    def admit(self, limit: int) -> bool:
        state = self.state
        with state.lock:
            if self.route == "own":
                if state.own_live >= limit:
                    return False
                state.own_live += 1
            else:
                for task, entry in list(state.caller_tasks.items()):
                    if _guard(entry.loop.is_closed, False):  # its loop ended with the read pending
                        del state.caller_tasks[task]
                        entry.release()
                if state.caller_live >= limit:
                    return False
                state.caller_live += 1
            self._slot = True
            state.pending.add(self)
        return True

    def release(self) -> None:
        """The read's I/O has returned (or will never run): free its slot."""
        state = self.state
        with state.lock:
            if not self._slot:
                return
            self._slot = False
            if self.route == "own":
                state.own_live -= 1
            else:
                state.caller_live -= 1

    # -- the end of the span -------------------------------------------------------------

    def attributes(self, outcome: Optional[str]) -> Dict[str, Any]:
        extra = dict(self.extra)
        if self.found is None:
            reason = self.outcome or outcome
            if reason:
                extra[STREAM_READ] = reason
            extra.update(_status_from_stream(self.extra, self.last_status))
        return extra

    def end(self, outcome: Optional[str] = None) -> None:
        """End the span as of the stream's end with what is known; once."""
        state = self.state
        with state.lock:
            if self._over:
                return
            self._over = True
        try:
            self.call.finish(
                collector=self.collector,
                prediction=self.found,
                end_time=self.ended,
                extra=self.attributes(outcome),
            )
        finally:
            with state.lock:
                state.pending.discard(self)
                state.cond.notify_all()

    # -- the read itself -------------------------------------------------------------------

    def _take(self, data: Dict[str, Any]) -> bool:
        """Record one answer; True when it is final."""
        status = data.get("status")
        if status in TERMINAL_STATUSES:
            self.found = types.SimpleNamespace(**{name: data.get(name) for name in _PREDICTION_FIELDS})
            return True
        if isinstance(status, str):
            self.last_status = status
        return False

    def _pause(self) -> Optional[float]:
        """Seconds to wait before the next attempt, or None when this was the last."""
        if self.attempts >= _READ_ATTEMPTS:
            self.outcome = UNFINISHED
            return None
        pause = min(self.poll_interval, self.deadline - _monotonic())
        if pause <= 0:
            self.outcome = TIMEOUT
            return None
        return pause

    def _request(self) -> Optional[httpx.Request]:
        remaining = self.deadline - _monotonic()
        if remaining <= 0:
            self.outcome = TIMEOUT
            return None
        headers = httpx.Headers(self.http.headers)
        headers["Accept-Encoding"] = "identity"
        return httpx.Request(
            "GET", self.url, headers=headers, extensions={"timeout": httpx.Timeout(remaining).as_dict()}
        )

    async def aread(self, send: Callable[["_Read", httpx.Request], Any]) -> None:
        while self.attempts < _READ_ATTEMPTS:
            request = self._request()
            if request is None:
                return
            self.attempts += 1
            data = await send(self, request)
            if data is None:
                self.outcome = self.outcome or FAILED
                return
            if self._take(data):
                return
            pause = self._pause()
            if pause is None:
                return
            await asyncio.sleep(pause)

    def read_sync(self) -> None:
        while self.attempts < _READ_ATTEMPTS:
            request = self._request()
            if request is None:
                return
            self.attempts += 1
            data = self._send_sync(request)
            if data is None:
                self.outcome = self.outcome or FAILED
                return
            if self._take(data):
                return
            pause = self._pause()
            if pause is None:
                return
            time.sleep(pause)

    def _send_sync(self, request: httpx.Request) -> Optional[Dict[str, Any]]:
        """Through the caller's transport, below replicate's RetryTransport
        layers. Each phase gets at most the time left; the body is checked per
        chunk."""
        if self.http.is_closed:
            self.outcome = SKIPPED
            return None
        response = self.transport.handle_request(request)
        try:
            response.request = request
            if response.status_code != 200:
                return None
            if response.is_stream_consumed:
                return _loaded_body(response)
            body = _Body(response.headers.get("content-encoding"))
            for chunk in response.iter_raw():
                if not body.add(chunk) or _monotonic() > self.deadline:
                    return None
        finally:
            response.close()
        return body.prediction()


async def _send_own(entry: _Read, request: httpx.Request) -> Optional[Dict[str, Any]]:
    """Through the package's own pool, on the package's loop."""
    response = await _own_client(entry.state, entry.client).send(request, stream=True)
    try:
        if response.status_code != 200:
            return None
        body = _Body(response.headers.get("content-encoding"))
        async for chunk in response.aiter_raw():
            if not body.add(chunk):
                return None
    finally:
        await response.aclose()
    return body.prediction()


async def _send_caller_async(entry: _Read, request: httpx.Request) -> Optional[Dict[str, Any]]:
    """Through the caller's async transport, on the caller's loop."""
    if entry.http.is_closed:
        entry.outcome = SKIPPED
        return None
    response = await entry.transport.handle_async_request(request)
    try:
        response.request = request
        if response.status_code != 200:
            return None
        if response.is_stream_consumed:
            return _loaded_body(response)
        body = _Body(response.headers.get("content-encoding"))
        async for chunk in response.aiter_raw():
            if not body.add(chunk):
                return None
    finally:
        await response.aclose()
    return body.prediction()


async def _within(entry: _Read, read: Any, seconds: float) -> None:
    """Run ``read``, cancelled after ``seconds``.

    An anyio cancel scope, not ``asyncio.wait_for``: httpcore reads inside
    anyio scopes of its own, and a plain asyncio cancel delivered there can be
    absorbed, leaving a trickling read alive past its deadline (seen with
    anyio 4.15 under 100 concurrent reads). anyio re-delivers its own
    cancellation until the task leaves the scope.
    """
    if anyio is None:  # pragma: no cover
        await asyncio.wait_for(read, seconds)
        return
    with anyio.move_on_after(seconds) as scope:
        await read
    if scope.cancelled_caught:
        entry.outcome = entry.outcome or TIMEOUT


def _uncancel() -> None:
    task = asyncio.current_task()
    uncancel = getattr(task, "uncancel", None)  # 3.11+: keep asyncio.timeout() exact
    if uncancel is not None:
        uncancel()


async def _read_within_deadline(entry: _Read, send: Callable[[_Read, httpx.Request], Any]) -> None:
    """One read on an event loop, cancelled at its deadline (which closes its
    connection), then handed to the finisher. Instrumentation is suppressed.

    On a caller's loop the task can be cancelled from outside: ``asyncio.run``
    cancels every task left when its main coroutine returns. The first such
    cancel restarts the read (with the GETs it has left, within its
    deadline), so the end of the loop waits for it as interpreter exit does
    for a sync stream: at most the budget. A second cancel ends it.
    """
    token = context_api.attach(_suppressed())
    restarted = False
    try:
        while True:
            remaining = entry.deadline - _monotonic()
            if remaining <= 0:
                entry.outcome = entry.outcome or TIMEOUT
                return
            try:
                await _within(entry, entry.aread(send), remaining)
                return
            except asyncio.CancelledError:
                if restarted or entry.found is not None:
                    entry.outcome = entry.outcome or TIMEOUT
                    raise
                restarted = True
                _uncancel()
    except asyncio.TimeoutError:
        entry.outcome = entry.outcome or TIMEOUT
    except Exception as error:
        entry.outcome = entry.outcome or _failure(error)
        logger.debug("traceai-replicate: could not read the streamed prediction", exc_info=True)
    finally:
        context_api.detach(token)
        entry.release()
        _guard(lambda: _post(entry.state, ("end", entry)), None)


# -- the package's event loop (own reads) ----------------------------------------------------


class _ReadLoop(asyncio.SelectorEventLoop):
    """The package's event loop. Names are resolved on its own daemon threads
    (at most _MAX_RESOLVERS lookups at once), never on the default executor,
    whose threads interpreter exit would wait for."""

    async def getaddrinfo(  # type: ignore[override]
        self, host: Any, port: Any, *, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> Any:
        try:
            ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return await _resolve(self, (host, port, family, type, proto, flags))
        return socket.getaddrinfo(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)


async def _resolve(loop: asyncio.AbstractEventLoop, query: Tuple[Any, ...]) -> Any:
    state = _STATE
    future = loop.create_future()
    with state.lock:
        if state.resolving >= _MAX_RESOLVERS:
            raise OSError("traceai-replicate: name resolution is busy")
        state.resolving += 1
        start = state.resolver_threads < state.resolving
        if start:
            state.resolver_threads += 1
    try:
        if start:
            threading.Thread(target=_resolver_main, args=(state,), name=RESOLVER_NAME, daemon=True).start()
    except BaseException:
        with state.lock:
            state.resolving -= 1
            state.resolver_threads -= 1
        raise
    state.resolve_jobs.put((loop, future, query))
    return await future


def _settle_future(future: Any, ok: bool, value: Any) -> None:
    if not future.done():
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)


def _resolver_main(state: _State) -> None:
    while True:
        loop, future, query = state.resolve_jobs.get()
        try:
            if future.done():  # its read was cancelled while it waited
                continue
            try:
                ok, value = True, socket.getaddrinfo(*query)
            except Exception as error:
                ok, value = False, error
            try:
                loop.call_soon_threadsafe(_settle_future, future, ok, value)
            except RuntimeError:  # the loop is closed
                pass
        finally:
            with state.lock:
                state.resolving -= 1


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    _WORKER.active = True
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except BaseException:  # pragma: no cover - a daemon thread at interpreter exit
        logger.debug("traceai-replicate: the read loop stopped", exc_info=True)


def _ensure_loop(state: _State) -> asyncio.AbstractEventLoop:
    """Start the package's loop thread (from the finisher, never the caller)."""
    if state.loop is None:
        loop = _ReadLoop()
        threading.Thread(target=_run_loop, args=(loop,), name=LOOP_NAME, daemon=True).start()
        state.loop = loop
    return state.loop


def _own_client(state: _State, client: Any) -> Any:
    """The package's httpx client for this replicate client's proxy options
    (on the package's loop only)."""
    routing = _routing(client)
    key = tuple((name, repr(value)) for name, value in routing)
    own = state.own_clients.get(key)
    if own is not None and not own.is_closed:
        state.own_clients.move_to_end(key)
        return own
    limits = httpx.Limits(max_connections=_POOL_CONNECTIONS, max_keepalive_connections=_POOL_KEEPALIVE)
    own = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(limits=limits), **dict(routing))
    state.own_clients[key] = own
    while len(state.own_clients) > _MAX_OWN_CLIENTS:
        _, old = state.own_clients.popitem(last=False)
        _keep(state.own_tasks, asyncio.ensure_future(old.aclose()))
    return own


def _keep(tasks: Set[Any], task: Any) -> None:
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _start_own(entry: _Read) -> None:
    """On the package's loop: start the read."""
    _keep(entry.state.own_tasks, asyncio.ensure_future(_read_within_deadline(entry, _send_own)))


# -- reader threads (a caller's sync transport) -----------------------------------------------


def _sync_reader_main(state: _State) -> None:
    _WORKER.active = True
    while True:
        entry = state.sync_jobs.get()
        token = context_api.attach(_suppressed())
        try:
            entry.read_sync()
        except Exception as error:
            entry.outcome = entry.outcome or _failure(error)
            logger.debug("traceai-replicate: could not read the streamed prediction", exc_info=True)
        finally:
            context_api.detach(token)
            entry.release()
            _guard(lambda: _post(state, ("end", entry)), None)
            with state.lock:
                state.sync_idle += 1


def _ensure_sync_reader(state: _State) -> None:
    """From the finisher: promise the next job an idle reader thread, or start
    one, at most _MAX_CALLER_READS of them (admission keeps live reads under
    that, so a job never waits behind another)."""
    with state.lock:
        if state.sync_idle > 0:
            state.sync_idle -= 1
            return
        start = state.sync_threads < _MAX_CALLER_READS
        if start:
            state.sync_threads += 1
    if start:
        try:
            threading.Thread(
                target=_sync_reader_main, args=(state,), name=SYNC_READER_NAME, daemon=True
            ).start()
        except BaseException:
            with state.lock:
                state.sync_threads -= 1
            raise


# -- the finisher -----------------------------------------------------------------------------


def _dispatch(entry: _Read) -> None:
    state = entry.state
    if entry.route == "own":
        _ensure_loop(state).call_soon_threadsafe(_start_own, entry)
    elif entry.route == "sync":
        _ensure_sync_reader(state)
        state.sync_jobs.put(entry)
    # "async" runs on the caller's loop already.


def _finisher_main(state: _State) -> None:
    """Start reads, end spans when their read returns, and at each deadline."""
    _WORKER.active = True
    deadlines: List[Tuple[float, int, _Read]] = []
    while True:
        try:
            timeout = None if not deadlines else max(0.0, deadlines[0][0] - _monotonic())
            try:
                kind, entry = state.inbox.get(True, timeout)
            except queue.Empty:
                kind, entry = "", None  # type: ignore[assignment]
            if kind == "start":
                heapq.heappush(deadlines, (entry.deadline, entry.seq, entry))
                try:
                    _dispatch(entry)
                except Exception:
                    logger.debug("traceai-replicate: could not start the read", exc_info=True)
                    entry.outcome = entry.outcome or FAILED
                    entry.release()
                    entry.end()
            elif kind == "end":
                entry.end()
            now = _monotonic()
            while deadlines and deadlines[0][0] <= now:
                _guard(lambda: heapq.heappop(deadlines)[2].end(TIMEOUT), None)
        except Exception:  # pragma: no cover - never let the finisher die
            logger.debug("traceai-replicate: stream finisher step failed", exc_info=True)


def _ensure_finisher(state: _State) -> None:
    if state.finisher is not None:
        return
    with state.lock:
        if state.finisher is not None:
            return
        thread = threading.Thread(target=_finisher_main, args=(state,), name=FINISHER_NAME, daemon=True)
        thread.start()
        state.finisher = thread


def _start_on_callers_loop(entry: _Read) -> None:
    """An async stream through the caller's own transport: a task on the
    caller's running loop (its transport's connections belong to it), in a
    fresh context."""
    loop = asyncio.get_running_loop()
    entry.loop = loop
    task = contextvars.Context().run(loop.create_task, _read_within_deadline(entry, _send_caller_async))
    try:
        task._log_destroy_pending = False  # a loop closed without cancelling it
    except Exception:
        pass
    state = entry.state
    with state.lock:
        state.caller_tasks[task] = entry

    def forget(done: Any) -> None:
        # Also for a task cancelled before it ran (asyncio.run ending right
        # after the stream): its body never ran, so free its slot and end it here.
        with state.lock:
            state.caller_tasks.pop(done, None)
        entry.release()
        _guard(lambda: _post(state, ("end", entry)), None)

    task.add_done_callback(forget)


# -- completing a stream ----------------------------------------------------------------------


def _poll_interval(client: Any) -> float:
    value = getattr(client, "poll_interval", 0.5)
    return float(value) if isinstance(value, (int, float)) and value >= 0 else 0.5


def _done_reason(done: Any) -> Optional[str]:
    """``reason`` from the data of a ``done`` event (``{"reason": "canceled"}``)."""
    data = json.loads(getattr(done, "data", "") or "{}")
    reason = data.get("reason") if isinstance(data, dict) else None
    return reason if isinstance(reason, str) and reason else None


def stream_attributes(done: Any) -> Dict[str, Any]:
    """What the stream itself says about its end: why ``done`` came, or that
    it ended without one."""
    if done is None:
        return {STREAM_TRUNCATED: True}
    reason = _guard(lambda: _done_reason(done), None)
    return {STREAM_DONE_REASON: reason} if reason else {}


def _status_from_stream(extra: Dict[str, Any], last_status: Optional[str]) -> Dict[str, Any]:
    """The status to record when the read found no final one: ``canceled``
    from ``done``'s reason, or, for a stream that ended without ``done``, the
    non-final status the API answered."""
    if extra.get(STREAM_DONE_REASON) == "canceled":
        return {PREDICTION_STATUS: "canceled"}
    if extra.get(STREAM_TRUNCATED) and last_status:
        return {PREDICTION_STATUS: last_status}
    return {}


def _plan(
    call: Any, collector: Any, reads: Optional[StreamReads], ended: int, extra: Dict[str, Any], is_async: bool
) -> Any:
    """A read to start, SKIPPED, or None when no read is wanted."""
    if reads is None or reads.budget <= 0 or not call.stream:
        return None
    if not call.span.is_recording() or reads.shut_down:
        return None  # nothing would be exported: no request is spent on it
    prediction = call.prediction
    prediction_id = getattr(prediction, "id", None)
    if not isinstance(prediction_id, str) or not _PREDICTION_ID.fullmatch(prediction_id):
        return None
    if getattr(prediction, "status", None) in TERMINAL_STATUSES:
        return None  # already final: the span uses it as it is
    if not reads.hooked:
        return SKIPPED
    client = call.client
    http = vars(client).get(_ASYNC_HTTP if is_async else _SYNC_HTTP) if client is not None else None
    if http is None or http.is_closed:
        return SKIPPED
    url = _read_url(http, prediction_id)
    options = getattr(client, "_client_kwargs", None) or {}
    transport: Any = None
    if options.get("transport") is None and not options.get("mounts"):
        route, limit = "own", _MAX_READS
    else:
        # The caller's transport: sent synchronously on a reader thread when it
        # can be (also for an async stream), else on the caller's running loop.
        transport = _unwrap_retries(http._transport_for_url(url))
        route = "sync" if _sends_sync(transport) else "async"
        limit = _caller_limit(transport)
        if route == "async":
            asyncio.get_running_loop()  # RuntimeError under another event loop library: no read
    state = _STATE
    entry = _Read(
        state,
        call,
        collector,
        ended,
        _monotonic() + reads.budget,
        prediction_id,
        client,
        http,
        url,
        transport,
        route,
        extra,
    )
    return entry if entry.admit(limit) else SKIPPED


def complete_stream(
    call: Any,
    collector: Any,
    reads: Optional[StreamReads],
    *,
    is_async: bool = False,
    done: Any = None,
) -> None:
    """The stream completed: fix the span's end time and give the caller its
    control back at once.

    ``done`` is the stream's ``done`` event, or None when iteration ended
    without one. Without a read the span ends here; otherwise the stream is
    queued for the workers (``SimpleQueue.put``, which never blocks) and they
    end the span. Nothing here waits on I/O or starts a thread, except the
    finisher thread on the first completed stream of a process. No Exception
    escapes; a BaseException (KeyboardInterrupt) ends the span first, then
    propagates.
    """
    ended = time.time_ns()
    if call.completed or call.finished:
        return
    call.completed = True
    extra = stream_attributes(done) if call.stream else {}
    planned = _guard(lambda: _plan(call, collector, reads, ended, extra, is_async), SKIPPED)
    if not isinstance(planned, _Read):
        if planned is not None:
            extra = dict(extra, **{STREAM_READ: planned})
        call.finish(collector=collector, end_time=ended, extra=dict(extra, **_status_from_stream(extra, None)))
        return
    entry = planned
    try:
        _ensure_finisher(entry.state)
        _post(entry.state, ("start", entry))
        if entry.route == "async":
            _start_on_callers_loop(entry)
    except Exception:  # e.g. no new thread at interpreter shutdown: end it without the read
        logger.debug("traceai-replicate: could not start the read after a stream", exc_info=True)
        entry.outcome = entry.outcome or SKIPPED
        entry.release()
        entry.end()
    except BaseException:
        entry.outcome = entry.outcome or SKIPPED
        entry.release()
        entry.end()
        raise


# -- flush, shutdown and exit ----------------------------------------------------------------


def _wait(cond: threading.Condition, timeout: float) -> None:
    cond.wait(timeout)


def settle_stream_reads(until: Optional[float] = None) -> bool:
    """End the span of every completed stream whose read is still running.

    Reads in flight are waited for, no longer than the last of their deadlines
    (plus a short grace for the finisher) and, when ``until`` (a monotonic
    time) is given, no later than that; then each span still waiting ends
    with what is known (``replicate.stream.read`` = ``timeout``). Returns
    False when ``until`` cut a read short. On the finisher's or the read
    loop's own thread nothing is waited for. A KeyboardInterrupt (or any
    BaseException) while waiting ends every pending span first, then
    propagates.
    """
    state = _STATE
    with state.lock:
        pending = list(state.pending)
    if not pending:
        return True
    limit = max(entry.deadline for entry in pending) + _SETTLE_GRACE
    cut = limit if until is None else min(limit, until)
    if getattr(_WORKER, "active", False):
        cut = _monotonic()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        # A flush on the event loop that runs some of these reads: they cannot
        # progress while it waits, so their spans end now with what is known.
        for entry in pending:
            if entry.loop is running:
                _guard(lambda: entry.end(TIMEOUT), None)
    try:
        with state.lock:
            while any(entry in state.pending for entry in pending):
                remaining = cut - _monotonic()
                if remaining <= 0:
                    break
                _wait(state.cond, remaining)
    except BaseException:
        for entry in pending:
            _guard(lambda: entry.end(TIMEOUT), None)
        raise
    in_time = True
    for entry in pending:
        if entry in state.pending:
            in_time = in_time and cut >= limit
            _guard(lambda: entry.end(TIMEOUT), None)
    return in_time
