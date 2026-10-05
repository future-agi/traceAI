"""A LangChain callback handler that traces an mcp-use ``MCPAgent`` run.

mcp-use 1.7.1 hands the ``callbacks`` list it was constructed with to the
LangGraph agent on every run (``MCPAgent.stream`` and
``MCPAgent._generate_response_chunks_async`` pass
``config={"callbacks": self.callbacks, ...}``). This handler turns the run
tree LangChain reports into three kinds of spans:

- one ``AGENT`` span for the outermost run the handler sees (the LangGraph
  graph that ``MCPAgent.run`` / ``stream`` / ``stream_events`` drives),
- one ``LLM`` span per chat-model (or completion-model) call,
- one ``TOOL`` span per tool call.

The graph's internal node and middleware runs are not spans; an LLM or
tool span's parent is its nearest traced ancestor, which for mcp-use is the
agent span. Nothing is patched: the handler only exists where it is passed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from uuid import UUID

from fi_instrumentation import FITracer, REDACTED_VALUE, TraceConfig
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, Status, StatusCode

from traceai_mcp_use._text import (
    MAX_ERROR_BYTES,
    MAX_NAME_BYTES,
    MAX_STACKTRACE_BYTES,
    MAX_VALUE_BYTES,
    KnownTexts,
    Secrets,
    cap,
    clean,
)
from traceai_mcp_use.version import __version__

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

AGENT_SPAN_NAME = "mcp_use.agent"

SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
OPERATION = SpanAttributes.GEN_AI_OPERATION_NAME
INPUT_VALUE = SpanAttributes.INPUT_VALUE
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
INPUT_MESSAGES = SpanAttributes.GEN_AI_INPUT_MESSAGES
OUTPUT_MESSAGES = SpanAttributes.GEN_AI_OUTPUT_MESSAGES
REQUEST_MODEL = SpanAttributes.GEN_AI_REQUEST_MODEL
RESPONSE_MODEL = SpanAttributes.GEN_AI_RESPONSE_MODEL
PROVIDER = SpanAttributes.GEN_AI_PROVIDER_NAME
TEMPERATURE = SpanAttributes.GEN_AI_REQUEST_TEMPERATURE
MAX_TOKENS = SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS
FINISH_REASONS = SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS
INPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS
CACHE_READ_TOKENS = SpanAttributes.GEN_AI_USAGE_CACHE_READ_TOKENS
TOOL_NAME = SpanAttributes.GEN_AI_TOOL_NAME
TOOL_CALL_ID = SpanAttributes.GEN_AI_TOOL_CALL_ID
TOOL_ARGUMENTS = SpanAttributes.GEN_AI_TOOL_CALL_ARGUMENTS
TOOL_RESULT = SpanAttributes.GEN_AI_TOOL_CALL_RESULT

LLM_CALL_COUNT = "mcp_use.agent.llm_call_count"
TOOL_CALL_COUNT = "mcp_use.agent.tool_call_count"
TOOL_ERROR_COUNT = "mcp_use.agent.tool_error_count"
INPUT_MESSAGE_COUNT = "mcp_use.llm.input_message_count"
REQUESTED_TOOL_CALLS = "mcp_use.llm.tool_call_count"
CHUNK_COUNT = "mcp_use.llm.chunk_count"
CHUNK_EVENT = "mcp_use.llm.chunk"
CHUNK_INDEX = "mcp_use.llm.chunk.index"
TOOL_ERROR_TYPE = "mcp_use.tool.error_type"
CANCELLED = "mcp_use.cancelled"
INCOMPLETE = "mcp_use.incomplete"

# At most this many messages are written per LLM span (the most recent ones);
# mcp_use.llm.input_message_count keeps the exact number.
MAX_MESSAGES = 32
# At most this many tool calls are written per output message.
MAX_TOOL_CALLS = 16
# At most this many chunk events per streamed LLM span; the count stays exact.
MAX_CHUNK_EVENTS = 128
# Open runs tracked at once across all agent runs of one handler. Past it,
# the oldest agent run is ended (mcp_use.incomplete) and forgotten.
MAX_OPEN_RUNS = 10_000

# mcp-use 1.7.1 returns a failed MCP tool call as this dict
# (mcp_use/errors/error_formatting.py format_error) instead of raising.
_MCP_USE_ERROR_KEYS = frozenset(("error", "details", "stack", "code"))

_ROLES = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}

_AGENT = "agent"
_LLM = "llm"
_TOOL = "tool"
_CHAIN = "chain"


@dataclass
class _Failure:
    type_name: str
    qualified: str
    message: Optional[str]
    stacktrace: Optional[str]
    # The message is the tool's own output (an MCP error result).
    from_output: bool = False


@dataclass
class _Root:
    inputs: KnownTexts = field(default_factory=KnownTexts)
    outputs: KnownTexts = field(default_factory=KnownTexts)
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0


@dataclass
class _Run:
    run_id: UUID
    kind: str
    root: UUID
    parent: Optional[UUID]
    span: Optional[Span] = None
    chunks: int = 0


def _describe(error: BaseException) -> str:
    try:
        return str(error)
    except Exception:
        return "<unprintable {0}>".format(type(error).__name__)


def _qualified(error: BaseException) -> str:
    error_type = type(error)
    module = getattr(error_type, "__module__", "")
    if module and module != "builtins":
        return "{0}.{1}".format(module, error_type.__qualname__)
    return error_type.__qualname__


def _failure_from_exception(error: BaseException) -> _Failure:
    stacktrace: Optional[str]
    try:
        stacktrace = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    except Exception:
        stacktrace = None
    return _Failure(type(error).__name__, _qualified(error), _describe(error), stacktrace)


def _is_cancellation(error: BaseException) -> bool:
    # asyncio cancellation, or a consumer that stopped reading MCPAgent.stream()
    # (LangGraph reports the closed generator as GeneratorExit).
    return isinstance(error, (asyncio.CancelledError, GeneratorExit))


def _text_of(content: Any) -> Optional[str]:
    """Plain text of a LangChain message content (str or list of blocks)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _role(message: Any) -> str:
    if isinstance(message, Mapping):
        role = message.get("role") or message.get("type")
        return _ROLES.get(role, role) if isinstance(role, str) else "unknown"
    kind = getattr(message, "type", None)
    if isinstance(kind, str):
        return _ROLES.get(kind, kind)
    return "unknown"


def _content(message: Any) -> Optional[str]:
    """Text of a LangChain message object or a {"role", "content"} dict."""
    if isinstance(message, Mapping):
        return _text_of(message.get("content"))
    return _text_of(getattr(message, "content", None))


def _tool_calls(message: Any) -> List[Mapping[str, Any]]:
    if isinstance(message, Mapping):
        calls = message.get("tool_calls")
    else:
        calls = getattr(message, "tool_calls", None)
    if isinstance(calls, list):
        return [call for call in calls if isinstance(call, Mapping)]
    return []


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _strings_in(value: Any, depth: int = 0) -> Iterable[str]:
    """Every string inside a tool-argument structure (bounded depth)."""
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings_in(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings_in(item, depth + 1)


def _mcp_use_error(output: Any) -> Optional[Mapping[str, Any]]:
    """The mcp-use formatted error a tool returned, if this output is one."""
    candidate: Any = output
    content = getattr(output, "content", None)
    if content is not None and not isinstance(output, Mapping):
        candidate = content
    if isinstance(candidate, str):
        stripped = candidate.strip()
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return None
        try:
            candidate = json.loads(stripped)
        except ValueError:
            return None
    if (
        isinstance(candidate, Mapping)
        and _MCP_USE_ERROR_KEYS.issubset(candidate.keys())
        and isinstance(candidate.get("error"), str)
    ):
        return candidate
    return None


def _tool_output_text(output: Any) -> Optional[str]:
    content = getattr(output, "content", None)
    if content is not None:
        return _text_of(content)
    if isinstance(output, str):
        return output
    if output is None:
        return None
    return _json(output)


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


class FutureAGICallback(BaseCallbackHandler):
    """Trace an mcp-use agent: pass it as ``MCPAgent(..., callbacks=[...])``.

    Args:
        tracer_provider: the provider spans go to; defaults to the global
            provider. Pass the one ``fi_instrumentation.register()`` returned.
        config: a ``fi_instrumentation.TraceConfig``. ``hide_inputs``,
            ``hide_outputs`` and ``pii_redaction`` (or ``FI_HIDE_INPUTS``,
            ``FI_HIDE_OUTPUTS``, ``FI_PII_REDACTION``) apply to every text
            this handler writes.
        capture_content: record prompts, model output, tool arguments, tool
            results and error messages. Off by default: spans then carry
            names, kinds, model, usage, counts, statuses and exception types.
        redact: secret strings to remove from everything written (for
            example an LLM key or an MCP server header the environment does
            not hold).
    """

    raise_error = False
    # Run on the event loop, not in an executor thread. LangChain otherwise
    # hands each sync callback to run_in_executor; a cancelled run can then
    # deliver on_chain_error for a parent while on_tool_start for its child
    # is still running in a thread, and the child span would be orphaned.
    # Inline, every callback runs to completion in order. The handler does
    # no I/O of its own; span export is the span processor's.
    run_inline = True

    def __init__(
        self,
        tracer_provider: Optional[trace_api.TracerProvider] = None,
        config: Optional[TraceConfig] = None,
        capture_content: bool = False,
        redact: Iterable[str] = (),
    ) -> None:
        super().__init__()
        if config is None:
            config = TraceConfig()
        elif not isinstance(config, TraceConfig):
            raise TypeError(
                "config must be a fi_instrumentation.TraceConfig, got {0}".format(
                    type(config).__name__
                )
            )
        if not isinstance(capture_content, bool):
            raise TypeError(
                "capture_content must be a bool, got {0}".format(type(capture_content).__name__)
            )
        if isinstance(redact, str):
            raise TypeError("redact must be an iterable of strings, not a string")
        secrets = list(redact)
        for value in secrets:
            if not isinstance(value, str):
                raise TypeError(
                    "redact must contain strings, got {0}".format(type(value).__name__)
                )
        provider = tracer_provider or trace_api.get_tracer_provider()
        # FITracer applies the config's masking and PII pass to attributes and
        # stamps using_session / using_user / using_metadata / using_tags.
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, provider), config=config
        )
        self._capture = capture_content
        self._hide_inputs = bool(config.hide_inputs)
        self._hide_outputs = bool(config.hide_outputs)
        self._pii = bool(config.pii_redaction)
        self._secrets = Secrets(secrets)
        self._lock = threading.RLock()
        self._runs: Dict[UUID, _Run] = {}
        self._roots: "OrderedDict[UUID, _Root]" = OrderedDict()

    # ------------------------------------------------------------------ text

    def _scrubs(self, root: Optional[_Root]) -> List[KnownTexts]:
        if root is None:
            return []
        scrubs: List[KnownTexts] = []
        if self._hide_inputs:
            scrubs.append(root.inputs)
        if self._hide_outputs:
            scrubs.append(root.outputs)
        return scrubs

    def _clean(
        self, text: Optional[str], root: Optional[_Root], limit: int, tail: bool = False
    ) -> Optional[str]:
        if not isinstance(text, str):
            return None
        return clean(text, self._secrets, limit, self._pii, self._scrubs(root), tail)

    def _name(self, text: Any) -> Optional[str]:
        """An identifier (model, tool name, id): secrets removed, 256-byte cap."""
        if not isinstance(text, str) or not text:
            return None
        return cap(self._secrets.redact(text), MAX_NAME_BYTES)

    def _remember(self, root: Optional[_Root], texts: Iterable[Optional[str]], output: bool) -> None:
        if root is None or not self._capture:
            return
        if output and not self._hide_outputs:
            return
        if not output and not self._hide_inputs:
            return
        known = root.outputs if output else root.inputs
        with self._lock:
            for text in texts:
                if isinstance(text, str):
                    known.add(text, self._secrets)

    # ------------------------------------------------------------ bookkeeping

    def _begin(
        self,
        run_id: UUID,
        parent_run_id: Optional[UUID],
        kind: str,
        name: str,
        attributes: Dict[str, Any],
    ) -> Tuple[Optional[_Run], Optional[_Root]]:
        """Track a run and start its span (none for collapsed chain runs)."""
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return None, None
        evicted: List[_Run] = []
        with self._lock:
            parent = self._runs.get(parent_run_id) if parent_run_id is not None else None
            if parent is None:
                root_id = run_id
                if kind == _CHAIN:
                    kind = _AGENT
                self._roots[root_id] = _Root()
            else:
                root_id = parent.root
            root = self._roots.get(root_id)
            if root is None:  # the root was evicted while this run started
                return None, None
            if kind == _LLM:
                root.llm_calls += 1
            elif kind == _TOOL:
                root.tool_calls += 1
            run = _Run(run_id=run_id, kind=kind, root=root_id, parent=parent_run_id)
            self._runs[run_id] = run
            traced_parent = self._traced_ancestor(parent)
            while len(self._runs) > MAX_OPEN_RUNS and len(self._roots) > 1:
                oldest = next(iter(self._roots))
                if oldest == root_id:
                    break
                evicted.extend(self._forget_root(oldest))
        for stale in evicted:
            self._end_incomplete(stale, None)
        if kind == _CHAIN:
            return run, root
        if kind == _AGENT:
            name = AGENT_SPAN_NAME
        try:
            parent_context = (
                trace_api.set_span_in_context(traced_parent.span)
                if traced_parent is not None and traced_parent.span is not None
                else None
            )
            run.span = self._tracer.start_span(name, context=parent_context, attributes=attributes)
        except Exception:
            logger.debug("Could not start the mcp-use span", exc_info=True)
            run.span = None
        return run, root

    def _traced_ancestor(self, run: Optional[_Run]) -> Optional[_Run]:
        while run is not None:
            if run.kind != _CHAIN:
                return run
            run = self._runs.get(run.parent) if run.parent is not None else None
        return None

    def _forget_root(self, root_id: UUID) -> List[_Run]:
        """Drop an agent run and every run under it; caller holds the lock."""
        self._roots.pop(root_id, None)
        dropped = [run for run in self._runs.values() if run.root == root_id]
        for run in dropped:
            self._runs.pop(run.run_id, None)
        return [run for run in dropped if run.span is not None]

    def _descends(self, run: _Run, ancestor: UUID) -> bool:
        parent = run.parent
        while parent is not None:
            if parent == ancestor:
                return True
            above = self._runs.get(parent)
            if above is None:
                return False
            parent = above.parent
        return False

    def _pop(self, run_id: UUID) -> Tuple[Optional[_Run], Optional[_Root], List[_Run]]:
        """Remove a finished run and any still-open run below it."""
        with self._lock:
            run = self._runs.pop(run_id, None)
            if run is None:
                return None, None, []
            orphans = [other for other in self._runs.values() if self._descends(other, run_id)]
            for orphan in orphans:
                self._runs.pop(orphan.run_id, None)
            root = self._roots.get(run.root)
            if run.run_id == run.root:
                self._roots.pop(run.root, None)
                for leftover in [other for other in self._runs.values() if other.root == run.root]:
                    self._runs.pop(leftover.run_id, None)
                    orphans.append(leftover)
        return run, root, [orphan for orphan in orphans if orphan.span is not None]

    # ----------------------------------------------------------------- ending

    def _set(self, span: Span, key: str, value: Any) -> None:
        if value is None:
            return
        try:
            span.set_attribute(key, value)
        except Exception:
            logger.debug("Could not set %s", key, exc_info=True)

    def _end(self, span: Optional[Span]) -> None:
        if span is None:
            return
        try:
            span.end()
        except Exception:
            logger.debug("Could not end the mcp-use span", exc_info=True)

    def _ok(self, span: Span) -> None:
        try:
            span.set_status(Status(StatusCode.OK))
        except Exception:
            logger.debug("Could not set the span status", exc_info=True)

    def _cancelled(self, span: Span) -> None:
        # Cancellation is not an exception: no event, no message.
        self._set(span, CANCELLED, True)
        try:
            span.set_status(Status(StatusCode.ERROR, "cancelled"))
        except Exception:
            logger.debug("Could not mark the span cancelled", exc_info=True)

    def _end_incomplete(self, run: _Run, cause: Optional[BaseException]) -> None:
        """End a span whose own end callback never came (its parent ended first)."""
        span = run.span
        if span is None:
            return
        if cause is not None and _is_cancellation(cause):
            self._cancelled(span)
        else:
            self._set(span, INCOMPLETE, True)
            try:
                span.set_status(Status(StatusCode.ERROR, "ended without a result"))
            except Exception:
                logger.debug("Could not set the span status", exc_info=True)
        self._end(span)

    def _record_failure(self, span: Span, failure: _Failure, root: Optional[_Root]) -> None:
        type_name = self._name(failure.type_name) or "Error"
        qualified = self._name(failure.qualified) or type_name
        message: Optional[str] = None
        stacktrace: Optional[str] = None
        if self._capture and not (failure.from_output and self._hide_outputs):
            # Error text can quote prompts, tool arguments and tool results,
            # so it is content: recorded only with capture_content, and with
            # every hidden input/output removed from it.
            message = self._clean(failure.message, root, MAX_ERROR_BYTES)
            stacktrace = self._clean(failure.stacktrace, root, MAX_STACKTRACE_BYTES, tail=True)
        description = "{0}: {1}".format(type_name, message) if message else type_name
        try:
            span.set_status(Status(StatusCode.ERROR, description))
        except Exception:
            logger.debug("Could not set the error status", exc_info=True)
        event: Dict[str, Any] = {"exception.type": qualified}
        if message:
            event["exception.message"] = message
        if stacktrace:
            event["exception.stacktrace"] = stacktrace
        try:
            span.add_event("exception", event)
        except Exception:
            logger.debug("Could not record the exception", exc_info=True)

    def _finish_error(self, run_id: UUID, error: BaseException) -> None:
        run, root, orphans = self._pop(run_id)
        if run is None:
            return
        for orphan in orphans:
            self._end_incomplete(orphan, error)
        if run.kind == _TOOL and root is not None and not _is_cancellation(error):
            with self._lock:
                root.tool_errors += 1
        span = run.span
        if span is None:
            return
        if run.kind == _AGENT:
            self._agent_counts(span, root)
        if run.kind == _LLM and run.chunks:
            self._set(span, CHUNK_COUNT, run.chunks)
        if _is_cancellation(error):
            self._cancelled(span)
        else:
            self._record_failure(span, _failure_from_exception(error), root)
        self._end(span)

    def _agent_counts(self, span: Span, root: Optional[_Root]) -> None:
        if root is None:
            return
        self._set(span, LLM_CALL_COUNT, root.llm_calls)
        self._set(span, TOOL_CALL_COUNT, root.tool_calls)
        self._set(span, TOOL_ERROR_COUNT, root.tool_errors)

    # ------------------------------------------------------------- callbacks

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            attributes = {
                SPAN_KIND: FiSpanKindValues.AGENT.value,
                OPERATION: "invoke_agent",
            }
            run, root = self._begin(run_id, parent_run_id, _CHAIN, AGENT_SPAN_NAME, attributes)
            if run is None or run.kind != _AGENT or run.span is None:
                return
            messages = inputs.get("messages") if isinstance(inputs, Mapping) else None
            texts = [_content(m) for m in messages or []] if isinstance(
                messages, list
            ) else []
            self._remember(root, texts, output=False)
            if not self._capture:
                return
            if self._hide_inputs:
                self._set(run.span, INPUT_VALUE, REDACTED_VALUE)
                return
            query = None
            for message in reversed(messages or []) if isinstance(messages, list) else []:
                if _role(message) == "user":
                    query = _content(message)
                    break
            self._set(run.span, INPUT_VALUE, self._clean(query, root, MAX_VALUE_BYTES))
        except Exception:
            logger.debug("mcp-use callback on_chain_start failed", exc_info=True)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            run, root, orphans = self._pop(run_id)
            if run is None:
                return
            for orphan in orphans:
                self._end_incomplete(orphan, None)
            if run.kind != _AGENT or run.span is None:
                return
            span = run.span
            self._agent_counts(span, root)
            if self._capture:
                answer = None
                messages = outputs.get("messages") if isinstance(outputs, Mapping) else None
                for message in reversed(messages) if isinstance(messages, list) else []:
                    if _role(message) == "assistant" and not _tool_calls(message):
                        answer = _content(message)
                        break
                if answer is not None:
                    if self._hide_outputs:
                        self._set(span, OUTPUT_VALUE, REDACTED_VALUE)
                    else:
                        self._set(span, OUTPUT_VALUE, self._clean(answer, root, MAX_VALUE_BYTES))
            self._ok(span)
            self._end(span)
        except Exception:
            logger.debug("mcp-use callback on_chain_end failed", exc_info=True)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish_error(run_id, error)
        except Exception:
            logger.debug("mcp-use callback on_chain_error failed", exc_info=True)

    # Retriever runs are not spans, but their children need a known parent.
    def on_retriever_start(
        self,
        serialized: Dict[str, Any],
        query: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            if parent_run_id is not None:
                with self._lock:
                    known = parent_run_id in self._runs
                if known:
                    self._begin(run_id, parent_run_id, _CHAIN, "", {})
        except Exception:
            logger.debug("mcp-use callback on_retriever_start failed", exc_info=True)

    def on_retriever_end(self, documents: Any, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            self._pop(run_id)
        except Exception:
            logger.debug("mcp-use callback on_retriever_end failed", exc_info=True)

    def on_retriever_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            self._pop(run_id)
        except Exception:
            logger.debug("mcp-use callback on_retriever_error failed", exc_info=True)

    def _llm_attributes(
        self, operation: str, kwargs: Mapping[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        metadata = kwargs.get("metadata") or {}
        params = kwargs.get("invocation_params") or {}
        model = None
        for source, key in (
            (metadata, "ls_model_name"),
            (params, "model"),
            (params, "model_name"),
            (params, "model_id"),
        ):
            if isinstance(source, Mapping) and isinstance(source.get(key), str) and source.get(key):
                model = source.get(key)
                break
        attributes: Dict[str, Any] = {
            SPAN_KIND: FiSpanKindValues.LLM.value,
            OPERATION: operation,
        }
        model_name = self._name(model)
        if model_name:
            attributes[REQUEST_MODEL] = model_name
        if isinstance(metadata, Mapping):
            provider = self._name(metadata.get("ls_provider"))
            if provider:
                attributes[PROVIDER] = provider
            temperature = _number(metadata.get("ls_temperature"))
            if temperature is not None:
                attributes[TEMPERATURE] = temperature
            max_tokens = _int(metadata.get("ls_max_tokens"))
            if max_tokens is not None:
                attributes[MAX_TOKENS] = max_tokens
        name = "{0} {1}".format(operation, model_name) if model_name else operation
        return name, attributes

    def _message_attributes(
        self, prefix: str, messages: Sequence[Any], root: Optional[_Root]
    ) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {}
        for index, message in enumerate(list(messages)[-MAX_MESSAGES:]):
            base = "{0}.{1}.message".format(prefix, index)
            attributes[base + ".role"] = _role(message)
            text = self._clean(_content(message), root, MAX_VALUE_BYTES)
            if text:
                attributes[base + ".content"] = text
            for call_index, call in enumerate(_tool_calls(message)[:MAX_TOOL_CALLS]):
                call_base = "{0}.tool_calls.{1}.tool_call".format(base, call_index)
                call_name = self._name(call.get("name"))
                if call_name:
                    attributes[call_base + ".function.name"] = call_name
                call_id = self._name(call.get("id"))
                if call_id:
                    attributes[call_base + ".id"] = call_id
                # Tool-call arguments are the tool's input, also when they
                # appear in the model's output: hide_inputs drops them.
                if self._hide_inputs:
                    continue
                arguments = self._clean(_json(call.get("args")), root, MAX_VALUE_BYTES)
                if arguments:
                    attributes[call_base + ".function.arguments"] = arguments
        return attributes

    def _start_llm(
        self,
        operation: str,
        messages: Sequence[Any],
        run_id: UUID,
        parent_run_id: Optional[UUID],
        kwargs: Mapping[str, Any],
    ) -> None:
        name, attributes = self._llm_attributes(operation, kwargs)
        attributes[INPUT_MESSAGE_COUNT] = len(messages)
        run, root = self._begin(run_id, parent_run_id, _LLM, name, attributes)
        if run is None or run.span is None:
            return
        texts: List[Optional[str]] = []
        for message in messages:
            texts.append(_content(message))
            for call in _tool_calls(message):
                texts.extend(_strings_in(call.get("args")))
        self._remember(root, texts, output=False)
        if not self._capture:
            return
        if self._hide_inputs:
            self._set(run.span, INPUT_VALUE, REDACTED_VALUE)
            return
        for key, value in self._message_attributes(INPUT_MESSAGES, messages, root).items():
            self._set(run.span, key, value)
        if messages:
            last = self._clean(_content(messages[-1]), root, MAX_VALUE_BYTES)
            if last:
                self._set(run.span, INPUT_VALUE, last)

    def on_chat_model_start(
        self,
        serialized: Dict[str, Any],
        messages: List[List[Any]],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            first = messages[0] if messages else []
            self._start_llm("chat", list(first), run_id, parent_run_id, kwargs)
        except Exception:
            logger.debug("mcp-use callback on_chat_model_start failed", exc_info=True)

    def on_llm_start(
        self,
        serialized: Dict[str, Any],
        prompts: List[str],
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            from langchain_core.messages import HumanMessage

            messages = [HumanMessage(content=prompt) for prompt in prompts[:1]]
            self._start_llm("text_completion", messages, run_id, parent_run_id, kwargs)
        except Exception:
            logger.debug("mcp-use callback on_llm_start failed", exc_info=True)

    def on_llm_new_token(
        self,
        token: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        # One event per chunk (index only, never the text), up to
        # MAX_CHUNK_EVENTS; mcp_use.llm.chunk_count keeps the exact number.
        try:
            with self._lock:
                run = self._runs.get(run_id)
                if run is None or run.span is None:
                    return
                run.chunks += 1
                index = run.chunks - 1
            if index < MAX_CHUNK_EVENTS:
                run.span.add_event(CHUNK_EVENT, {CHUNK_INDEX: index})
        except Exception:
            logger.debug("mcp-use callback on_llm_new_token failed", exc_info=True)

    def on_llm_end(
        self,
        response: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            run, root, orphans = self._pop(run_id)
            if run is None:
                return
            for orphan in orphans:
                self._end_incomplete(orphan, None)
            span = run.span
            if span is None:
                return
            if run.chunks:
                self._set(span, CHUNK_COUNT, run.chunks)
            try:
                self._llm_response(span, response, root)
            except Exception:
                logger.debug("Could not read the LLM response", exc_info=True)
            self._ok(span)
            self._end(span)
        except Exception:
            logger.debug("mcp-use callback on_llm_end failed", exc_info=True)

    def _llm_response(self, span: Span, response: Any, root: Optional[_Root]) -> None:
        generations = getattr(response, "generations", None) or []
        first = list(generations[0]) if generations else []
        llm_output = getattr(response, "llm_output", None) or {}
        messages: List[Any] = []
        reasons: List[str] = []
        usage: Optional[Mapping[str, Any]] = None
        response_model: Any = None
        for generation in first:
            message = getattr(generation, "message", None)
            if message is None:
                from langchain_core.messages import AIMessage

                message = AIMessage(content=getattr(generation, "text", "") or "")
            messages.append(message)
            metadata = getattr(message, "response_metadata", None) or {}
            info = getattr(generation, "generation_info", None) or {}
            for source in (metadata, info):
                reason = source.get("finish_reason") or source.get("stop_reason")
                if isinstance(reason, str) and reason:
                    reasons.append(reason)
                    break
            response_model = response_model or metadata.get("model_name") or metadata.get("model")
            usage = usage or getattr(message, "usage_metadata", None)
        if isinstance(llm_output, Mapping):
            response_model = response_model or llm_output.get("model_name")
        name = self._name(response_model)
        if name:
            self._set(span, RESPONSE_MODEL, name)
        if reasons:
            self._set(span, FINISH_REASONS, [self._name(reason) for reason in reasons])
        if isinstance(usage, Mapping):
            self._set(span, INPUT_TOKENS, _int(usage.get("input_tokens")))
            self._set(span, OUTPUT_TOKENS, _int(usage.get("output_tokens")))
            self._set(span, TOTAL_TOKENS, _int(usage.get("total_tokens")))
            details = usage.get("input_token_details")
            if isinstance(details, Mapping):
                self._set(span, CACHE_READ_TOKENS, _int(details.get("cache_read")))
        elif isinstance(llm_output, Mapping) and isinstance(llm_output.get("token_usage"), Mapping):
            token_usage = llm_output["token_usage"]
            self._set(span, INPUT_TOKENS, _int(token_usage.get("prompt_tokens")))
            self._set(span, OUTPUT_TOKENS, _int(token_usage.get("completion_tokens")))
            self._set(span, TOTAL_TOKENS, _int(token_usage.get("total_tokens")))
        if messages:
            self._set(span, REQUESTED_TOOL_CALLS, sum(len(_tool_calls(m)) for m in messages))
        texts: List[Optional[str]] = []
        arguments: List[Optional[str]] = []
        for message in messages:
            texts.append(_content(message))
            for call in _tool_calls(message):
                arguments.extend(_strings_in(call.get("args")))
        self._remember(root, texts + arguments, output=True)
        # The arguments the model chose are the next tool call's input.
        self._remember(root, arguments, output=False)
        if not self._capture or not messages:
            return
        if self._hide_outputs:
            self._set(span, OUTPUT_VALUE, REDACTED_VALUE)
            return
        for key, value in self._message_attributes(OUTPUT_MESSAGES, messages, root).items():
            self._set(span, key, value)
        answer = self._clean(_content(messages[0]), root, MAX_VALUE_BYTES)
        if answer:
            self._set(span, OUTPUT_VALUE, answer)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish_error(run_id, error)
        except Exception:
            logger.debug("mcp-use callback on_llm_error failed", exc_info=True)

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        inputs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        try:
            tool = None
            if isinstance(serialized, Mapping):
                tool = serialized.get("name")
            tool = self._name(tool or kwargs.get("name")) or "unknown"
            attributes: Dict[str, Any] = {
                SPAN_KIND: FiSpanKindValues.TOOL.value,
                OPERATION: "execute_tool",
                TOOL_NAME: tool,
            }
            call_id = self._name(kwargs.get("tool_call_id"))
            if call_id:
                attributes[TOOL_CALL_ID] = call_id
            run, root = self._begin(
                run_id, parent_run_id, _TOOL, "execute_tool {0}".format(tool), attributes
            )
            if run is None or run.span is None:
                return
            arguments = _json(inputs) if isinstance(inputs, Mapping) else input_str
            texts: List[Optional[str]] = [arguments, input_str]
            if isinstance(inputs, Mapping):
                texts.extend(_strings_in(inputs))
            self._remember(root, texts, output=False)
            if not self._capture:
                return
            if self._hide_inputs:
                self._set(run.span, INPUT_VALUE, REDACTED_VALUE)
                return
            text = self._clean(arguments, root, MAX_VALUE_BYTES)
            self._set(run.span, TOOL_ARGUMENTS, text)
            self._set(run.span, INPUT_VALUE, text)
        except Exception:
            logger.debug("mcp-use callback on_tool_start failed", exc_info=True)

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            run, root, orphans = self._pop(run_id)
            if run is None:
                return
            for orphan in orphans:
                self._end_incomplete(orphan, None)
            failure: Optional[_Failure] = None
            try:
                formatted = _mcp_use_error(output)
                if formatted is not None:
                    error_type = str(formatted.get("error"))
                    details = formatted.get("details")
                    stack = formatted.get("stack")
                    failure = _Failure(
                        error_type,
                        error_type,
                        details if isinstance(details, str) else None,
                        stack if isinstance(stack, str) else None,
                        from_output=True,
                    )
                elif getattr(output, "status", None) == "error":
                    failure = _Failure(
                        "ToolException",
                        "langchain_core.tools.ToolException",
                        _tool_output_text(output),
                        None,
                        from_output=True,
                    )
            except Exception:
                logger.debug("Could not classify the tool result", exc_info=True)
            text = _tool_output_text(output)
            self._remember(root, [text], output=True)
            if failure is not None and root is not None:
                with self._lock:
                    root.tool_errors += 1
            span = run.span
            if span is None:
                return
            if failure is not None:
                self._set(span, TOOL_ERROR_TYPE, self._name(failure.type_name))
                self._record_failure(span, failure, root)
            else:
                if self._capture:
                    if self._hide_outputs:
                        self._set(span, OUTPUT_VALUE, REDACTED_VALUE)
                    else:
                        cleaned = self._clean(text, root, MAX_VALUE_BYTES)
                        self._set(span, TOOL_RESULT, cleaned)
                        self._set(span, OUTPUT_VALUE, cleaned)
                self._ok(span)
            self._end(span)
        except Exception:
            logger.debug("mcp-use callback on_tool_end failed", exc_info=True)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: Optional[UUID] = None,
        **kwargs: Any,
    ) -> None:
        try:
            self._finish_error(run_id, error)
        except Exception:
            logger.debug("mcp-use callback on_tool_error failed", exc_info=True)
