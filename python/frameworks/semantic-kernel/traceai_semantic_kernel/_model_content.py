"""Put model-call prompts and completions on Semantic Kernel's chat spans.

With sensitive diagnostics on, Semantic Kernel writes a model call's messages
as INFO log records while the chat span is current
(``model_diagnostics/decorators.py`` ``_set_completion_input`` and
``_set_completion_response``, lines 366-447 at 1.45.0), never as span
attributes. :class:`ModelContentLogger` stands in for that module's
``logger`` while ``instrument(sensitive=True)`` is active: it appends each
record's message to the current chat span as ``gen_ai.input.messages`` /
``gen_ai.output.messages`` (JSON lists) and then passes the call to the real
logger unchanged. The span processor applies the content rules to those keys
like any other content key.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional

from opentelemetry import trace as trace_api

from .processor import INPUT_MESSAGES, OPERATION, OUTPUT_MESSAGES, _is_semantic_kernel

logger = logging.getLogger(__name__)

# The logger this replaces lives in this module (semantic-kernel 1.38.0-1.45.0).
MODEL_DIAGNOSTICS_MODULE = "semantic_kernel.utils.telemetry.model_diagnostics.decorators"

# ``extra["event.name"]`` values (model_diagnostics/gen_ai_attributes.py).
_EVENT_NAME = "event.name"
_INPUT_EVENTS = frozenset(
    {"gen_ai.system.message", "gen_ai.user.message", "gen_ai.assistant.message", "gen_ai.tool.message"}
)
_PROMPT_EVENT = "gen_ai.prompt"  # text completion: the record is the raw prompt
_CHOICE_EVENT = "gen_ai.choice"  # {"message": {...}, "finish_reason": ..., "index": ...}
_MODEL_OPERATIONS = ("chat", "text_completions")


def _parse(text: Any) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _append(span: Any, key: str, item: Any) -> None:
    current = _parse((span.attributes or {}).get(key))
    items = current if isinstance(current, list) else []
    items.append(item)
    span.set_attribute(key, json.dumps(items, ensure_ascii=False, default=str))


def record_on_current_span(msg: Any, extra: Optional[Mapping[str, Any]]) -> None:
    """Append one Semantic Kernel content record to the current chat span."""
    event = (extra or {}).get(_EVENT_NAME)
    if event not in _INPUT_EVENTS and event not in (_PROMPT_EVENT, _CHOICE_EVENT):
        return
    span = trace_api.get_current_span()
    if not span.is_recording() or not _is_semantic_kernel(span):
        return
    if (getattr(span, "attributes", None) or {}).get(OPERATION) not in _MODEL_OPERATIONS:
        return
    if event == _PROMPT_EVENT:
        _append(span, INPUT_MESSAGES, {"role": "user", "content": str(msg)})
    elif event == _CHOICE_EVENT:
        choice = _parse(msg)
        message = choice.get("message") if isinstance(choice, dict) else None
        _append(span, OUTPUT_MESSAGES, message if message is not None else str(msg))
    else:
        message = _parse(msg)
        _append(span, INPUT_MESSAGES, message if message is not None else str(msg))


class ModelContentLogger:
    """Proxy for Semantic Kernel's model-diagnostics logger (``sensitive=True`` only).

    ``info`` records the message on the current chat span, then calls the real
    logger's ``info`` with the same arguments. Everything else is the real
    logger's.
    """

    def __init__(self, wrapped: logging.Logger) -> None:
        self._wrapped = wrapped

    @property
    def wrapped(self) -> logging.Logger:
        return self._wrapped

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        try:
            record_on_current_span(msg, kwargs.get("extra"))
        except Exception:
            logger.debug("traceai-semantic-kernel: could not record model content", exc_info=True)
        self._wrapped.info(msg, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)
