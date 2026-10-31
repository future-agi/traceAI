"""Lazy stream proxies: trace only SDK work, never the consumer's loop body."""

import logging
from contextlib import contextmanager
from inspect import isawaitable
from typing import Any, Callable, Iterator, Optional

import opentelemetry.context as context_api
from opentelemetry import trace
from wrapt import ObjectProxy

from traceai_portkey._request_attributes_extractor import get_attribute
from traceai_portkey._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


@contextmanager
def _suppress_nested() -> Iterator[None]:
    token = context_api.attach(
        context_api.set_value(context_api._SUPPRESS_INSTRUMENTATION_KEY, True)
    )
    try:
        yield
    finally:
        context_api.detach(token)


class _Accumulator:
    """Keep the completion, not an additional copy of every streamed chunk."""

    def __init__(self) -> None:
        self.response = {"choices": []}
        self.choices = {}
        self.tools = {}

    def add(self, chunk: Any) -> None:
        for key in ("model", "usage"):
            value = get_attribute(chunk, key)
            if value is not None:
                self.response[key] = value
        for choice in get_attribute(chunk, "choices", ()) or ():
            index = get_attribute(choice, "index", 0)
            message = self.choices.setdefault(index, {"role": "assistant"})
            delta = get_attribute(choice, "delta") or get_attribute(choice, "message")
            if delta is None:
                # Saved prompts may use a text-completion model rather than chat.
                text = get_attribute(choice, "text")
                if isinstance(text, str):
                    message["content"] = message.get("content", "") + text
                continue
            for key in ("role", "content"):
                value = get_attribute(delta, key)
                if isinstance(value, str):
                    message[key] = (
                        (message.get(key, "") + value) if key == "content" else value
                    )
            function = get_attribute(delta, "function_call")
            if function:
                self._merge_function(message.setdefault("function_call", {}), function)
            for tool in get_attribute(delta, "tool_calls", ()) or ():
                tool_index = get_attribute(tool, "index", 0)
                tools = self.tools.setdefault(index, {})
                target = tools.setdefault(
                    tool_index, {"type": "function", "function": {}}
                )
                if tool_id := get_attribute(tool, "id"):
                    target["id"] = tool_id
                if function := get_attribute(tool, "function"):
                    self._merge_function(target["function"], function)
                message["tool_calls"] = [tools[i] for i in sorted(tools)]
        self.response["choices"] = [
            {"index": i, "message": self.choices[i]} for i in sorted(self.choices)
        ]

    @staticmethod
    def _merge_function(target, function) -> None:
        for key in ("name", "arguments"):
            value = get_attribute(function, key)
            if isinstance(value, str):
                target[key] = target.get(key, "") + value


class _Stream(ObjectProxy):
    def __init__(
        self,
        response: Any,
        span: _WithSpan,
        finish: Callable[[_WithSpan, Any, trace.Status], None],
    ) -> None:
        super().__init__(response)
        self._self_span = span
        self._self_finish = finish
        self._self_accumulator = _Accumulator()

    @contextmanager
    def _context(self) -> Iterator[None]:
        with self._self_span.use_span(), _suppress_nested():
            yield

    def _finish(
        self, error: Optional[BaseException] = None, exhausted: bool = False
    ) -> None:
        if self._self_span.is_finished:
            return
        if error is not None:
            self._self_span.record_exception(error)
            status = trace.Status(
                trace.StatusCode.ERROR, f"{type(error).__name__}: {error}"
            )
        else:
            # Closing early is not evidence of a successful completion.
            status = trace.Status(
                trace.StatusCode.OK if exhausted else trace.StatusCode.UNSET
            )
        self._self_finish(self._self_span, self._self_accumulator.response, status)

    def _add(self, chunk: Any) -> None:
        try:
            self._self_accumulator.add(chunk)
        except Exception as error:
            # Fail open, without logging response content from the exception.
            logger.warning(
                "Failed to accumulate Portkey chunk (%s)", type(error).__name__
            )

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> Any:
        return self._advance(next, self.__wrapped__)

    def send(self, value: Any) -> Any:
        return self._advance(self.__wrapped__.send, value)

    def throw(self, *args: Any) -> Any:
        return self._advance(self.__wrapped__.throw, *args)

    def _advance(self, method, *args) -> Any:
        try:
            with self._context():
                chunk = method(*args)
        except StopIteration:
            self._finish(exhausted=True)
            raise
        except BaseException as error:
            self._finish(error=error)
            raise
        self._add(chunk)
        return chunk

    def close(self) -> Any:
        try:
            with self._context():
                close = getattr(self.__wrapped__, "close", None)
                result = close() if close else None
        except BaseException as error:
            self._finish(error=error)
            raise
        self._finish()
        return result

    def __enter__(self) -> "_Stream":
        try:
            with self._context():
                enter = getattr(self.__wrapped__, "__enter__", None)
                if enter:
                    enter()
        except BaseException as error:
            self._finish(error=error)
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> Any:
        try:
            with self._context():
                exit_ = getattr(self.__wrapped__, "__exit__", None)
                if exit_:
                    result = exit_(exc_type, exc_value, traceback)
                else:
                    close = getattr(self.__wrapped__, "close", None)
                    result = close() if close else None
        except BaseException as error:
            self._finish(error=error)
            raise
        self._finish(error=exc_value)
        return result


class _AsyncStream(_Stream):
    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        return await self._advance_async(self.__wrapped__.__anext__)

    async def asend(self, value: Any) -> Any:
        return await self._advance_async(self.__wrapped__.asend, value)

    async def athrow(self, *args: Any) -> Any:
        return await self._advance_async(self.__wrapped__.athrow, *args)

    async def _advance_async(self, method, *args) -> Any:
        try:
            with self._context():
                chunk = await method(*args)
        except StopAsyncIteration:
            self._finish(exhausted=True)
            raise
        except BaseException as error:
            self._finish(error=error)
            raise
        self._add(chunk)
        return chunk

    async def close(self) -> Any:
        try:
            with self._context():
                close = getattr(self.__wrapped__, "aclose", None) or getattr(
                    self.__wrapped__, "close", None
                )
                result = close() if close else None
                if isawaitable(result):
                    result = await result
        except BaseException as error:
            self._finish(error=error)
            raise
        self._finish()
        return result

    async def aclose(self) -> Any:
        return await self.close()

    async def __aenter__(self) -> "_AsyncStream":
        try:
            with self._context():
                enter = getattr(self.__wrapped__, "__aenter__", None)
                if enter:
                    await enter()
        except BaseException as error:
            self._finish(error=error)
            raise
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> Any:
        try:
            with self._context():
                exit_ = getattr(self.__wrapped__, "__aexit__", None)
                if exit_:
                    result = await exit_(exc_type, exc_value, traceback)
                else:
                    close = getattr(self.__wrapped__, "aclose", None) or getattr(
                        self.__wrapped__, "close", None
                    )
                    result = close() if close else None
                    if isawaitable(result):
                        result = await result
        except BaseException as error:
            self._finish(error=error)
            raise
        self._finish(error=exc_value)
        return result
