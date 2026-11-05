"""Observe raw response consumption without reading or replacing the SDK's body."""
import inspect
from functools import wraps
from typing import Any, Callable

from opentelemetry import trace
from wrapt import ObjectProxy

from traceai_anthropic._stream import _MessagesStream
from traceai_anthropic._utils import _finish_request_error
from traceai_anthropic._with_span import _WithSpan


class _RawResponse(ObjectProxy):
    def __init__(self, response: Any, span: _WithSpan, on_result: Callable[[Any], Any]):
        super().__init__(response)
        self._self_span = span
        self._self_on_result = on_result
        self._self_results = []
        # with_raw_response has already read non-streaming bodies. Keeping its
        # span open until parse()/GC loses headers-only requests at shutdown.
        # SSE and with_streaming_response still belong to the consumer.
        if (
            response.http_response.is_stream_consumed
            and not getattr(response, "_is_sse_stream", False)
            and not getattr(response, "_stream", False)
        ):
            try:
                # The SDK's body-only parser neither invokes options.post_parser
                # nor populates the caller's parse cache. It is synchronous even
                # for AsyncAPIResponse, and never reads an unread HTTP body.
                parsed = response._parse()
            except Exception as error:
                # Preserve the raw API: returning headers must not raise a parse
                # error. A later caller parse still raises the SDK's own error.
                _finish_request_error(span, error)
            else:
                on_result(parsed)

    def __getattr__(self, name: str) -> Any:
        value = super().__getattr__(name)
        # LegacyAPIResponse has body properties (notably .text), while APIResponse
        # exposes methods. Only wrap existing callables so both contracts survive.
        if name not in ("read", "text", "json", "iter_bytes", "iter_text", "iter_lines") or not callable(value):
            return value

        @wraps(value)
        def read_body(*args: Any, **kwargs: Any) -> Any:
            try:
                result = value(*args, **kwargs)
            except BaseException as error:
                _finish_request_error(self._self_span, error)
                raise
            if name.startswith("iter_"):
                return _RawBodyIterator(result, self._self_span)
            if inspect.isawaitable(result):
                async def wait_for_body():
                    try:
                        return await result
                    except BaseException as error:
                        _finish_request_error(self._self_span, error)
                        raise
                return wait_for_body()
            return result

        return read_body

    def _parsed(self, result: Any) -> Any:
        # The SDK caches parse results (including streams). Preserve that identity
        # and never put multiple observers around the same iterator.
        for original, observed in self._self_results:
            if original is result:
                return observed
        if self._self_span.is_finished:
            return result
        observed = self._self_on_result(result)
        self._self_results.append((result, observed))
        return observed

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            result = self.__wrapped__.parse(*args, **kwargs)
        except BaseException as error:
            _finish_request_error(self._self_span, error)
            raise
        if inspect.isawaitable(result):
            async def wait_for_parse():
                try:
                    parsed = await result
                except BaseException as error:
                    _finish_request_error(self._self_span, error)
                    raise
                return self._parsed(parsed)
            return wait_for_parse()
        return self._parsed(result)

    def _closed(self):
        stream_owned = False
        for _, observed in self._self_results:
            if isinstance(observed, _MessagesStream):
                stream_owned = True
                # The response has already closed its transport. Settle the observer
                # without invoking the native close a second time.
                observed._begin_operation()
                observed._end_operation(terminal=True)
        if not stream_owned:
            self._self_span.finish_tracing(status=trace.Status(trace.StatusCode.UNSET))

    def close(self) -> Any:
        try:
            result = self.__wrapped__.close()
        except BaseException as error:
            _finish_request_error(self._self_span, error)
            raise
        if inspect.isawaitable(result):
            async def wait_for_close():
                try:
                    value = await result
                except BaseException as error:
                    _finish_request_error(self._self_span, error)
                    raise
                self._closed()
                return value
            return wait_for_close()
        self._closed()
        return result

    def __del__(self):
        # Legacy raw responses have no close() method. Abandonment is not success.
        # Do not finalize a parsed stream here: the caller may still own it.
        try:
            if not self._self_results:
                self._self_span.finish_tracing(status=trace.Status(trace.StatusCode.UNSET))
        except Exception:
            pass


class _RawBodyIterator(ObjectProxy):
    """Observe only consumer-driven advancement; retain native iterator helpers."""

    def __init__(self, iterator: Any, span: _WithSpan):
        super().__init__(iterator)
        self._self_span = span

    def __iter__(self):
        return self

    def __aiter__(self):
        return self

    def __next__(self):
        try:
            return next(self.__wrapped__)
        except StopIteration:
            raise
        except BaseException as error:
            _finish_request_error(self._self_span, error)
            raise

    async def __anext__(self):
        try:
            return await self.__wrapped__.__anext__()
        except StopAsyncIteration:
            raise
        except BaseException as error:
            _finish_request_error(self._self_span, error)
            raise
