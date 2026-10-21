"""The read after a completed stream, off the caller's path (TH-8320).

Replicate's SSE stream reports no status, metrics or token counts; the
prediction does. When a stream completes (its ``done`` event, or the end of
iteration) and its span is recording, :func:`complete_stream` fixes the span's
end time and hands the stream to the workers with one ``SimpleQueue.put``
(which neither blocks nor releases the GIL, so a caller among hundreds of
threads is not made to wait for it again). Nothing waits on the caller's
thread or event loop, and no thread starts there: the worker threads start in
``instrument()`` (a forked child starts its own once, on its first completed
stream, and a completer never waits for another to do it). A dispatcher thread
passes the queued streams, in batches, to the package's read loop, which
starts each read, ``GET /v1/predictions/{id}`` once (twice when the API still
answers a non-final status), within ``stream_reload_seconds``, and arms a
timer at the read's deadline. A finisher thread ends the span, as of the
stream's end, with what the read found, when the read returns or at the
deadline. The finisher only ends spans, so an exporter that runs inline delays
span ends, never the start of a read.

Where the read goes:

* ``own``, through a connection pool this package owns, on the read loop,
  with the caller's base URL and headers:

  - the client built its transport itself (no ``transport=`` or ``mounts=``
    passed to ``replicate.Client``): with the client's proxy options;
  - the caller passed a plain ``httpx.HTTPTransport`` / ``AsyncHTTPTransport``
    without a proxy: with that transport's TLS context, HTTP versions, local
    address, socket options and connect retries (see :func:`_plain_settings`);
    the caller's pool itself is not used.

  It never touches the caller's pools and never builds an httpx client on the
  caller's ``Client``. The budget is an absolute deadline: at the deadline the
  task is cancelled (an anyio cancel scope, see :func:`_within`), which closes
  its connection. Name lookups run on at most ``_MAX_RESOLVERS`` daemon
  threads; concurrent lookups of one name share one, a result is kept for
  ``_DNS_CACHE_SECONDS``, and a lookup waits for a free resolver within the
  read's deadline (it fails at once only while every resolver has been stuck
  for ``_RESOLVER_STUCK_SECONDS``).
* ``sync`` / ``async``, through any other transport the caller passed, the
  only thing that knows how to reach the API. When it can send synchronously
  (``handle_request``) the read runs on a daemon reader thread, for a sync or
  an async stream; each phase gets at most the time left, but a server that
  trickles bytes can keep that thread (and one of the caller's connections)
  until the transport's own timeouts end it. An async-only transport is used
  from the caller's own event loop, as a task (its connections belong to that
  loop), cancelled at the deadline; a cancel from the caller's loop (for
  example ``asyncio.run()`` ending) ends it at once. Replicate's
  ``RetryTransport`` layers on top are skipped; a transport of the caller's
  that retries by itself is used as it is.

Every read counts as live from admission until its I/O has actually returned
(a read past its deadline still counts until then): at most ``_MAX_READS``
``own`` reads, and ``_MAX_CALLER_READS`` per caller transport (fewer for a
small pool, see :func:`_caller_limit`) and ``_MAX_CALLER_READS_TOTAL`` over
all of them. A stream that completes beyond that ends at once without one
(``replicate.stream.read`` = ``skipped``).
"""

from __future__ import annotations

import asyncio
import collections
import contextvars
import inspect
import ipaddress
import json
import logging
import os
import queue
import re
import socket
import ssl
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

try:  # httpx's own transport is built on httpcore
    import httpcore
except ImportError:  # pragma: no cover
    httpcore = None  # type: ignore[assignment]

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
# 0 turns the read off. It bounds how long a shutdown and interpreter exit wait
# for reads in flight, so it is capped.
DEFAULT_STREAM_RELOAD_SECONDS = 2.0
MAX_STREAM_RELOAD_SECONDS = 10.0
# How long force_flush() waits for reads in flight (never more than half its
# timeout); 0, the default, waits for none: their spans end at once.
DEFAULT_STREAM_FLUSH_WAIT_SECONDS = 0.0

_READ_ATTEMPTS = 2
# A language model's prediction is tens of KB; bodies are counted as received
# (compressed bytes, then decompressed bytes), never inflated past this.
_MAX_READ_BYTES = 2 * 1024 * 1024
# Live reads through the package's own pools, and the connections one such
# pool may open: one per admitted read, so an admitted read never queues for
# a connection.
_MAX_READS = 64
_POOL_CONNECTIONS = _MAX_READS
_POOL_KEEPALIVE = 8
# Live reads through one transport the caller passed to replicate.Client, and
# through all of them (each is a reader thread or a task on the caller's loop).
_MAX_CALLER_READS = 4
_MAX_CALLER_READS_TOTAL = 16
# Stream spans admitted and not yet ended (their read in flight, or returned
# and waiting for the finisher to export them) count against the same cap. A
# slow inline exporter can fall behind; past the cap, a stream ends on the
# caller's thread without a read, as it would without the read at all. That
# bounds memory, and what shutdown and exit have left to export.
_MAX_PENDING_SPANS = _MAX_READS
# A caller pool of N connections lends the read at most N // _CALLER_POOL_SHARE.
_CALLER_POOL_SHARE = 8
# Name lookups: resolver threads, how long a lookup may run before its thread
# counts as stuck, and how long a result is kept.
_MAX_RESOLVERS = 2
_RESOLVER_STUCK_SECONDS = 5.0
_DNS_CACHE_SECONDS = 5.0
_DNS_CACHE_ENTRIES = 32
# Package-owned httpx clients kept, one per set of routing options.
_MAX_OWN_CLIENTS = 4
# How long a shutdown or exit waits past the last deadline, for the finisher.
_SETTLE_GRACE = 0.25
# How long a fork waits for the finisher to finish ending (exporting) a span.
_FORK_WAIT_SECONDS = 5.0
_PREDICTION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# What the span uses from the read; its input, output and logs are not kept.
_PREDICTION_FIELDS = ("id", "status", "version", "model", "metrics", "error")
# replicate.Client options that change where a request is sent (proxies, and
# the TLS settings of a proxy transport); the package's own client copies them.
_ROUTING_OPTIONS = ("proxy", "proxies", "trust_env", "verify", "cert", "http1", "http2")
# What a plain httpx transport is built with, copied into the package's own.
_PLAIN_SETTINGS = (
    ("verify", "_ssl_context"),
    ("http1", "_http1"),
    ("http2", "_http2"),
    ("retries", "_retries"),
    ("local_address", "_local_address"),
    ("uds", "_uds"),
    ("socket_options", "_socket_options"),
)
_SYNC_HTTP = "_Client__client"
_ASYNC_HTTP = "_Client__async_client"

FINISHER_NAME = "traceai-replicate-stream-finish"
DISPATCHER_NAME = "traceai-replicate-stream-dispatch"
LOOP_NAME = "traceai-replicate-stream-read"
SYNC_READER_NAME = "traceai-replicate-stream-read-sync"
RESOLVER_NAME = "traceai-replicate-resolve"

# replicate.stream.read values: why a stream span has no final status.
SKIPPED = "skipped"  # no read was made (a cap, a closed client, no provider hook)
TIMEOUT = "timeout"  # the read did not return within its budget, or its loop ended
FAILED = "failed"  # refused (not 200), unreadable or over 2 MiB
UNFINISHED = "unfinished"  # the API still answered a non-final status
FLUSHED = "flushed"  # a force_flush() ended the span before the read returned

_monotonic = time.monotonic
_T = TypeVar("_T")


def _guard(function: Callable[[], _T], default: _T) -> _T:
    try:
        return function()
    except Exception:
        logger.debug("traceai-replicate: stream read step failed", exc_info=True)
        return default


def _parameters(function: Any) -> frozenset:
    try:
        return frozenset(inspect.signature(function).parameters)
    except (TypeError, ValueError):  # pragma: no cover
        return frozenset()


_ASYNC_TRANSPORT_PARAMETERS = _parameters(httpx.AsyncHTTPTransport.__init__)


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

    __slots__ = ("budget", "flush_wait", "hooked", "shut_down")

    def __init__(self, budget: float, flush_wait: float = DEFAULT_STREAM_FLUSH_WAIT_SECONDS) -> None:
        self.budget = budget
        self.flush_wait = flush_wait
        # The provider's force_flush/shutdown end the spans of reads in flight.
        # Without that (no provider hook could be installed, or after
        # uninstrument()) a read could end its span after the processors
        # stopped, so none is made.
        self.hooked = False
        # The provider was shut down: its spans are no longer exported.
        self.shut_down = False


# -- shared worker state (reset in a forked child) -------------------------------------------


class _Lookup:
    """One name lookup in flight, shared by every read that needs that name."""

    __slots__ = ("future", "waiters")

    def __init__(self, future: Any) -> None:
        self.future = future
        self.waiters = 0


class _State:
    def __init__(self) -> None:
        # Re-entrant: a signal handler (register()'s shutdown) can settle reads
        # on a thread that is inside complete_stream().
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.pending: Set["_Read"] = set()
        # (entry, outcome) for the finisher, which ends spans and nothing else.
        self.inbox: "queue.SimpleQueue[Tuple[_Read, Optional[str]]]" = queue.SimpleQueue()
        # Admitted reads for the dispatcher, which hands them to the read loop.
        self.starts: "queue.SimpleQueue[_Read]" = queue.SimpleQueue()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_running = False
        self.dispatcher_running = False
        self.finisher_running = False
        self.started = False
        self.start_lock = threading.Lock()  # never waited on: see _ensure_workers
        # Held by the finisher while it ends a span, and by a fork (see _before_fork).
        self.finishing = threading.RLock()
        self.own_live = 0
        self.caller_live = 0
        self.caller_by: Dict[int, int] = {}  # live reads per caller transport
        self.caller_tasks: Dict[Any, "_Read"] = {}  # read tasks on a caller's loop
        self.own_tasks: Set[Any] = set()
        self.own_clients: "collections.OrderedDict[Tuple[Any, ...], _OwnClient]" = collections.OrderedDict()
        self.sync_jobs: "queue.SimpleQueue[_Read]" = queue.SimpleQueue()
        self.sync_threads = 0
        self.sync_idle = 0  # reader threads waiting for a job and not yet promised one
        self.resolve_jobs: "queue.SimpleQueue[Any]" = queue.SimpleQueue()
        self.resolver_threads = 0
        self.resolver_busy: Dict[int, float] = {}  # resolver thread -> when its lookup started
        self.lookups: Dict[Tuple[Any, ...], _Lookup] = {}
        self.dns_cache: "collections.OrderedDict[Tuple[Any, ...], Tuple[float, Any]]" = (
            collections.OrderedDict()
        )


_STATE = _State()
# Set on the package's own threads: a flush from one of them cannot wait for reads.
_WORKER = threading.local()
_FORK = threading.local()


def _before_fork() -> None:
    """Hold the finisher between two span ends while the process forks, so a
    child never inherits an exporter lock the finisher held mid-export (the
    child has no finisher to release it). Bounded by _FORK_WAIT_SECONDS."""
    try:
        _FORK.held = _STATE.finishing.acquire(timeout=_FORK_WAIT_SECONDS)
        _FORK.state = _STATE
    except Exception:  # pragma: no cover
        _FORK.held = False


def _after_fork_in_parent() -> None:
    if getattr(_FORK, "held", False):
        _FORK.held = False
        _guard(_FORK.state.finishing.release, None)


def _after_fork_in_child() -> None:
    """A forked child has none of the parent's worker threads. It starts its
    own when it needs them; the reads the parent had in flight are the
    parent's to end (their spans are skipped here, see ``_Call.finish``)."""
    global _STATE
    _FORK.held = False
    _STATE = _State()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork, after_in_parent=_after_fork_in_parent, after_in_child=_after_fork_in_child
    )


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


def _post(state: _State, entry: "_Read", outcome: Optional[str] = None) -> None:
    """Ask the finisher to end ``entry``'s span."""
    state.inbox.put((entry, outcome))


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
    """A response a transport built with its content already read (the
    transport has already held all of it in memory; it is checked here)."""
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


def _plain_settings(transport: Any) -> Optional[Tuple[Tuple[str, Any], ...]]:
    """What to build the package's own transport with, for a plain
    ``httpx.HTTPTransport`` / ``AsyncHTTPTransport`` (exactly that class, so
    nothing overrides how it sends) whose pool is a plain connection pool (no
    proxy): its TLS context, HTTP versions, connect retries, local address,
    Unix socket and socket options. None for any other transport."""
    if httpcore is None or type(transport) not in (httpx.HTTPTransport, httpx.AsyncHTTPTransport):
        return None
    pool = getattr(transport, "_pool", None)
    plain_pools = tuple(
        kind
        for kind in (getattr(httpcore, "ConnectionPool", None), getattr(httpcore, "AsyncConnectionPool", None))
        if kind is not None
    )
    if type(pool) not in plain_pools or getattr(pool, "_proxy", None) is not None:
        return None  # HTTPProxy and SOCKSProxy pools subclass the plain ones
    if not all(name in _ASYNC_TRANSPORT_PARAMETERS for name, _ in _PLAIN_SETTINGS):
        return None  # an httpx too old to build the same transport
    try:
        settings = tuple((name, getattr(pool, attribute)) for name, attribute in _PLAIN_SETTINGS)
    except AttributeError:
        return None
    if not isinstance(settings[0][1], ssl.SSLContext):
        return None
    return settings


def _caller_limit(transport: Any) -> int:
    """How many reads may borrow one of the caller's transports at once.

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
        "http",
        "url",
        "transport",
        "transport_key",
        "route",
        "routing",
        "limit",
        "extra",
        "poll_interval",
        "found",
        "last_status",
        "outcome",
        "loop",
        "attempts",
        "timer",
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
        extra: Dict[str, Any],
    ) -> None:
        self.state = state
        self.call = call
        self.collector = collector
        self.ended = ended
        self.deadline = deadline
        self.prediction_id = prediction_id
        self.http = http
        self.url = url
        self.extra = extra
        self.poll_interval = _poll_interval(client)
        self.transport: Any = None  # the caller's (sync and async routes)
        self.transport_key = 0
        self.route = "own"
        self.routing: Tuple[str, Tuple[Tuple[str, Any], ...]] = ("client", ())
        self.limit = _MAX_READS
        self.found: Any = None
        self.last_status: Optional[str] = None
        self.outcome: Optional[str] = None
        self.loop: Any = None  # the caller's loop, for an "async" read
        self.attempts = 0  # GETs made (or started) so far
        self.timer: Any = None  # the deadline timer on the read loop
        self._slot = False
        self._over = False

    # -- admission and release ---------------------------------------------------------

    def admit(self) -> bool:
        state = self.state
        with state.lock:
            if len(state.pending) >= _MAX_PENDING_SPANS:
                return False
            if self.route == "own":
                if state.own_live >= self.limit:
                    return False
                state.own_live += 1
            else:
                for task, entry in list(state.caller_tasks.items()):
                    if _guard(entry.loop.is_closed, False):  # its loop ended with the read pending
                        del state.caller_tasks[task]
                        entry.release()
                key = self.transport_key
                if state.caller_live >= _MAX_CALLER_READS_TOTAL or state.caller_by.get(key, 0) >= self.limit:
                    return False
                state.caller_live += 1
                state.caller_by[key] = state.caller_by.get(key, 0) + 1
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
                left = state.caller_by.get(self.transport_key, 1) - 1
                if left > 0:
                    state.caller_by[self.transport_key] = left
                else:
                    state.caller_by.pop(self.transport_key, None)

    def io_over(self) -> None:
        """The read's I/O has returned: free its slot and what only it needed."""
        self.release()
        self.http = None
        self.transport = None

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
        """End the span as of the stream's end with what is known; once. The
        span, its output and the read's answer are let go once it has ended
        (a timer may keep this object until its deadline)."""
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
            self.call = None
            self.collector = None
            self.found = None
            with state.lock:
                state.pending.discard(self)
                state.cond.notify_all()

    # -- the read itself -------------------------------------------------------------------

    def _take(self, data: Dict[str, Any]) -> bool:
        """Record one answer; True when it is final."""
        status = data.get("status")
        if status in TERMINAL_STATUSES:
            if not self._over:
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
        if remaining <= 0 or self._over:
            self.outcome = self.outcome or TIMEOUT
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
    own = _own_client(entry.state, entry.routing)
    own.reads += 1
    try:
        response = await own.http.send(request, stream=True)
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
    finally:
        own.reads -= 1
        _close_if_idle(entry.state, own)


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


async def _read_within_deadline(entry: _Read, send: Callable[[_Read, httpx.Request], Any]) -> None:
    """One read on an event loop, cancelled at its deadline (which closes its
    connection), then handed to the finisher. Instrumentation is suppressed.

    A cancel from outside (on a caller's loop, ``asyncio.run()`` cancelling
    the tasks left when its main coroutine returns) is never swallowed: the
    read ends there (``timeout``) and the cancel propagates, so the end of
    that loop does not wait for it.
    """
    token = context_api.attach(_suppressed())
    try:
        remaining = entry.deadline - _monotonic()
        if remaining <= 0:
            entry.outcome = entry.outcome or TIMEOUT
            return
        await _within(entry, entry.aread(send), remaining)
    except asyncio.CancelledError:
        entry.outcome = entry.outcome or TIMEOUT
        raise
    except asyncio.TimeoutError:
        entry.outcome = entry.outcome or TIMEOUT
    except Exception as error:
        entry.outcome = entry.outcome or _failure(error)
        logger.debug("traceai-replicate: could not read the streamed prediction", exc_info=True)
    finally:
        context_api.detach(token)
        if entry.route == "own" and entry.timer is not None:
            entry.timer.cancel()  # on the read loop, which owns the timer
        entry.io_over()
        _guard(lambda: _post(entry.state, entry), None)


# -- the package's event loop -----------------------------------------------------------------


class _ReadLoop(asyncio.SelectorEventLoop):
    """The package's event loop. Names are resolved on its own daemon threads
    (see :func:`_resolve`), never on the default executor, whose threads
    interpreter exit would wait for."""

    async def getaddrinfo(  # type: ignore[override]
        self, host: Any, port: Any, *, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0
    ) -> Any:
        try:
            ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return await _resolve(self, (host, port, family, type, proto, flags))
        return socket.getaddrinfo(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)


async def _resolve(loop: asyncio.AbstractEventLoop, query: Tuple[Any, ...]) -> Any:
    """A name lookup on the resolver threads.

    A result is kept for _DNS_CACHE_SECONDS. Reads that need the same name at
    the same time share one lookup. A lookup waits for a free resolver thread
    (the read's own deadline bounds that wait); only while every resolver
    thread has been stuck on one lookup for _RESOLVER_STUCK_SECONDS does a new
    one fail at once.
    """
    state = _STATE
    start = False
    with state.lock:
        now = _monotonic()
        cached = state.dns_cache.get(query)
        if cached is not None:
            if cached[0] > now:
                return cached[1]
            del state.dns_cache[query]
        lookup = state.lookups.get(query)
        if lookup is None:
            busy = state.resolver_busy
            if len(busy) >= _MAX_RESOLVERS and all(now - since > _RESOLVER_STUCK_SECONDS for since in busy.values()):
                raise OSError("traceai-replicate: name resolution is not answering")
            lookup = _Lookup(loop.create_future())
            state.lookups[query] = lookup
            start = state.resolver_threads < _MAX_RESOLVERS
            if start:
                state.resolver_threads += 1
            state.resolve_jobs.put((loop, lookup, query))
        lookup.waiters += 1
    try:
        if start:
            try:
                threading.Thread(target=_resolver_main, args=(state,), name=RESOLVER_NAME, daemon=True).start()
            except Exception as error:
                with state.lock:
                    state.resolver_threads -= 1
                    none_left = state.resolver_threads == 0
                if none_left:
                    _settle_lookup(state, query, lookup, False, OSError(str(error)))
        return await asyncio.shield(lookup.future)
    finally:
        with state.lock:
            lookup.waiters -= 1
            if lookup.waiters == 0 and not lookup.future.done():
                lookup.future.cancel()  # nobody waits for it: a resolver skips it
                if state.lookups.get(query) is lookup:
                    del state.lookups[query]


def _settle_lookup(state: _State, query: Tuple[Any, ...], lookup: _Lookup, ok: bool, value: Any) -> None:
    """On the read loop: hand a lookup's result to its waiters, and keep it."""
    with state.lock:
        if state.lookups.get(query) is lookup:
            del state.lookups[query]
        if ok:
            state.dns_cache[query] = (_monotonic() + _DNS_CACHE_SECONDS, value)
            state.dns_cache.move_to_end(query)
            while len(state.dns_cache) > _DNS_CACHE_ENTRIES:
                state.dns_cache.popitem(last=False)
    future = lookup.future
    if not future.done():
        if ok:
            future.set_result(value)
        else:
            future.set_exception(value)


def _resolver_main(state: _State) -> None:
    ident = threading.get_ident()
    while True:
        loop, lookup, query = state.resolve_jobs.get()
        if lookup.future.done():  # every read that wanted it has gone
            continue
        with state.lock:
            state.resolver_busy[ident] = _monotonic()
        try:
            try:
                ok, value = True, socket.getaddrinfo(*query)
            except Exception as error:
                ok, value = False, error
        finally:
            with state.lock:
                state.resolver_busy.pop(ident, None)
        try:
            loop.call_soon_threadsafe(_settle_lookup, state, query, lookup, ok, value)
        except RuntimeError:  # the loop is closed
            pass


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    _WORKER.active = True
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except BaseException:  # pragma: no cover - a daemon thread at interpreter exit
        logger.debug("traceai-replicate: the read loop stopped", exc_info=True)


def _loop(state: _State) -> asyncio.AbstractEventLoop:
    """The package's loop object (made once; its thread starts in _ensure_workers)."""
    loop = state.loop
    if loop is None:
        with state.lock:
            if state.loop is None:
                state.loop = _ReadLoop()
            loop = state.loop
    return loop


def _ensure_workers(state: _State) -> None:
    """Start the read loop's thread, the dispatcher and the finisher, once.

    ``instrument()`` does it, off the caller's path. In a forked child the
    first completed stream does; a stream completing meanwhile on another
    thread does not wait for that (its hand-off is already queued) and is
    served once the threads run.
    """
    if state.started:
        return
    if not state.start_lock.acquire(blocking=False):
        return
    try:
        if state.started:
            return
        loop = _loop(state)
        if not state.loop_running:
            threading.Thread(target=_run_loop, args=(loop,), name=LOOP_NAME, daemon=True).start()
            state.loop_running = True
        if not state.dispatcher_running:
            threading.Thread(target=_dispatcher_main, args=(state,), name=DISPATCHER_NAME, daemon=True).start()
            state.dispatcher_running = True
        if not state.finisher_running:
            threading.Thread(target=_finisher_main, args=(state,), name=FINISHER_NAME, daemon=True).start()
            state.finisher_running = True
        state.started = True
    finally:
        state.start_lock.release()


def start_workers() -> None:
    """Start the worker threads now (``instrument()``), not on a caller's path."""
    _guard(lambda: _ensure_workers(_STATE), None)


class _OwnClient:
    """A package-owned httpx client, and the reads using it right now."""

    __slots__ = ("http", "reads", "retired")

    def __init__(self, http: Any) -> None:
        self.http = http
        self.reads = 0
        self.retired = False  # dropped from the cache; closed once its last read returns


def _own_client(state: _State, routing: Tuple[str, Tuple[Tuple[str, Any], ...]]) -> _OwnClient:
    """The package's httpx client for these routing options (on the package's loop only).

    At most _MAX_OWN_CLIENTS are kept; the least recently used one is dropped
    beyond that and closed once no read uses it any more (an app that builds
    a client per call, each with its own TLS context, gets one per call)."""
    kind, values = routing
    key = (kind,) + tuple((name, repr(value)) for name, value in values)
    own = state.own_clients.get(key)
    if own is not None and not own.http.is_closed:
        state.own_clients.move_to_end(key)
        return own
    limits = httpx.Limits(max_connections=_POOL_CONNECTIONS, max_keepalive_connections=_POOL_KEEPALIVE)
    if kind == "transport":
        transport = httpx.AsyncHTTPTransport(limits=limits, trust_env=False, **dict(values))
        own = _OwnClient(httpx.AsyncClient(transport=transport, trust_env=False))
    else:
        own = _OwnClient(httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(limits=limits), **dict(values)))
    state.own_clients[key] = own
    while len(state.own_clients) > _MAX_OWN_CLIENTS:
        _, old = state.own_clients.popitem(last=False)
        old.retired = True
        _close_if_idle(state, old)
    return own


def _close_if_idle(state: _State, own: _OwnClient) -> None:
    if own.retired and own.reads == 0 and not own.http.is_closed:
        _keep(state.own_tasks, asyncio.ensure_future(own.http.aclose()))


def _keep(tasks: Set[Any], task: Any) -> None:
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _deadline_passed(entry: _Read) -> None:
    if not entry._over:
        _post(entry.state, entry, TIMEOUT)


def _on_read_loop(entries: List[_Read]) -> None:
    """On the package's loop: for each read, arm its deadline, then start it."""
    loop = asyncio.get_running_loop()
    for entry in entries:
        state = entry.state
        try:
            entry.timer = loop.call_at(entry.deadline, _deadline_passed, entry)
            if entry.route == "own":
                _keep(state.own_tasks, loop.create_task(_read_within_deadline(entry, _send_own)))
            elif entry.route == "sync":
                _ensure_sync_reader(state)
                state.sync_jobs.put(entry)
            # "async" runs on the caller's loop already.
        except Exception:
            logger.debug("traceai-replicate: could not start the read", exc_info=True)
            entry.outcome = entry.outcome or FAILED
            entry.io_over()
            _post(state, entry)


def _dispatcher_main(state: _State) -> None:
    """Pass admitted reads to the read loop, everything queued at once, with
    one wake-up of the loop per batch."""
    _WORKER.active = True
    loop = _loop(state)
    while True:
        batch = [state.starts.get()]
        try:
            while len(batch) < _MAX_READS:
                batch.append(state.starts.get_nowait())
        except queue.Empty:
            pass
        try:
            loop.call_soon_threadsafe(_on_read_loop, batch, context=contextvars.Context())
        except Exception:  # pragma: no cover - the loop is gone (interpreter exit)
            logger.debug("traceai-replicate: could not start reads", exc_info=True)
            for entry in batch:
                entry.outcome = entry.outcome or FAILED
                entry.io_over()
                _post(state, entry)


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
            entry.io_over()
            _guard(lambda: _post(state, entry), None)
            with state.lock:
                state.sync_idle += 1


def _ensure_sync_reader(state: _State) -> None:
    """On the read loop: promise the next job an idle reader thread, or start
    one, at most _MAX_CALLER_READS_TOTAL of them (admission keeps live reads
    under that, so a job never waits behind another)."""
    with state.lock:
        if state.sync_idle > 0:
            state.sync_idle -= 1
            return
        start = state.sync_threads < _MAX_CALLER_READS_TOTAL
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


def _finisher_main(state: _State) -> None:
    """End spans: when their read returns, and at each deadline. Nothing else
    runs here, so an inline exporter delays only other span ends."""
    _WORKER.active = True
    while True:
        try:
            entry, outcome = state.inbox.get()
            with state.finishing:
                entry.end(outcome)
        except Exception:  # pragma: no cover - never let the finisher die
            logger.debug("traceai-replicate: stream finisher step failed", exc_info=True)


def _start_on_callers_loop(entry: _Read) -> None:
    """An async stream through the caller's own async-only transport: a task
    on the caller's running loop (its transport's connections belong to it),
    in a fresh context."""
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
        # after the stream): its body never ran, so free its slot, say why
        # there is no read, and end it here.
        with state.lock:
            state.caller_tasks.pop(done, None)
        if entry.found is None and entry.outcome is None:
            entry.outcome = SKIPPED if entry.attempts == 0 else TIMEOUT
        entry.io_over()
        _guard(lambda: _post(state, entry), None)

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
    """A read to admit, SKIPPED, or None when no read is wanted."""
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
    entry = _Read(
        _STATE, call, collector, ended, _monotonic() + reads.budget, prediction_id, client, http, url, extra
    )
    options = getattr(client, "_client_kwargs", None) or {}
    if options.get("transport") is None and not options.get("mounts"):
        entry.routing = ("client", _routing(client))
        return entry
    transport = _unwrap_retries(http._transport_for_url(url))
    settings = _plain_settings(transport)
    if settings is not None:
        entry.routing = ("transport", settings)
        return entry
    # Any other transport of the caller's: sent synchronously on a reader
    # thread when it can be (also for an async stream), else on the caller's
    # running loop.
    entry.transport = transport
    entry.transport_key = id(transport)
    entry.route = "sync" if _sends_sync(transport) else "async"
    entry.limit = _caller_limit(transport)
    if entry.route == "async":
        asyncio.get_running_loop()  # RuntimeError under another event loop library: no read
    return entry


def _hand_over(entry: _Read) -> None:
    """Give an admitted read to the workers: one ``SimpleQueue.put`` (plus,
    for an async-only caller transport, a task on the caller's loop). Never
    waits and never releases the GIL; starts no thread unless the workers
    have not been started in this process (a forked child)."""
    state = entry.state
    if entry.route == "async":
        _start_on_callers_loop(entry)
    state.starts.put(entry)
    _ensure_workers(state)


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
    handed to the read loop (see :func:`_hand_over`) and the finisher ends
    the span. Nothing here waits on I/O. No Exception escapes; a
    BaseException (KeyboardInterrupt) at any step ends the span (and frees
    the read's slot) first, then propagates.
    """
    ended = time.time_ns()
    if call.completed or call.finished:
        return
    call.completed = True
    extra = stream_attributes(done) if call.stream else {}
    entry: Optional[_Read] = None

    def end_without_read() -> None:
        if entry is not None:
            entry.outcome = entry.outcome or SKIPPED
            entry.release()
            entry.end()
        else:
            wanted = reads is not None and reads.budget > 0 and call.stream
            reason = dict(extra, **{STREAM_READ: SKIPPED}) if wanted else extra
            call.finish(collector=collector, end_time=ended, extra=dict(reason, **_status_from_stream(extra, None)))

    try:
        planned = _guard(lambda: _plan(call, collector, reads, ended, extra, is_async), SKIPPED)
        if isinstance(planned, _Read):
            entry = planned  # held before admission, so an interrupt in admit() frees its slot
            if not entry.admit():
                entry, planned = None, SKIPPED
        if entry is None:
            if planned is not None:
                extra = dict(extra, **{STREAM_READ: planned})
            call.finish(collector=collector, end_time=ended, extra=dict(extra, **_status_from_stream(extra, None)))
            return
        _hand_over(entry)
    except Exception:  # e.g. no new thread at interpreter shutdown: end it without the read
        logger.debug("traceai-replicate: could not start the read after a stream", exc_info=True)
        _guard(end_without_read, None)
    except BaseException:  # KeyboardInterrupt at any step: the span ends first
        _guard(end_without_read, None)
        raise


# -- flush, shutdown and exit ----------------------------------------------------------------


def _wait(cond: threading.Condition, timeout: float) -> None:
    cond.wait(timeout)


def settle_stream_reads(until: Optional[float] = None, outcome: str = TIMEOUT) -> bool:
    """End the span of every completed stream whose read is still running.

    Reads in flight are waited for, no longer than the last of their deadlines
    (plus a short grace for the finisher) and, when ``until`` (a monotonic
    time) is given, no later than that; then each span still waiting ends
    with what is known (``replicate.stream.read`` = ``outcome``: ``timeout``
    at shutdown and exit, ``flushed`` for a flush). Returns False when
    ``until`` cut a read short. On the finisher's or the read loop's own
    thread nothing is waited for. A KeyboardInterrupt (or any BaseException)
    while waiting ends every pending span first, then propagates.
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
                _guard(lambda: entry.end(outcome), None)
    try:
        with state.lock:
            while any(entry in state.pending for entry in pending):
                remaining = cut - _monotonic()
                if remaining <= 0:
                    break
                _wait(state.cond, remaining)
    except BaseException:
        for entry in pending:
            _guard(lambda: entry.end(outcome), None)
        raise
    in_time = True
    for entry in pending:
        if entry in state.pending:
            in_time = in_time and cut >= limit
            _guard(lambda: entry.end(outcome), None)
    return in_time
