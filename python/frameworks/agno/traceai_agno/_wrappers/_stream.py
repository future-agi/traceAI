"""Scope telemetry context to iterator advancement, never to caller code."""

from contextlib import contextmanager
from typing import Any, AsyncIterator, Iterator

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
