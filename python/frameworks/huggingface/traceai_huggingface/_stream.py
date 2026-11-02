"""Lazy stream tracing. Only the producer runs with the inference span current."""

import asyncio
import logging
from typing import Any, Dict, List, Mapping, Optional

from opentelemetry import trace as trace_api

from traceai_huggingface._utils import _finish_tracing, _to_dict
from traceai_huggingface._with_span import _WithSpan

logger = logging.getLogger(__name__)


class _TextAccumulator:
    def __init__(self) -> None:
        self._parts: List[str] = []
        self._final_text: Optional[str] = None
        self._details: Dict[str, Any] = {}

    def add(self, chunk: Any) -> None:
        if isinstance(chunk, str):
            self._parts.append(chunk)
            return
        data = _to_dict(chunk)
        token = _to_dict(data.get("token"))
        if isinstance(token.get("text"), str) and not token.get("special"):
            self._parts.append(token["text"])
        if isinstance(data.get("generated_text"), str):
            self._final_text = data["generated_text"]
        if data.get("details") is not None:
            self._details.update(_to_dict(data["details"]))

    def response(self, tokens_only: bool = False) -> Dict[str, Any]:
        return {
            "generated_text": (
                "".join(self._parts)
                if tokens_only or self._final_text is None
                else self._final_text
            ),
            "details": self._details,
        }


class _ChatAccumulator:
    def __init__(self) -> None:
        self._choices: Dict[int, Dict[str, Any]] = {}
        self._parts: Dict[int, List[str]] = {}
        self._tools: Dict[int, Dict[int, Dict[str, Any]]] = {}
        self._metadata: Dict[str, Any] = {}

    def add(self, chunk: Any) -> None:
        data = _to_dict(chunk)
        for key in ("id", "model", "usage"):
            if data.get(key) is not None:
                self._metadata[key] = (
                    _to_dict(data[key]) if key == "usage" else data[key]
                )
        for position, raw_choice in enumerate(data.get("choices") or []):
            choice = _to_dict(raw_choice)
            index = choice.get("index", position)
            if not isinstance(index, int):
                continue
            if index not in self._choices:
                self._choices[index] = {
                    "index": index,
                    "message": {"role": "assistant"},
                }
                self._parts[index] = []
                self._tools[index] = {}
            result = self._choices[index]
            delta = _to_dict(choice.get("delta"))
            if delta.get("role"):
                result["message"]["role"] = delta["role"]
            if isinstance(delta.get("content"), str):
                self._parts[index].append(delta["content"])
            if choice.get("finish_reason") is not None:
                result["finish_reason"] = choice["finish_reason"]
            for position, raw_tool in enumerate(delta.get("tool_calls") or []):
                tool = _to_dict(raw_tool)
                tool_index = tool.get("index", position)
                if not isinstance(tool_index, int):
                    continue
                target = self._tools[index].setdefault(tool_index, {"function": {}})
                for key in ("id", "type"):
                    if tool.get(key) is not None:
                        target[key] = tool[key]
                function = _to_dict(tool.get("function"))
                # The function name is metadata; arguments are incremental JSON text.
                if function.get("name") is not None:
                    target["function"]["name"] = function["name"]
                if isinstance(function.get("arguments"), str):
                    target["function"]["arguments"] = (
                        target["function"].get("arguments", "") + function["arguments"]
                    )

    def response(self) -> Dict[str, Any]:
        choices = []
        for index in sorted(self._choices):
            choice = self._choices[index]
            choice["message"]["content"] = "".join(self._parts[index])
            if self._tools[index]:
                choice["message"]["tool_calls"] = [
                    self._tools[index][i] for i in sorted(self._tools[index])
                ]
            choices.append(choice)
        return {**self._metadata, "choices": choices}


class _StreamState:
    def __init__(
        self,
        span: _WithSpan,
        extractor: Any,
        request: Mapping[str, Any],
        chat: bool,
        config: Any = None,
    ) -> None:
        self.span = span
        self.extractor = extractor
        self.request = request
        self.accumulator = _ChatAccumulator() if chat else _TextAccumulator()
        self.finished = False
        self.config = config

    def current(self):
        return trace_api.use_span(
            self.span._span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        )

    def add(self, chunk: Any) -> None:
        try:
            self.accumulator.add(chunk)
        except Exception:
            # Tracing must never turn an otherwise usable provider chunk into an error.
            logger.debug("Could not aggregate Hugging Face stream chunk", exc_info=True)

    def finish(self, outcome: str, error: Optional[BaseException] = None) -> None:
        if self.finished:
            return
        self.finished = True
        status = trace_api.Status(trace_api.StatusCode.UNSET)
        if error is not None:
            status = trace_api.Status(
                trace_api.StatusCode.ERROR, f"{type(error).__name__}: {error}"
            )
        elif outcome == "completed":
            status = trace_api.Status(trace_api.StatusCode.OK)
        try:
            self.span._span.set_attribute("huggingface.stream.status", outcome)
            if error is not None:
                self.span.record_exception(error)
            # A full-text final chunk may prepend the private input prompt.
            # Tokens are the only safe output when input capture is disabled.
            tokens_only = bool(
                not isinstance(self.accumulator, _ChatAccumulator)
                and self.config
                and self.config.hide_inputs
                and self.request.get("return_full_text")
            )
            response = (
                self.accumulator.response()
                if isinstance(self.accumulator, _ChatAccumulator)
                else self.accumulator.response(tokens_only=tokens_only)
            )
            if tokens_only:
                # Prefill tokens are input-origin text, too.
                response["details"].pop("prefill", None)
            extra_attributes = self.extractor.get_extra_attributes(
                response, self.request
            )
            if self.config and (
                self.config.hide_output_messages or self.config.hide_output_text
            ):
                # Raw output bypasses FITracer's typed-message privacy mask.
                extra_attributes = (
                    (key, value)
                    for key, value in extra_attributes
                    if key != "output.value"
                )
            _finish_tracing(
                status=status,
                with_span=self.span,
                attributes=self.extractor.get_attributes(response),
                extra_attributes=extra_attributes,
            )
        except Exception:
            logger.debug("Could not finalize Hugging Face stream", exc_info=True)
        finally:
            self.span.finish_tracing(status=status)


class _Stream:
    """Owns the SDK iterator, including close before the first next()."""

    def __init__(self, response: Any, state: _StreamState) -> None:
        self._iterator = iter(response)
        self._state = state

    def __iter__(self):
        return self

    def __next__(self):
        if self._state.finished:
            raise StopIteration
        with self._state.current():
            try:
                chunk = next(self._iterator)
            except StopIteration:
                self._state.finish("completed")
                raise
            except BaseException as error:
                self._state.finish("error", error)
                raise
            self._state.add(chunk)
        return chunk

    def close(self) -> None:
        self._close("closed")

    def _close(self, outcome: str) -> None:
        if self._state.finished:
            return
        with self._state.current():
            try:
                close = getattr(self._iterator, "close", None)
                if close is not None:
                    close()
            except BaseException as error:
                self._state.finish("error", error)
                raise
            finally:
                self._state.finish(outcome)

    def __del__(self) -> None:
        # Like the old generator's finally block, keep a partial trace when a
        # consumer drops the stream without exhausting or explicitly closing it.
        try:
            if hasattr(self, "_state") and hasattr(self, "_iterator"):
                self._close("abandoned")
        except BaseException:
            # Destructors must not raise; _close has already recorded the error.
            pass


class _AsyncStream:
    """Async counterpart; cancellation is recorded and propagated, never swallowed."""

    def __init__(self, response: Any, state: _StreamState) -> None:
        self._iterator = response.__aiter__()
        self._state = state

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._state.finished:
            raise StopAsyncIteration
        with self._state.current():
            try:
                chunk = await self._iterator.__anext__()
            except StopAsyncIteration:
                self._state.finish("completed")
                raise
            except BaseException as error:
                self._state.finish("error", error)
                raise
            self._state.add(chunk)
        return chunk

    async def aclose(self) -> None:
        if self._state.finished:
            return
        with self._state.current():
            try:
                close = getattr(self._iterator, "aclose", None)
                if close is not None:
                    await close()
            except BaseException as error:
                self._state.finish("error", error)
                raise
            finally:
                self._state.finish("closed")

    def __del__(self) -> None:
        # Awaiting cannot be done from a destructor. End the trace immediately;
        # close the producer asynchronously if its event loop is still running.
        try:
            if not hasattr(self, "_state") or self._state.finished:
                return
            with self._state.current():
                self._state.finish("abandoned")
            close = getattr(self._iterator, "aclose", None)
            if close is not None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    return

                async def release():
                    try:
                        await close()
                    except BaseException:
                        logger.debug(
                            "Could not close abandoned Hugging Face stream", exc_info=True
                        )

                loop.create_task(release())
        except BaseException:
            pass
