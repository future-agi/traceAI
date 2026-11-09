"""Keep raw SDK response ownership separate from parsed completion telemetry."""

import inspect
from typing import Any, Callable

from opentelemetry import trace
from wrapt import ObjectProxy

from traceai_deepseek._stream import _AsyncStream, _Stream
from traceai_deepseek._with_span import _WithSpan


class _RawResponse(ObjectProxy):
    """Lazy raw response proxy, including OpenAI 1.0's synchronous parse API.

    Only parsed completions/streams reach the extractor. Never serialize the raw
    container: it holds the client's request options, including authorization.
    """

    def __init__(
        self, response: Any, finalize: Callable[[Any], Any], span: _WithSpan,
        asynchronous: bool,
    ) -> None:
        super().__init__(response)
        self._self_finalize = finalize
        self._self_span = span
        self._self_asynchronous = asynchronous
        self._self_parsed = None
        self._self_parsed_raw = None
        self._self_closed = False

    def _self_error(self, error: BaseException) -> None:
        self._self_span.record_exception(error)
        self._self_span.finish_tracing(status=trace.Status(
            trace.StatusCode.ERROR, f"{type(error).__name__}: {error}"
        ))

    def _self_wrap(self, parsed: Any) -> Any:
        # The SDK owns its parse cache. Preserve the corresponding proxy identity
        # without replacing the SDK's cache or consuming a stream to collect data.
        if self._self_parsed is not None and parsed is self._self_parsed_raw:
            return self._self_parsed
        wrapped = self._self_finalize(parsed)
        self._self_parsed_raw = parsed
        self._self_parsed = wrapped
        return wrapped

    def parse(self, *args: Any, **kwargs: Any) -> Any:
        try:
            parsed = self.__wrapped__.parse(*args, **kwargs)
            if inspect.isawaitable(parsed):
                async def await_parsed() -> Any:
                    try:
                        return self._self_wrap(await parsed)
                    except BaseException as error:
                        self._self_error(error)
                        raise
                return await_parsed()
            return self._self_wrap(parsed)
        except BaseException as error:
            self._self_error(error)
            raise

    def _self_finish(self) -> None:
        if isinstance(self._self_parsed, (_Stream, _AsyncStream)):
            # Closing the HTTP response also closes the parsed stream. Preserve
            # accumulated output without claiming that partial work completed.
            self._self_parsed._self_state.finish()
        else:
            self._self_span.finish_tracing()

    def close(self) -> Any:
        if self._self_asynchronous:
            return self.aclose()
        if self._self_closed:
            return None
        self._self_closed = True
        try:
            # Raw 1.0 responses have no close method, only http_response.close.
            close = getattr(self.__wrapped__, "close", None)
            if close is None:
                close = self.__wrapped__.http_response.close
            return close()
        except BaseException as error:
            self._self_error(error)
            raise
        finally:
            self._self_finish()

    async def aclose(self) -> None:
        if self._self_closed:
            return
        self._self_closed = True
        try:
            close = getattr(self.__wrapped__, "aclose", None)
            if close is None:
                close = getattr(self.__wrapped__, "close", None)
            if close is None:
                close = self.__wrapped__.http_response.aclose
            result = close()
            if inspect.isawaitable(result):
                await result
        except BaseException as error:
            self._self_error(error)
            raise
        finally:
            self._self_finish()

    def __enter__(self) -> "_RawResponse":
        try:
            enter = getattr(self.__wrapped__, "__enter__", None)
            if enter is not None:
                enter()
            return self
        except BaseException as error:
            self._self_error(error)
            raise

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            exit_raw = getattr(self.__wrapped__, "__exit__", None)
            return exit_raw(exc_type, exc, tb) if exit_raw is not None else None
        except BaseException as error:
            exc = error
            raise
        finally:
            if exc is not None:
                self._self_error(exc)
            self.close()

    async def __aenter__(self) -> "_RawResponse":
        try:
            enter = getattr(self.__wrapped__, "__aenter__", None)
            if enter is not None:
                await enter()
            return self
        except BaseException as error:
            self._self_error(error)
            raise

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            exit_raw = getattr(self.__wrapped__, "__aexit__", None)
            return await exit_raw(exc_type, exc, tb) if exit_raw is not None else None
        except BaseException as error:
            exc = error
            raise
        finally:
            if exc is not None:
                self._self_error(exc)
            await self.aclose()
