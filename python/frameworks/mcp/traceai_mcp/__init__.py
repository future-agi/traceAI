from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from copy import copy
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Collection, Tuple, cast
from weakref import ref

from opentelemetry import context, propagate
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from traceai_mcp.package import _instruments
from traceai_mcp._attachments import capture_mcp_result, mcp_attachment_capabilities, wrap_mcp_client
from wrapt import ObjectProxy, register_post_import_hook, wrap_function_wrapper


__all__ = ["MCPInstrumentor", "capture_mcp_result", "mcp_attachment_capabilities", "wrap_mcp_client"]


class MCPInstrumentor(BaseInstrumentor):  # type: ignore
    """
    An instrumenter for MCP.

    It propagates trace context over MCP transports only and never captures
    media. Use ``wrap_mcp_client(session)`` for tool and resource media.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        # Hooks can remain pending after uninstrument(), so associate each one
        # with this instrumentation generation and ignore stale callbacks.
        generation = object()
        # Existing sessions retain their stream proxies across installations.
        # Share one switch across generations so reinstall reactivates them.
        if not hasattr(self, "_stream_state"):
            self._stream_state = _StreamState()
        self._stream_state.active = True
        self._hook_generation = generation
        self._wrapped_targets = []

        if not hasattr(self, "_pending_hooks"):
            self._pending_hooks = set()

        def register(module_name: str, names: Tuple[str, ...], wrapper_name: str) -> None:
            # wrapt offers no public hook removal API. Reuse a single pending
            # hook per module across enable cycles and hold only a weak owner.
            if module_name in self._pending_hooks:
                return
            self._pending_hooks.add(module_name)
            owner_ref = ref(self)

            def on_import(module: Any) -> None:
                instrumentor = owner_ref()
                if instrumentor is None:
                    return
                instrumentor._pending_hooks.discard(module_name)
                current_generation = instrumentor._hook_generation
                if current_generation is None:
                    return
                wrapper = getattr(instrumentor, wrapper_name)
                for name in names:
                    owner = module
                    parts = name.split(".")
                    for part in parts[:-1]:
                        owner = getattr(owner, part, None)
                        if owner is None:
                            break
                    if owner is None or parts[-1] not in vars(owner):
                        continue
                    attr = parts[-1]
                    original = vars(owner)[attr]

                    # A foreign wrapper may retain ours after teardown.
                    def dispatch(wrapped: Callable[..., Any], instance: Any, args: Any, kwargs: Any) -> Any:
                        if instrumentor._hook_generation is not current_generation:
                            return wrapped(*args, **kwargs)
                        return wrapper(wrapped, instance, args, kwargs)

                    wrap_function_wrapper(module, name, dispatch)
                    installed = vars(owner)[attr]
                    instrumentor._wrapped_targets.append((owner, attr, original, installed))
            register_post_import_hook(on_import, module_name)

        register("mcp.client.streamable_http", ("streamable_http_client", "streamablehttp_client"), "_wrap_transport_with_callback")
        register("mcp.server.streamable_http", ("StreamableHTTPServerTransport.connect",), "_wrap_plain_transport")
        register("mcp.client.sse", ("sse_client",), "_wrap_plain_transport")
        register("mcp.server.sse", ("SseServerTransport.connect_sse",), "_wrap_plain_transport")
        register("mcp.client.stdio", ("stdio_client",), "_wrap_plain_transport")
        register("mcp.server.stdio", ("stdio_server",), "_wrap_plain_transport")
        register("mcp.server.session", ("ServerSession.__init__",), "_base_session_init_wrapper")

    def _uninstrument(self, **kwargs: Any) -> None:
        self._hook_generation = None
        self._stream_state.active = False
        for owner, attr, original, installed in reversed(self._wrapped_targets):
            # Do not erase wrappers installed by another instrumentor later.
            if vars(owner).get(attr) is installed:
                setattr(owner, attr, original)
        self._wrapped_targets = []

    def _wrap_stream(self, stream: Any, proxy_type: type["_SwitchableProxy"]) -> Any:
        # Legacy transport aliases may delegate to another wrapped entry point.
        # Reuse only our own proxies; other owners keep their lifecycle state.
        if isinstance(stream, proxy_type) and stream._self_state is self._stream_state:
            return stream
        return proxy_type(stream, self._stream_state)

    @asynccontextmanager
    async def _wrap_transport_with_callback(
        self, wrapped: Callable[..., Any], instance: Any, args: Any, kwargs: Any
    ) -> AsyncGenerator[
        Tuple["InstrumentedStreamReader", "InstrumentedStreamWriter", Any], None
    ]:
        async with wrapped(*args, **kwargs) as streams:
            reader = self._wrap_stream(streams[0], InstrumentedStreamReader)
            try:
                yield (
                    reader,
                    self._wrap_stream(streams[1], InstrumentedStreamWriter),
                    *streams[2:],
                )
            finally:
                reader._restore_context()

    @asynccontextmanager
    async def _wrap_plain_transport(
        self, wrapped: Callable[..., Any], instance: Any, args: Any, kwargs: Any
    ) -> AsyncGenerator[
        Tuple["InstrumentedStreamReader", "InstrumentedStreamWriter"], None
    ]:
        async with wrapped(*args, **kwargs) as (read_stream, write_stream):
            reader = self._wrap_stream(read_stream, InstrumentedStreamReader)
            try:
                yield (
                    reader,
                    self._wrap_stream(write_stream, InstrumentedStreamWriter),
                )
            finally:
                reader._restore_context()

    def _base_session_init_wrapper(
        self, wrapped: Callable[..., None], instance: Any, args: Any, kwargs: Any
    ) -> None:
        wrapped(*args, **kwargs)
        reader = getattr(instance, "_incoming_message_stream_reader", None)
        writer = getattr(instance, "_incoming_message_stream_writer", None)
        if reader and writer:
            setattr(
                instance,
                "_incoming_message_stream_reader",
                ContextAttachingStreamReader(reader, self._stream_state),
            )
            setattr(
                instance,
                "_incoming_message_stream_writer",
                ContextSavingStreamWriter(writer, self._stream_state),
            )


class _StreamState:
    active = True


class _SwitchableProxy(ObjectProxy):  # type: ignore
    def __init__(self, wrapped: Any, state: Any = None) -> None:
        super().__init__(wrapped)
        self._self_state = state if state is not None else _StreamState()


# A token from this marker proves ownership of the exact Context, not just
# the task: copied contexts inherit values but cannot reset each other's tokens.
_context_owner: ContextVar[None] = ContextVar("traceai_mcp_context_owner", default=None)


class _ContextIterator:
    def __init__(self, reader: "_ContextStreamReader", wrapped: Any) -> None:
        self._reader = reader
        self._wrapped = wrapped
        self._key = object()
        self._closed = False

    def __aiter__(self) -> "_ContextIterator":
        return self

    async def __anext__(self) -> Any:
        if self._closed:
            # Another Context can close this iterator without restoring the
            # owner's tokens. Finish cleanup when the owner observes EOF,
            # but do not detach an attachment from a newer iterator.
            self._reader._restore_context(self._key)
            raise StopAsyncIteration
        # A caller may start a fresh async-for after breaking out of the last
        # one. Its previous message is finished even if its iterator was lost.
        self._reader._restore_context()
        try:
            item = await self._wrapped.__anext__()
            item, ctx = self._reader._prepare_item(item)
        except BaseException:
            self._closed = True
            raise
        if ctx is not None:
            self._reader._attach_context(self._key, ctx)
        return item

    async def aclose(self) -> None:
        self._reader._restore_context(self._key)
        self._closed = True


class _ContextStreamReader(_SwitchableProxy):
    def __init__(self, wrapped: Any, state: Any = None) -> None:
        super().__init__(wrapped, state)
        self._self_attachment: ContextVar[tuple[object, Token, Token] | None] = ContextVar(
            "traceai_mcp_stream_context", default=None
        )

    def _attach_context(self, key: object, ctx: context.Context) -> None:
        restore = context.attach(ctx)
        owner = _context_owner.set(None)
        self._self_attachment.set((key, restore, owner))

    def _restore_context(self, key: Any = None) -> None:
        attachment = self._self_attachment.get()
        if attachment is None:
            return
        iterator_key, restore, owner = attachment
        if key is not None and iterator_key is not key:
            return
        self._self_attachment.set(None)
        try:
            _context_owner.reset(owner)
        except (ValueError, RuntimeError):
            # A different Context may close the shared native stream. It
            # drops only its inherited bookkeeping; the consuming Context
            # still owns its tokens and restores them on its own exit.
            return
        context.detach(restore)

    def __aiter__(self) -> _ContextIterator:
        # Never suspend a generator with context attached: async-generator GC
        # may run in a different task, and async-for break does not close it.
        return _ContextIterator(self, self.__wrapped__.__aiter__())

    def _prepare_item(self, item: Any) -> Tuple[Any, Any]:
        raise NotImplementedError

    async def __aenter__(self) -> Any:
        await self.__wrapped__.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        self._restore_context()
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    def close(self) -> Any:
        self._restore_context()
        return self.__wrapped__.close()

    async def aclose(self) -> Any:
        # Restore synchronously, before a cancellation checkpoint in native IO.
        self._restore_context()
        return await self.__wrapped__.aclose()


class InstrumentedStreamReader(_ContextStreamReader):
    def _prepare_item(self, item: Any) -> Tuple[Any, Any]:
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCRequest

        # SDK receive streams carry transport exceptions as values.
        if not self._self_state.active or isinstance(item, Exception):
            return item, None
        session_message = cast(SessionMessage, item)
        message = session_message.message
        # MCP 1.x uses a JSONRPCMessage root model; 2.x stores the
        # concrete JSONRPC variant directly.
        request = getattr(message, "root", message)
        if isinstance(request, JSONRPCRequest) and request.params:
            meta = request.params.get("_meta")
            if meta:
                return item, propagate.extract(meta)
        return item, None


class InstrumentedStreamWriter(_SwitchableProxy):
    # ObjectProxy missing context manager - https://github.com/GrahamDumpleton/wrapt/issues/73
    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def send(self, item: Any) -> Any:
        from mcp.shared.message import SessionMessage
        from mcp.types import JSONRPCRequest

        if not self._self_state.active or isinstance(item, Exception):
            return await self.__wrapped__.send(item)
        session_message = cast(SessionMessage, item)
        message = session_message.message
        request = getattr(message, "root", message)
        if not isinstance(request, JSONRPCRequest):
            return await self.__wrapped__.send(item)
        carrier: dict = {}
        propagate.get_global_textmap().inject(carrier)
        if not carrier:
            return await self.__wrapped__.send(item)
        # Copy only the envelope and the metadata path: arbitrary request
        # payloads and transport metadata retain their native identity.
        params = dict(request.params or {})
        original_meta = params.get("_meta")
        meta = {} if original_meta is None else dict(original_meta)
        if "_meta" in params and all(key in meta for key in carrier):
            return await self.__wrapped__.send(item)
        for key, value in carrier.items():
            meta.setdefault(key, value)
        params["_meta"] = meta
        request_copy = request.model_copy(update={"params": params})
        if message is request:
            message_copy = request_copy
        else:
            message_copy = message.model_copy(update={"root": request_copy})
        session_copy = copy(session_message)
        session_copy.message = message_copy
        return await self.__wrapped__.send(session_copy)


@dataclass(slots=True, frozen=True)
class ItemWithContext:
    item: Any
    ctx: context.Context


class ContextSavingStreamWriter(_SwitchableProxy):
    async def __aenter__(self) -> Any:
        return await self.__wrapped__.__aenter__()

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> Any:
        return await self.__wrapped__.__aexit__(exc_type, exc_value, traceback)

    async def send(self, item: Any) -> Any:
        if not self._self_state.active:
            return await self.__wrapped__.send(item)
        ctx = context.get_current()
        return await self.__wrapped__.send(ItemWithContext(item, ctx))


class ContextAttachingStreamReader(_ContextStreamReader):
    def _prepare_item(self, item: Any) -> Tuple[Any, Any]:
        # Items can cross a lifecycle transition while queued. Always
        # unwrap our envelope, but attach only while currently enabled.
        if isinstance(item, ItemWithContext):
            return item.item, item.ctx if self._self_state.active else None
        return item, None
