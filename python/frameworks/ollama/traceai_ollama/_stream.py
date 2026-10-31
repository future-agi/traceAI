"""Bounded telemetry and explicit lifecycle for Ollama's lazy iterators."""
import json
import logging
from typing import Any, AsyncIterator, ContextManager, Dict, Iterator, Mapping, Optional

from fi_instrumentation import TraceConfig, safe_json_dumps
from opentelemetry import trace

from traceai_ollama._response_attributes_extractor import _ResponseAttributesExtractor
from traceai_ollama._utils import _finish_tracing, _to_dict
from traceai_ollama._with_span import _WithSpan

logger = logging.getLogger(__name__)

# A stream may be arbitrarily long. Keep a preview, not every raw chunk, and
# continue collecting usage after the preview fills. Limits never affect users.
_MAX_OUTPUT_CHARACTERS = 64 * 1024
_MAX_TOOL_CALLS = 32
_METADATA_KEYS = (
    "model", "created_at", "done", "done_reason", "total_duration",
    "load_duration", "prompt_eval_count", "prompt_eval_duration",
    "eval_count", "eval_duration",
)


class _ResponseAccumulator:
    def __init__(self, config: TraceConfig) -> None:
        self._config = config
        self._remaining = _MAX_OUTPUT_CHARACTERS
        self._metadata: Dict[str, Any] = {}
        self._content: list[str] = []
        self._thinking: list[str] = []
        self._tools: list[Dict[str, Any]] = []
        self._chat = False
        self.truncated = False

    def _text(self, value: Any, target: list) -> None:
        if not isinstance(value, str) or not value:
            return
        if self._config.hide_output_text:
            return
        preview = value[:self._remaining]
        if preview:
            target.append(preview)
            self._remaining -= len(preview)
        self.truncated |= len(preview) < len(value)

    def add(self, chunk: Any) -> None:
        data = _to_dict(chunk)
        for key in _METADATA_KEYS:
            value = data.get(key)
            if isinstance(value, (bool, int, float)):
                self._metadata[key] = value
            elif isinstance(value, str):
                self._metadata[key] = value[:1024]
        self._chat |= "message" in data
        if self._config.hide_outputs or self._config.hide_output_messages:
            return
        message = data.get("message") or data
        self._text(message.get("content" if self._chat else "response"), self._content)
        self._text(message.get("thinking"), self._thinking)
        for tool in message.get("tool_calls") or ():
            if len(self._tools) >= _MAX_TOOL_CALLS or not self._remaining:
                self.truncated = True
                break
            serialized = safe_json_dumps(tool)
            if len(serialized) > self._remaining:
                self.truncated = True
                continue
            self._remaining -= len(serialized)
            # Keep a detached, bounded copy; callers still receive the original.
            self._tools.append(json.loads(serialized))

    def response(self) -> Dict[str, Any]:
        result = dict(self._metadata)
        message = {"content" if self._chat else "response": "".join(self._content)}
        if self._thinking:
            message["thinking"] = "".join(self._thinking)
        if self._tools:
            message["tool_calls"] = list(self._tools)
        if self._chat:
            result["message"] = dict(role="assistant", **message)
        else:
            result.update(message)
        return result


class _StreamState:
    def __init__(
        self, span: _WithSpan, extractor: _ResponseAttributesExtractor,
        parameters: Mapping[str, Any], config: TraceConfig,
    ) -> None:
        self.span = span
        self._extractor = extractor
        self._parameters = parameters
        self._accumulator = _ResponseAccumulator(config)
        self.finished = False

    def current_span(self) -> ContextManager[trace.Span]:
        # Scope only SDK advancement/close, never application code between yields.
        return trace.use_span(
            self.span._span, end_on_exit=False,
            record_exception=False, set_status_on_exception=False,
        )

    def accept(self, chunk: Any) -> None:
        try:
            self._accumulator.add(chunk)
        except Exception:
            logger.exception("Failed to collect Ollama stream attributes")

    def finish(self, error: Optional[BaseException] = None, complete: bool = False) -> None:
        if self.finished:
            return
        self.finished = True
        status = trace.Status(trace.StatusCode.OK if complete else trace.StatusCode.UNSET)
        try:
            if error is not None:
                self.span.record_exception(error)
                status = trace.Status(trace.StatusCode.ERROR, f"{type(error).__name__}: {error}")
            response = self._accumulator.response()
            if self._accumulator.truncated:
                self.span._span.set_attribute("ollama.output_truncated", True)
            _finish_tracing(
                status=status, with_span=self.span,
                attributes=self._extractor.get_attributes(response, is_streaming=True),
                extra_attributes=self._extractor.get_extra_attributes(
                    response, self._parameters, is_streaming=True,
                ),
            )
        except Exception:
            logger.exception("Failed to finalize Ollama stream")
        finally:
            self.span.finish_tracing(status=status)

    def __del__(self) -> None:
        # Best effort for abandoned streams. Explicit close/aclose is deterministic.
        self.finish()


class _SyncStream(Iterator[Any]):
    def __init__(self, response: Iterator[Any], state: _StreamState) -> None:
        self._response = iter(response)
        self._state = state

    def __iter__(self) -> "_SyncStream":
        return self

    def __next__(self) -> Any:
        if self._state.finished:
            raise StopIteration
        try:
            with self._state.current_span():
                chunk = next(self._response)
        except StopIteration:
            self._state.finish(complete=True)
            raise
        except BaseException as error:
            self._state.finish(error=error)
            raise
        self._state.accept(chunk)
        return chunk

    def close(self) -> None:
        try:
            with self._state.current_span():
                close = getattr(self._response, "close", None)
                if close is not None:
                    close()
        except BaseException as error:
            self._state.finish(error=error)
            raise
        finally:
            self._state.finish()


class _AsyncStream(AsyncIterator[Any]):
    def __init__(self, response: AsyncIterator[Any], state: _StreamState) -> None:
        self._response = response.__aiter__()
        self._state = state

    def __aiter__(self) -> "_AsyncStream":
        return self

    async def __anext__(self) -> Any:
        if self._state.finished:
            raise StopAsyncIteration
        try:
            with self._state.current_span():
                chunk = await self._response.__anext__()
        except StopAsyncIteration:
            self._state.finish(complete=True)
            raise
        except BaseException as error:
            self._state.finish(error=error)
            raise
        self._state.accept(chunk)
        return chunk

    async def aclose(self) -> None:
        try:
            with self._state.current_span():
                close = getattr(self._response, "aclose", None)
                if close is not None:
                    await close()
        except BaseException as error:
            self._state.finish(error=error)
            raise
        finally:
            self._state.finish()
