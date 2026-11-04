"""SDK-compatible streams that own a request span until exhaustion or close."""

import logging
from typing import Any, Dict, Optional

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue
from wrapt import ObjectProxy

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _get_stream_output_attributes(
    chunks: list,
    full_content: str,
) -> Dict[str, AttributeValue]:
    """Extract output attributes from streaming response chunks."""
    attributes: Dict[str, AttributeValue] = {
        SpanAttributes.OUTPUT_VALUE: full_content,
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}": "assistant",
        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}": full_content,
    }
    if chunks:
        last_chunk = chunks[-1]
        if hasattr(last_chunk, "model"):
            attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = last_chunk.model
        if hasattr(last_chunk, "usage") and last_chunk.usage:
            usage = last_chunk.usage
            # OpenAI 1.0 exposes Fireworks' extra streaming usage as a dict.
            for field, attribute in (
                ("prompt_tokens", SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS),
                ("completion_tokens", SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS),
                ("total_tokens", SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS),
            ):
                value = (
                    usage.get(field)
                    if isinstance(usage, dict)
                    else getattr(usage, field, None)
                )
                if value is not None:
                    attributes[attribute] = value
    try:
        attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps([
            c.model_dump() if hasattr(c, "model_dump") else str(c) for c in chunks
        ])
    except Exception:
        pass
    return attributes


class _StreamBase(ObjectProxy):
    def __init__(self, stream: Any, span: Span) -> None:
        super().__init__(stream)
        self._self_span = span
        self._self_chunks = []
        self._self_content = ""
        self._self_finished = False
        self._self_closed = False

    def _record_chunk(self, chunk: Any) -> None:
        self._self_chunks.append(chunk)
        if getattr(chunk, "choices", None):
            content = getattr(getattr(chunk.choices[0], "delta", None), "content", None)
            if isinstance(content, str):
                self._self_content += content

    def _finish(self, error: Optional[BaseException] = None) -> None:
        if self._self_finished:
            return
        self._self_finished = True
        try:
            self._self_span.set_attributes(
                _get_stream_output_attributes(self._self_chunks, self._self_content)
            )
            if error is None:
                self._self_span.set_status(Status(StatusCode.OK))
            else:
                self._self_span.record_exception(error)
                self._self_span.set_status(Status(StatusCode.ERROR, str(error)))
        except Exception:
            # Telemetry must not mask a provider or cancellation exception.
            logger.debug("Failed to annotate Fireworks stream span")
        finally:
            self._self_span.end()


class _Stream(_StreamBase):
    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> Any:
        if self._self_closed:
            raise StopIteration
        try:
            chunk = next(self.__wrapped__)
        except StopIteration:
            self.close()
            raise
        except BaseException as error:
            try:
                self._close(error)
            except BaseException:
                # Preserve the original read error even if response cleanup fails.
                pass
            raise
        self._record_chunk(chunk)
        return chunk

    def __enter__(self) -> "_Stream":
        try:
            enter = getattr(self.__wrapped__, "__enter__", None)
            if enter is not None:
                enter()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            exit_stream = getattr(self.__wrapped__, "__exit__", None)
            if exit_stream is not None:
                return exit_stream(exc_type, exc, tb)
            return self._close(exc)
        except BaseException as error:
            exc = error
            raise
        finally:
            self._self_closed = True
            self._finish(exc)

    def close(self) -> Any:
        return self._close()

    def _close(self, error: Optional[BaseException] = None) -> Any:
        if self._self_closed:
            return None
        self._self_closed = True
        try:
            close = getattr(self.__wrapped__, "close", None)
            if close is not None:
                return close()
            # SDK 1.0 has no stream close/context methods; it owns an HTTP response.
            return self.__wrapped__.response.close()
        except BaseException as close_error:
            if error is None:
                error = close_error
            raise
        finally:
            self._finish(error)


class _AsyncStream(_StreamBase):
    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._self_closed:
            raise StopAsyncIteration
        try:
            chunk = await self.__wrapped__.__anext__()
        except StopAsyncIteration:
            await self.close()
            raise
        except BaseException as error:
            try:
                await self._close(error)
            except BaseException:
                pass
            raise
        self._record_chunk(chunk)
        return chunk

    async def __aenter__(self) -> "_AsyncStream":
        try:
            enter = getattr(self.__wrapped__, "__aenter__", None)
            if enter is not None:
                await enter()
        except BaseException as error:
            self._finish(error)
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            exit_stream = getattr(self.__wrapped__, "__aexit__", None)
            if exit_stream is not None:
                return await exit_stream(exc_type, exc, tb)
            return await self._close(exc)
        except BaseException as error:
            exc = error
            raise
        finally:
            self._self_closed = True
            self._finish(exc)

    async def close(self) -> Any:
        return await self._close()

    async def aclose(self) -> Any:
        # Keep the previous async-generator interface as an alias to SDK close().
        return await self.close()

    async def _close(self, error: Optional[BaseException] = None) -> Any:
        if self._self_closed:
            return None
        self._self_closed = True
        try:
            close = getattr(self.__wrapped__, "close", None)
            if close is not None:
                return await close()
            return await self.__wrapped__.response.aclose()
        except BaseException as close_error:
            if error is None:
                error = close_error
            raise
        finally:
            self._finish(error)
