"""The wrap around ``Realtime.connect`` and ``AsyncRealtime.connect``.

``connect()`` returns a connection manager. The wrapper hands back a proxy of
that manager (``isinstance`` still holds) whose enter installs four hooks on the
connection object the SDK yields:

* ``recv_bytes``: every server frame passes through it, whether the caller
  iterates the connection, calls ``recv()``, ``recv_bytes()`` or
  ``dispatch_events()``. A websockets close raised here is the socket close.
* ``send`` and ``send_raw``: every client event, including the typed helpers
  (``connection.session.update(...)`` and so on), which call ``send``.
* ``close``: the caller ended the session.

Events queued on the manager before the socket opened are observed when they
are queued and replayed into the session once it exists.

The hooks never change what the SDK returns or raises. Nothing here opens a
socket, retries one, or calls OpenAI.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import wrapt
from fi_instrumentation import get_attributes_from_context

from ._session import SessionRecorder, Settings

logger = logging.getLogger(__name__)

_GPT_LIVE = re.compile(r"/live(?:/|$)")


class SessionFactory:
    """Everything a new session needs, captured once at ``instrument()``."""

    def __init__(self, tracer: Any, settings: Settings, provider: Any, registry: "set[SessionRecorder]") -> None:
        self.tracer = tracer
        self.settings = settings
        self.provider = provider
        self.registry = registry

    def new(self, model: Optional[str], client: Any, *, is_async: bool) -> SessionRecorder:
        return SessionRecorder(
            self.tracer,
            self.settings,
            model=model,
            secrets=_client_secrets(client),
            context_attributes=dict(get_attributes_from_context()),
            flush=self._flush_async if is_async else self._flush_sync,
            on_finish=self.registry.discard,
        )

    def _flush_sync(self) -> None:
        flush = getattr(self.provider, "force_flush", None)
        if callable(flush):
            flush()

    def _flush_async(self) -> None:
        # Hand the flush to the default executor so the event loop (and every other
        # session on it) is not blocked while the exporter sends.
        flush = getattr(self.provider, "force_flush", None)
        if not callable(flush):
            return
        try:
            asyncio.get_running_loop().run_in_executor(None, flush)
        except RuntimeError:
            flush()


def realtime_path(client: Any) -> str:
    """The path ``connect()`` will open, as the SDK builds it: the websocket base
    URL (or the HTTP base URL) plus ``/realtime``. Never the host or the query."""
    base = getattr(client, "websocket_base_url", None) or getattr(client, "base_url", None) or ""
    try:
        path = urlsplit(str(base)).path
    except ValueError:
        path = ""
    return path.rstrip("/") + "/realtime"


def _client_secrets(client: Any) -> Tuple[str, ...]:
    out = []
    for attr in ("api_key", "_api_key"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value:
            out.append(value)
    return tuple(out)


def _model_arg(kwargs: Dict[str, Any]) -> Optional[str]:
    model = kwargs.get("model")
    return model if isinstance(model, str) and model else None


class ConnectWrapper:
    """wrapt wrapper for ``Realtime.connect`` (sync) or ``AsyncRealtime.connect``."""

    def __init__(self, factory: SessionFactory, *, is_async: bool) -> None:
        self._factory = factory
        self._is_async = is_async

    def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        manager = wrapped(*args, **kwargs)
        try:
            client = getattr(instance, "_client", None)
            if _GPT_LIVE.search(realtime_path(client)):
                logger.warning(
                    "traceai-openai-realtime: this connect() points at a GPT-Live endpoint (/live/...). "
                    "GPT-Live is not instrumented; the session runs untraced."
                )
                return manager
            proxy = AsyncManagerProxy if self._is_async else SyncManagerProxy
            return proxy(manager, self._factory, _model_arg(kwargs), client)
        except Exception:
            logger.debug("traceai-openai-realtime: connect() left unwrapped", exc_info=True)
            return manager


class _ManagerProxyBase(wrapt.ObjectProxy):  # type: ignore[misc]
    def __init__(self, wrapped: Any, factory: SessionFactory, model: Optional[str], client: Any) -> None:
        super().__init__(wrapped)
        self._self_factory = factory
        self._self_model = model
        self._self_client = client
        self._self_queued: List[Tuple[int, Any]] = []
        self._self_recorder: Optional[SessionRecorder] = None

    def send(self, event: Any) -> None:
        """Queue an event before the socket opens (SDK 3.x). Observed when queued."""
        result = self.__wrapped__.send(event)
        self._self_queued.append((time.time_ns(), event))
        return result

    # SDK 3.x handler registration returns the manager for chaining. Return this
    # proxy instead, so `async with client.realtime.connect(...).on(...)` stays traced.
    def on(self, *args: Any, **kwargs: Any) -> Any:
        return self._chain(self.__wrapped__.on(*args, **kwargs))

    def once(self, *args: Any, **kwargs: Any) -> Any:
        return self._chain(self.__wrapped__.once(*args, **kwargs))

    def off(self, *args: Any, **kwargs: Any) -> Any:
        return self._chain(self.__wrapped__.off(*args, **kwargs))

    def _chain(self, result: Any) -> Any:
        return self if result is self.__wrapped__ else result

    def _new_recorder(self) -> SessionRecorder:
        recorder = self._self_factory.new(self._self_model, self._self_client, is_async=self._is_async)
        return recorder

    def _opened(self, recorder: SessionRecorder, connection: Any, start_ns: int) -> None:
        recorder.start(start_ns)
        self._self_factory.registry.add(recorder)
        for ts, event in self._self_queued:
            recorder.on_client_event(event, ts)
        self._self_queued = []
        self._self_recorder = recorder

    @staticmethod
    def _exit_reason(exc: BaseException) -> str:
        if isinstance(exc, asyncio.CancelledError):
            return "task_cancelled"
        if type(exc).__name__ in ("ConnectionClosedError", "WebSocketConnectionClosedError"):
            return "socket_closed"
        return "exception"


class SyncManagerProxy(_ManagerProxyBase):
    _is_async = False

    def __enter__(self) -> Any:
        recorder = self._new_recorder()
        start_ns = time.time_ns()
        try:
            connection = self.__wrapped__.__enter__()
        except BaseException as exc:
            recorder.connect_failed(exc, start_ns)
            raise
        try:
            install_sync(connection, recorder)
            self._opened(recorder, connection, start_ns)
        except Exception:
            logger.debug("traceai-openai-realtime: session not traced", exc_info=True)
        return connection

    enter = __enter__

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> Any:
        recorder = self._self_recorder
        if recorder is not None and exc is not None:
            recorder.finish(self._exit_reason(exc), exc=exc)
        try:
            return self.__wrapped__.__exit__(exc_type, exc, tb)
        finally:
            if recorder is not None:
                recorder.finish("client_closed")


class AsyncManagerProxy(_ManagerProxyBase):
    _is_async = True

    async def __aenter__(self) -> Any:
        recorder = self._new_recorder()
        start_ns = time.time_ns()
        try:
            connection = await self.__wrapped__.__aenter__()
        except BaseException as exc:
            recorder.connect_failed(exc, start_ns)
            raise
        try:
            install_async(connection, recorder)
            self._opened(recorder, connection, start_ns)
        except Exception:
            logger.debug("traceai-openai-realtime: session not traced", exc_info=True)
        return connection

    enter = __aenter__

    async def __aexit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> Any:
        recorder = self._self_recorder
        if recorder is not None and exc is not None:
            recorder.finish(self._exit_reason(exc), exc=exc)
        try:
            return await self.__wrapped__.__aexit__(exc_type, exc, tb)
        finally:
            if recorder is not None:
                recorder.finish("client_closed")


def _reconnect_possible(connection: Any) -> bool:
    # SDK 3.x reconnects only from the iterator, and only when the caller passed
    # on_reconnecting (the manager then hands the connection a socket factory).
    return getattr(connection, "_on_reconnecting", None) is not None and getattr(connection, "_make_ws", None) is not None


def install_sync(connection: Any, recorder: SessionRecorder) -> None:
    recv_bytes = connection.recv_bytes
    send = connection.send
    send_raw = getattr(connection, "send_raw", None)
    close = connection.close

    def traced_recv_bytes() -> bytes:
        try:
            data = recv_bytes()
        except BaseException as exc:
            recorder.on_socket_error(exc, reconnect_possible=_reconnect_possible(connection))
            raise
        recorder.on_server_frame(data)
        return data

    def traced_send(event: Any) -> None:
        result = send(event)
        recorder.on_client_event(event)
        return result

    def traced_send_raw(data: Any) -> None:
        result = send_raw(data)
        recorder.on_client_event(data)
        return result

    def traced_close(*args: Any, **kwargs: Any) -> None:
        recorder.mark_client_close()
        try:
            return close(*args, **kwargs)
        finally:
            recorder.finish("client_closed")

    connection.recv_bytes = traced_recv_bytes
    connection.send = traced_send
    if callable(send_raw):
        connection.send_raw = traced_send_raw
    connection.close = traced_close
    connection._fi_realtime_session = recorder


def install_async(connection: Any, recorder: SessionRecorder) -> None:
    recv_bytes = connection.recv_bytes
    send = connection.send
    send_raw = getattr(connection, "send_raw", None)
    close = connection.close

    async def traced_recv_bytes() -> bytes:
        try:
            data = await recv_bytes()
        except BaseException as exc:
            recorder.on_socket_error(exc, reconnect_possible=_reconnect_possible(connection))
            raise
        recorder.on_server_frame(data)
        return data

    async def traced_send(event: Any) -> None:
        result = await send(event)
        recorder.on_client_event(event)
        return result

    async def traced_send_raw(data: Any) -> None:
        result = await send_raw(data)
        recorder.on_client_event(data)
        return result

    async def traced_close(*args: Any, **kwargs: Any) -> None:
        recorder.mark_client_close()
        try:
            return await close(*args, **kwargs)
        finally:
            recorder.finish("client_closed")

    connection.recv_bytes = traced_recv_bytes
    connection.send = traced_send
    if callable(send_raw):
        connection.send_raw = traced_send_raw
    connection.close = traced_close
    connection._fi_realtime_session = recorder
