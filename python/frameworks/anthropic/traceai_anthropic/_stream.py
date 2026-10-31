from collections import defaultdict
from copy import deepcopy
import inspect
import threading
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Callable,
    DefaultDict,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiMimeTypeValues,
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue
from traceai_anthropic._utils import (
    _as_output_attributes,
    _error_status,
    _finish_tracing,
    _ValueAndType,
)
from traceai_anthropic._with_span import _WithSpan
from wrapt import ObjectProxy

if TYPE_CHECKING:
    from anthropic import Stream
    from anthropic.types import Completion, RawMessageStreamEvent


# Settled items held back by a stalled earlier extraction before ordering is
# given up for that extraction.
_MAX_PENDING = 1024


class _StreamLifecycle(ObjectProxy):
    """Keep the tracing lifetime coupled to the SDK stream's native lifetime."""

    def __init__(self, stream):
        super().__init__(stream)
        self._self_lock = threading.RLock()
        self._self_active = 0
        self._self_terminal = False
        self._self_error = None
        self._self_finalizing = False
        # Items are extracted outside the lock, possibly concurrently; tickets
        # taken on arrival keep their merges in the order the stream produced them.
        # A settled item waits only for earlier tickets still being extracted.
        self._self_next_ticket = 0
        self._self_extracting = set()
        self._self_pending = {}
        self._self_skipped = set()

    def _begin_operation(self):
        with self._self_lock:
            self._self_active += 1

    def _end_operation(self, *, terminal=False, error=None, item=None, has_item=False):
        ticket = values = None
        try:
            if has_item:
                with self._self_lock:
                    ticket = self._self_next_ticket
                    self._self_next_ticket += 1
                    self._self_extracting.add(ticket)
                values = self._extract(item)
        finally:
            self._settle(terminal, error, ticket, values)

    def _extract(self, item):
        # SDK model methods and the values they return are external code, so read
        # and copy the item before taking the lifecycle lock. A telemetry failure
        # must not replace the native item.
        try:
            return _plain(self._response_accumulator.extract(item))
        except Exception:
            return None

    def _merge(self, values):
        # Once finalization is claimed, a subsequent native operation
        # must not mutate the accumulator being read by the exporter.
        if values is None or self._self_finalizing:
            return
        try:
            self._response_accumulator.merge(values)
        except Exception:
            # Losing one item's telemetry must not change the native operation.
            pass

    def _drain(self, everything=False):
        # Merge settled items in ticket order, up to the earliest ticket still
        # being extracted. Recomputed from state on every call, so an interrupt
        # part-way through leaves nothing stranded.
        floor = None
        if not everything and self._self_extracting:
            floor = min(self._self_extracting)
        for ticket in sorted(self._self_pending):
            if floor is not None and ticket > floor:
                break
            self._merge(self._self_pending.pop(ticket))

    def _settle(self, terminal, error, ticket, values):
        finalize = False
        final_error = None
        try:
            with self._self_lock:
                try:
                    if ticket is not None:
                        self._self_extracting.discard(ticket)
                        if ticket in self._self_skipped:
                            # Later items were already merged without it.
                            self._self_skipped.discard(ticket)
                        else:
                            self._self_pending[ticket] = values
                        if len(self._self_pending) > _MAX_PENDING:
                            # An earlier extraction is stalled; stop holding later
                            # items for it so the queue stays bounded.
                            latest = max(self._self_pending)
                            stalled = {t for t in self._self_extracting if t < latest}
                            self._self_skipped |= stalled
                            self._self_extracting -= stalled
                        self._drain()
                finally:
                    if terminal:
                        self._self_terminal = True
                    if error is not None and self._self_error is None:
                        self._self_error = error
                    self._self_active -= 1
                    if (self._self_terminal and not self._self_active
                            and not self._self_finalizing):
                        # Every operation has settled; merge whatever is left.
                        self._drain(everything=True)
                        self._self_finalizing = True
                        finalize = True
                        final_error = self._self_error
        finally:
            if finalize:
                # Span processors/exporters are arbitrary synchronous user code.
                # Claim exactly once under the lock, then release it before calling
                # them so another thread can always reach the native close.
                try:
                    if final_error is not None:
                        self._finish_error(final_error)
                    else:
                        self._finish_tracing(trace_api.Status(trace_api.StatusCode.OK))
                except BaseException:
                    # Telemetry must not replace the native operation's outcome.
                    pass

    def _advance(self, iterator):
        self._begin_operation()
        try:
            item = next(iterator)
        except StopIteration:
            self._end_operation(terminal=True)
            raise
        except BaseException as exc:
            self._end_operation(terminal=True, error=exc)
            raise
        self._end_operation(item=item, has_item=True)
        return item

    async def _advance_async(self, iterator):
        self._begin_operation()
        try:
            item = await iterator.__anext__()
        except StopAsyncIteration:
            self._end_operation(terminal=True)
            raise
        except BaseException as exc:
            self._end_operation(terminal=True, error=exc)
            raise
        self._end_operation(item=item, has_item=True)
        return item

    def close(self):
        # Calling an async native close merely creates a coroutine; ownership
        # starts when it is awaited, just as the underlying close does.
        if inspect.iscoroutinefunction(self.__wrapped__.close):
            async def close_async():
                self._begin_operation()
                try:
                    result = await self.__wrapped__.close()
                except BaseException as exc:
                    self._end_operation(terminal=True, error=exc)
                    raise
                self._end_operation(terminal=True)
                return result
            return close_async()
        self._begin_operation()
        try:
            result = self.__wrapped__.close()
        except BaseException as exc:
            self._end_operation(terminal=True, error=exc)
            raise
        if inspect.isawaitable(result):
            async def wait_for_close():
                try:
                    value = await result
                except BaseException as exc:
                    self._end_operation(terminal=True, error=exc)
                    raise
                self._end_operation(terminal=True)
                return value
            return wait_for_close()
        self._end_operation(terminal=True)
        return result

    def __enter__(self):
        self.__wrapped__.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        self._begin_operation()
        try:
            result = self.__wrapped__.__exit__(exc_type, exc, tb)
        except BaseException as error:
            self._end_operation(terminal=True, error=error)
            raise
        self._end_operation(terminal=True, error=exc)
        return result

    async def __aenter__(self):
        await self.__wrapped__.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._begin_operation()
        try:
            result = await self.__wrapped__.__aexit__(exc_type, exc, tb)
        except BaseException as error:
            self._end_operation(terminal=True, error=error)
            raise
        self._end_operation(terminal=True, error=exc)
        return result

    def _finish_error(self, exception):
        status = _error_status(exception)
        try:
            self._with_span.record_exception(exception)
        except BaseException:
            pass
        self._finish_tracing(status)


class _Stream(_StreamLifecycle):  # type: ignore
    __slots__ = (
        "_response_accumulator",
        "_with_span",
        "_is_finished",
    )

    def __init__(
        self,
        stream: "Stream[Completion]",
        with_span: _WithSpan,
    ) -> None:
        super().__init__(stream)
        self._response_accumulator = _ResponseAccumulator()
        self._with_span = with_span

    def __next__(self):
        return self._advance(self.__wrapped__)

    async def __anext__(self):
        return await self._advance_async(self.__wrapped__)

    def __iter__(self) -> Iterator["Completion"]:
        iterator = iter(self.__wrapped__)
        while True:
            try:
                item = self._advance(iterator)
            except StopIteration:
                return
            # Closing this iterator does not close the native stream.
            yield item

    async def __aiter__(self) -> AsyncIterator["Completion"]:
        iterator = self.__wrapped__.__aiter__()
        while True:
            try:
                item = await self._advance_async(iterator)
            except StopAsyncIteration:
                return
            yield item

    def _finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
    ) -> None:
        _finish_tracing(
            with_span=self._with_span,
            has_attributes=_ResponseExtractor(
                response_accumulator=self._response_accumulator
            ),
            status=status,
        )


class _ResponseAccumulator:
    __slots__ = (
        "_is_null",
        "_values",
    )

    def __init__(self) -> None:
        self._is_null = True
        self._values = _ValuesAccumulator(
            completion=_StringAccumulator(),
            stop=_SimpleStringReplace(),
            stop_reason=_SimpleStringReplace(),
        )

    def extract(self, chunk: "Completion") -> Dict[str, Any]:
        return chunk.model_dump(exclude_unset=True, warnings=False)

    def merge(self, values: Mapping[str, Any]) -> None:
        self._is_null = False
        self._values += values

    def _result(self) -> Optional[Dict[str, Any]]:
        if self._is_null:
            return None
        return dict(self._values)


class _ResponseExtractor:
    __slots__ = ("_response_accumulator",)

    def __init__(
        self,
        response_accumulator: _ResponseAccumulator,
    ) -> None:
        self._response_accumulator = response_accumulator

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._response_accumulator._result()):
            return

        json_string = safe_json_dumps(result)
        yield SpanAttributes.OUTPUT_VALUE, json_string

        yield from _as_output_attributes(
            _ValueAndType(json_string, FiMimeTypeValues.JSON)
        )

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._response_accumulator._result()):
            return
        if completion := result.get("completion", ""):
            yield SpanAttributes.GEN_AI_OUTPUT_MESSAGES, completion


class _MessagesStream(_StreamLifecycle):  # type: ignore
    __slots__ = (
        "_response_accumulator",
        "_with_span",
        "_is_finished",
    )

    def __del__(self):
        # Preserve the prior best-effort cleanup for abandoned message streams.
        try:
            self._begin_operation()
            self._end_operation(terminal=True)
        except Exception:
            pass

    def __init__(
        self,
        stream: "Stream[RawMessageStreamEvent]",
        with_span: _WithSpan,
    ) -> None:
        super().__init__(stream)
        self._response_accumulator = _MessageResponseAccumulator()
        self._with_span = with_span

    def __next__(self):
        return self._advance(self.__wrapped__)

    async def __anext__(self):
        return await self._advance_async(self.__wrapped__)

    def __iter__(self) -> Iterator["RawMessageStreamEvent"]:
        iterator = iter(self.__wrapped__)
        while True:
            try:
                item = self._advance(iterator)
            except StopIteration:
                return
            # Closing this iterator does not close the native stream.
            yield item

    async def __aiter__(self) -> AsyncIterator["RawMessageStreamEvent"]:
        iterator = self.__wrapped__.__aiter__()
        while True:
            try:
                item = await self._advance_async(iterator)
            except StopAsyncIteration:
                return
            yield item

    def _finish_tracing(
        self,
        status: Optional[trace_api.Status] = None,
    ) -> None:
        _finish_tracing(
            with_span=self._with_span,
            has_attributes=_MessageResponseExtractor(
                response_accumulator=self._response_accumulator
            ),
            status=status,
        )


def _token_count(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _plain(value: Any) -> Any:
    """Copy extracted values into built-in types, so merging runs no foreign code."""
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int.__int__(value)
    if isinstance(value, float):
        return float.__float__(value)
    if isinstance(value, Mapping):
        return {_plain(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return str(value)


class _MessageResponseAccumulator:
    __slots__ = (
        "_is_null",
        "_values",
        "_current_message_idx",
        "_current_block",
    )

    def __init__(self) -> None:
        self._is_null = True
        self._current_message_idx = -1
        # (kind, type, tool name) of the open content block; kind is "text",
        # "tool_use" or None for blocks whose deltas are not recorded.
        self._current_block: Tuple[Optional[str], Optional[str], Optional[str]] = (
            None, None, None
        )
        self._values = _ValuesAccumulator(
            messages=_IndexedAccumulator(
                lambda: _ValuesAccumulator(
                    role=_SimpleStringReplace(),
                    content=_IndexedAccumulator(
                        lambda: _ValuesAccumulator(
                            type=_SimpleStringReplace(),
                            text=_StringAccumulator(),
                            tool_name=_SimpleStringReplace(),
                            tool_input=_StringAccumulator(),
                        ),
                    ),
                    stop_reason=_SimpleStringReplace(),
                    input_tokens=_SimpleStringReplace(),
                    output_tokens=_SimpleStringReplace(),
                ),
            ),
        )

    def extract(self, chunk: "RawMessageStreamEvent") -> Tuple[Any, ...]:
        """Read an SDK event into plain values; merge applies them in order."""
        from anthropic.types.raw_content_block_delta_event import (
            RawContentBlockDeltaEvent,
        )
        from anthropic.types.raw_content_block_start_event import (
            RawContentBlockStartEvent,
        )
        from anthropic.types.raw_message_delta_event import RawMessageDeltaEvent
        from anthropic.types.raw_message_start_event import RawMessageStartEvent
        from anthropic.types.text_block import TextBlock
        from anthropic.types.tool_use_block import ToolUseBlock

        if isinstance(chunk, RawMessageStartEvent):
            # The SDK accepts events whose usage is null; keep the rest of the event.
            usage = getattr(chunk.message, "usage", None)
            return ("message_start", chunk.message.role,
                    _token_count(getattr(usage, "input_tokens", None)))
        if isinstance(chunk, RawContentBlockStartEvent):
            block = chunk.content_block
            if isinstance(block, TextBlock):
                return ("block_start", "text", block.type, None)
            if isinstance(block, ToolUseBlock):
                return ("block_start", "tool_use", block.type, block.name)
            return ("block_start", None, None, None)
        if isinstance(chunk, RawContentBlockDeltaEvent):
            delta_type = getattr(chunk.delta, "type", None)
            if delta_type == "text_delta":
                return ("block_delta", chunk.index, delta_type, chunk.delta.text)  # type: ignore
            if delta_type == "input_json_delta":
                return ("block_delta", chunk.index, delta_type, chunk.delta.partial_json)  # type: ignore
            return ("other",)
        if isinstance(chunk, RawMessageDeltaEvent):
            usage = getattr(chunk, "usage", None)
            return ("message_delta", chunk.delta.stop_reason,
                    _token_count(getattr(usage, "output_tokens", None)))
        return ("other",)

    def merge(self, event: Tuple[Any, ...]) -> None:
        self._is_null = False
        kind = event[0]
        if kind == "message_start":
            _, role, input_tokens = event
            self._current_message_idx += 1
            self._values += {
                "messages": {
                    "index": str(self._current_message_idx),
                    "role": role,
                    "input_tokens": input_tokens,
                }
            }
        elif kind == "block_start":
            self._current_block = event[1:]
        elif kind == "block_delta":
            _, index, delta_type, fragment = event
            block_kind, block_type, tool_name = self._current_block
            if block_kind == "text" and delta_type == "text_delta":
                content = {"index": index, "type": block_type, "text": fragment}
            elif block_kind == "tool_use" and delta_type == "input_json_delta":
                content = {
                    "index": index,
                    "type": block_type,
                    "tool_name": tool_name,
                    "tool_input": fragment,
                }
            else:
                return
            self._values += {
                "messages": {
                    "index": str(self._current_message_idx),
                    "content": content,
                }
            }
        elif kind == "message_delta":
            _, stop_reason, output_tokens = event
            self._values += {
                "messages": {
                    "index": str(self._current_message_idx),
                    "stop_reason": stop_reason,
                    "output_tokens": output_tokens,
                }
            }

    def _result(self) -> Optional[Dict[str, Any]]:
        if self._is_null:
            return None
        return dict(self._values)


class _MessageResponseExtractor:
    __slots__ = ("_response_accumulator",)

    def __init__(
        self,
        response_accumulator: _MessageResponseAccumulator,
    ) -> None:
        self._response_accumulator = response_accumulator

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._response_accumulator._result()):
            return

        json_string = safe_json_dumps(result)
        yield SpanAttributes.OUTPUT_VALUE, json_string

        messages = result.get("messages", [])
        message_content = ""
        for message in messages:
            if message.get("role") == "assistant":
                for content_block in message.get("content", []):
                    if text := content_block.get("text"):
                        message_content += text
        if message_content:
            yield from _as_output_attributes(
                _ValueAndType(message_content, FiMimeTypeValues.TEXT)
            )
        else:
            json_string = safe_json_dumps(result)
            yield from _as_output_attributes(
                _ValueAndType(json_string, FiMimeTypeValues.JSON)
            )

    def get_extra_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        if not (result := self._response_accumulator._result()):
            return
        messages = result.get("messages", [])
        idx = 0
        total_completion_token_count = 0
        total_prompt_token_count = 0
        # TODO(harrison): figure out if we should always assume messages is 1.
        # The current non streaming implementation assumes the same
        for message in messages:
            if role := message.get("role"):
                yield (
                    f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{idx}.{MessageAttributes.MESSAGE_ROLE}",
                    role,
                )
            if output_tokens := message.get("output_tokens"):
                total_completion_token_count += int(output_tokens)
            if input_tokens := message.get("input_tokens"):
                total_prompt_token_count += int(input_tokens)

            # TODO(harrison): figure out if we should always assume the first message
            #  will always be a message output generally this block feels really
            #  brittle to imitate the current non streaming implementation.
            tool_idx = 0
            for content in message.get("content", []):
                # this is the current assumption of the non streaming implementation.
                if (content_type := content.get("type")) == "text":
                    yield (
                        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{idx}.{MessageAttributes.MESSAGE_CONTENT}",
                        content.get("text", ""),
                    )
                elif content_type == "tool_use":
                    yield (
                        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{idx}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_idx}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}",
                        content.get("tool_name", ""),
                    )
                    yield (
                        f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{idx}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{tool_idx}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}",
                        content.get("tool_input", "{}"),
                    )
                    tool_idx += 1
            idx += 1
        yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, total_completion_token_count
        yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, total_prompt_token_count
        yield (
            SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS,
            total_completion_token_count + total_prompt_token_count,
        )


class _ValuesAccumulator:
    __slots__ = ("_values",)

    def __init__(self, **values: Any) -> None:
        self._values: Dict[str, Any] = values

    def __iter__(self) -> Iterator[Tuple[str, Any]]:
        for key, value in self._values.items():
            if value is None:
                continue
            if isinstance(value, _ValuesAccumulator):
                if dict_value := dict(value):
                    yield key, dict_value
            elif isinstance(value, _IndexedAccumulator):
                list_value = list(value)
                if list_value:
                    yield key, list_value
            elif isinstance(value, _SimpleStringReplace):
                if str_value := str(value):
                    yield key, str_value
            elif isinstance(value, _StringAccumulator):
                if str_value := str(value):
                    yield key, str_value
            else:
                yield key, value

    def __iadd__(self, values: Optional[Mapping[str, Any]]) -> "_ValuesAccumulator":
        if not values:
            return self
        for key in self._values.keys():
            if (value := values.get(key)) is None:
                continue
            self_value = self._values[key]
            if isinstance(self_value, _ValuesAccumulator):
                if isinstance(value, Mapping):
                    self_value += value
            elif isinstance(self_value, _StringAccumulator):
                if isinstance(value, str):
                    self_value += value
            elif isinstance(self_value, _SimpleStringReplace):
                if isinstance(value, str):
                    self_value += value
            elif isinstance(self_value, _IndexedAccumulator):
                self_value += value
            elif isinstance(self_value, List) and isinstance(value, Iterable):
                self_value.extend(value)
            else:
                self._values[key] = value  # replacement
        for key in values.keys():
            if key in self._values or (value := values[key]) is None:
                continue
            value = deepcopy(value)
            if isinstance(value, Mapping):
                value = _ValuesAccumulator(**value)
            self._values[key] = value  # new entry
        return self


class _StringAccumulator:
    __slots__ = ("_fragments",)

    def __init__(self) -> None:
        self._fragments: List[str] = []

    def __str__(self) -> str:
        return "".join(self._fragments)

    def __iadd__(self, value: Optional[str]) -> "_StringAccumulator":
        if not value:
            return self
        self._fragments.append(value)
        return self


class _IndexedAccumulator:
    __slots__ = ("_indexed",)

    def __init__(self, factory: Callable[[], _ValuesAccumulator]) -> None:
        self._indexed: DefaultDict[int, _ValuesAccumulator] = defaultdict(factory)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        for _, values in sorted(self._indexed.items()):
            yield dict(values)

    def __iadd__(self, values: Optional[Mapping[str, Any]]) -> "_IndexedAccumulator":
        if (
            not values
            or not hasattr(values, "get")
            or (index := values.get("index")) is None
        ):
            return self
        self._indexed[index] += values
        return self


class _SimpleStringReplace:
    __slots__ = ("_str_val",)

    def __init__(self) -> None:
        self._str_val: str = ""

    def __str__(self) -> str:
        return self._str_val

    def __iadd__(self, value: Optional[str]) -> "_SimpleStringReplace":
        if not value:
            return self
        self._str_val = value
        return self
