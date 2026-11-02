"""Scope telemetry context to iterator advancement, never to caller code."""

from collections.abc import Coroutine
from contextlib import contextmanager
from typing import Any, AsyncGenerator, AsyncIterator, Generator, Iterator

from opentelemetry import context, trace


@contextmanager
def span_scope(span: Any, values: dict[str, Any] | None = None) -> Iterator[None]:
    ctx = trace.set_span_in_context(span)
    for key, value in (values or {}).items():
        ctx = context.set_value(key, value, ctx)
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


def iterate(
    iterator: Iterator[Any], span: Any, values: dict[str, Any] | None = None
) -> Iterator[Any]:
    """Advance and close under the span; restore context before every yield."""
    try:
        while True:
            with span_scope(span, values):
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            yield item
    finally:
        with span_scope(span, values):
            if close := getattr(iterator, "close", None):
                close()


async def aiterate(
    iterator: AsyncIterator[Any], span: Any, values: dict[str, Any] | None = None
) -> AsyncIterator[Any]:
    try:
        while True:
            with span_scope(span, values):
                try:
                    item = await anext(iterator)
                except StopAsyncIteration:
                    return
            yield item
    finally:
        with span_scope(span, values):
            if close := getattr(iterator, "aclose", None):
                await close()


@contextmanager
def finish_span(span: Any) -> Iterator[Any]:
    """End on all exits, including task cancellation and generator closure."""
    try:
        yield span
    except BaseException as error:
        # Closure/cancellation are not successful completion. Avoid recording a
        # GeneratorExit exception event for an intentional consumer close.
        if isinstance(error, GeneratorExit):
            span.set_status(trace.StatusCode.ERROR, "stream closed before completion")
        else:
            span.set_status(trace.StatusCode.ERROR, str(error) or type(error).__name__)
            span.record_exception(error)
        raise
    finally:
        span.end()


class EagerSpanCoroutine(Coroutine[Any, Any, Any]):
    """Own dispatcher resources even if a task is cancelled before its first send.

    Agno's synchronous async-dispatchers perform validation immediately. Preserve
    that behavior, but make pre-start throw/close release both the returned SDK
    coroutine and its span. Once started, the finishing coroutine owns cleanup.
    """

    def __init__(self, coroutine: Any, source: Any, span: Any) -> None:
        self._coroutine = coroutine
        self._source = source
        self._span = span
        self._started = False
        self._closed = False

    def __await__(self) -> Any:
        return self

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        return self.send(None)

    def send(self, value: Any) -> Any:
        self._started = True
        self._source = None
        try:
            return self._coroutine.send(value)
        except BaseException:
            self._closed = True
            raise

    def throw(self, *args: Any) -> Any:
        if self._started or self._closed:
            try:
                return self._coroutine.throw(*args)
            except BaseException:
                self._closed = True
                raise
        self._closed = True
        with finish_span(self._span):
            try:
                # A never-started coroutine raises the injected exception without
                # entering its body. finish_span records it and ends exactly once.
                return self._coroutine.throw(*args)
            finally:
                self._close_source()

    def _close_source(self) -> None:
        source, self._source = self._source, None
        if close := getattr(source, "close", None):
            close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._coroutine.close()
        else:
            with finish_span(self._span):
                self._span.set_status(
                    trace.StatusCode.ERROR, "coroutine closed before execution"
                )
                try:
                    self._close_source()
                finally:
                    self._coroutine.close()


class EagerSpanIterator(Iterator[Any]):
    """Own an eager span until a lazy finishing generator takes over on next().

    Closing a never-started Python generator does not execute its finally block.
    Until the first advance, this owner must close the source and end the span.
    After that, the finishing generator owns cleanup (including errors).
    """

    def __init__(
        self, iterator: Generator[Any, None, None], source: Any, span: Any
    ) -> None:
        self._iterator = iterator
        self._source = source
        self._span = span
        self._started = False
        self._closed = False

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        self._started = True
        # The finishing generator now owns the source; do not retain it after close.
        self._source = None
        try:
            return next(self._iterator)
        except BaseException:
            self._closed = True
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            self._iterator.close()
        else:
            with finish_span(self._span), span_scope(self._span):
                self._span.set_status(
                    trace.StatusCode.ERROR, "stream closed before completion"
                )
                if close := getattr(self._source, "close", None):
                    close()
                self._iterator.close()


class EagerSpanAsyncIterator(AsyncIterator[Any]):
    """Async counterpart of EagerSpanIterator; aclose owns unstarted cleanup."""

    def __init__(
        self, iterator: AsyncGenerator[Any, None], source: Any, span: Any
    ) -> None:
        self._iterator = iterator
        self._source = source
        self._span = span
        self._started = False
        self._closed = False

    async def __anext__(self) -> Any:
        if self._closed:
            raise StopAsyncIteration
        self._started = True
        # The finishing generator now owns the source; do not retain it after close.
        self._source = None
        try:
            return await anext(self._iterator)
        except BaseException:
            self._closed = True
            raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._started:
            await self._iterator.aclose()
        else:
            with finish_span(self._span), span_scope(self._span):
                self._span.set_status(
                    trace.StatusCode.ERROR, "stream closed before completion"
                )
                if close := getattr(self._source, "aclose", None):
                    await close()
                await self._iterator.aclose()
