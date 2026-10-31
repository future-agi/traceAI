"""Trace a lazy producer without leaking its context into the consumer."""

import sys
from typing import Any, Callable, Iterator, Mapping, Optional

from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue


def _trace_stream(
    tracer: trace_api.Tracer,
    name: str,
    attributes: Mapping[str, AttributeValue],
    factory: Callable[[], Iterator[Any]],
    finish: Callable[[trace_api.Span, Any], None],
    on_chunk: Optional[Callable[[Any], None]] = None,
    on_end: Optional[Callable[[trace_api.Span], None]] = None,
) -> Iterator[Any]:
    # Start only when consumed. Closing an unstarted stream must not invoke the
    # model, allocate a span or run an application decorator.
    span = tracer.start_span(name, attributes=attributes)
    producer_context = trace_api.set_span_in_context(span)
    iterator = advance = close = None
    advance_args = ()
    try:
        token = context_api.attach(producer_context)
        try:
            iterator = iter(factory())
        finally:
            producer_context = context_api.get_current()
            context_api.detach(token)
        value, thrown = None, None
        while True:
            # Resume the producer's latest context, including child spans or
            # attached values still open at a yield. Protocol lookups belong to
            # the producer too, even for decorated ordinary iterators.
            token = context_api.attach(producer_context)
            try:
                if thrown is not None:
                    try:
                        advance = iterator.throw
                    except AttributeError:
                        raise thrown[1]
                    advance_args = thrown
                elif value is None:
                    advance, advance_args = next, (iterator,)
                else:
                    advance, advance_args = iterator.send, (value,)
                try:
                    item = advance(*advance_args)
                except StopIteration as stopped:
                    finish(span, stopped.value)
                    return stopped.value
                if on_chunk is not None:
                    on_chunk(item)
            finally:
                producer_context = context_api.get_current()
                context_api.detach(token)

            try:
                value = yield item
            except GeneratorExit:
                token = context_api.attach(producer_context)
                try:
                    try:
                        close = iterator.close
                    except AttributeError:
                        pass
                    else:
                        close()
                finally:
                    producer_context = context_api.get_current()
                    context_api.detach(token)
                raise
            except BaseException:
                thrown = sys.exc_info()
            else:
                thrown = None
    except GeneratorExit:
        span.set_status(trace_api.StatusCode.OK)
        raise
    except BaseException as exc:
        span.record_exception(exc)
        span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(exc)))
        raise
    finally:
        # Releasing a map or other iterator without close() may finalize its
        # suspended generator. Release all protocol references under the latest
        # producer context, not when this proxy frame unwinds in the consumer.
        token = context_api.attach(producer_context)
        try:
            close = advance = None
            advance_args = ()
            iterator = None
            if on_end is not None:
                on_end(span)
        finally:
            context_api.detach(token)
            span.end()
