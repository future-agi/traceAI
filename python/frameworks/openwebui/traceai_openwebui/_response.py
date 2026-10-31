"""Observe provider responses, never reconstruct operations from a chat transcript."""

from __future__ import annotations

import codecs
import json
from typing import Any

from opentelemetry.trace import Span

_MAX_FRAME = 1_048_576


def is_chat_completion(value: Any) -> bool:
    """Require completed messages, not just a JSON object or streaming deltas.

    Compatible providers can omit IDs, timestamps, usage, role and finish_reason.
    Empty/filtered content, tool calls, refusals and audio are still completions.
    """
    if not isinstance(value, dict):
        return False
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            return False
        message = choice.get("message")
        if not isinstance(message, dict):
            return False
        if "content" in message:
            if message["content"] is not None and not isinstance(
                message["content"], str
            ):
                return False
        elif isinstance(message.get("refusal"), str):
            continue
        elif isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
            if not all(
                isinstance(call, dict) and call for call in message["tool_calls"]
            ):
                return False
        elif any(
            isinstance(message.get(key), dict) and message[key]
            for key in ("function_call", "audio")
        ):
            continue
        else:
            return False
    return True


class Observation:
    def __init__(
        self,
        span: Span,
        capture_content: bool,
        max_chars: int,
        choice_count: Any = None,
    ) -> None:
        self.span = span
        self.capture_content = capture_content
        self.max_chars = max_chars
        self.usage: dict[str, int] = {}
        self.output = ""
        self._terminal = False
        self._choice_count = (
            choice_count if type(choice_count) is int and choice_count > 0 else 0
        )
        self._finished_choices: set[int] = set()
        self._ambiguous_choices = self._choice_count == 0
        self.failed = False
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._data: list[str] = []
        self._size = 0
        self._dropping = False

    @property
    def terminal(self) -> bool:
        return self._terminal or (
            not self._ambiguous_choices
            and len(self._finished_choices) == self._choice_count
        )

    def payload(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        if value.get("error") or value.get("type") in {"error", "response.failed"}:
            self.failed = True
        response = value.get("response")
        if isinstance(response, dict):
            if response.get("error") or response.get("status") in {
                "failed",
                "incomplete",
                "cancelled",
            }:
                self.failed = True
            self._usage(response.get("usage"))
        self._usage(value.get("usage"))
        if value.get("type") in {
            "response.completed",
            "response.failed",
            "response.incomplete",
        }:
            self._terminal = True
            if value["type"] != "response.completed":
                self.failed = True
        if value.get("type") == "response.output_text.delta":
            self._text(value.get("delta"))
        choices = value.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    self._ambiguous_choices = True
                    continue
                # A choice finishing is not a request-level terminator. Account
                # for n even when a missing choice never appears on the wire.
                index = choice.get(
                    "index",
                    0 if self._choice_count == 1 and len(choices) == 1 else None,
                )
                if type(index) is not int or not 0 <= index < self._choice_count:
                    # Cannot correlate ambiguous choices; require [DONE] instead.
                    self._ambiguous_choices = True
                elif (
                    isinstance(choice.get("finish_reason"), str)
                    and choice["finish_reason"]
                ):
                    self._finished_choices.add(index)
                else:
                    self._finished_choices.discard(index)
                message = choice.get("delta") or choice.get("message")
                if isinstance(message, dict):
                    self._text(message.get("content"))
        # Responses API non-streaming output. Streamed output is captured only
        # from deltas, not again from response.completed's repeated output.
        if isinstance(value.get("output"), list):
            for item in value["output"]:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "message"
                    and isinstance(item.get("content"), list)
                ):
                    for part in item["content"]:
                        if isinstance(part, dict) and part.get("type") == "output_text":
                            self._text(part.get("text"))

    def _usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        for key, aliases in (
            ("input", ("prompt_tokens", "input_tokens")),
            ("output", ("completion_tokens", "output_tokens")),
        ):
            for alias in aliases:
                value = usage.get(alias)
                if type(value) is int and 0 <= value < 2**63:
                    # A provider's usage object is a snapshot, not a delta.
                    self.usage[key] = value
                    break

    def _text(self, value: Any) -> None:
        if self.capture_content and isinstance(value, str):
            self.output += value[: max(0, self.max_chars - len(self.output))]

    def chunk(self, chunk: str | bytes) -> None:
        text = self._decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        if not isinstance(text, str):
            return
        # Do not retain an oversized event or parse its suffix as a new event.
        for piece in text.splitlines(keepends=True):
            self._size += len(piece)
            if self._size > _MAX_FRAME:
                self._dropping = True
                self._buffer = ""
                self._data.clear()
            if not self._dropping:
                self._buffer += piece
            if piece.endswith("\n"):
                line = self._buffer.rstrip("\r\n")
                if self._dropping:
                    # A blank physical line terminates the dropped SSE frame.
                    if piece in {"\n", "\r\n"}:
                        self._reset()
                elif not line:
                    self._frame()
                    self._reset()
                elif line.startswith("data:"):
                    self._data.append(line[5:].lstrip(" "))
                self._buffer = ""

    def _reset(self) -> None:
        self._buffer = ""
        self._data.clear()
        self._size = 0
        self._dropping = False

    def _frame(self) -> None:
        data = "\n".join(self._data).strip()
        if data == "[DONE]":
            self._terminal = True
        elif data:
            try:
                self.payload(json.loads(data))
            except (ValueError, TypeError):
                pass

    def finish(self) -> None:
        if not self._dropping:
            # Native Pipe wrappers can end with an unterminated [DONE] line.
            if self._buffer.startswith("data:"):
                self._data.append(self._buffer[5:].strip())
            self._frame()
        self._reset()
        for key, count in self.usage.items():
            self.span.set_attribute(f"gen_ai.usage.{key}_tokens", count)
            legacy = "prompt" if key == "input" else "completion"
            self.span.set_attribute(f"llm.token_count.{legacy}", count)
        if len(self.usage) == 2 and sum(self.usage.values()) < 2**63:
            self.span.set_attribute("llm.token_count.total", sum(self.usage.values()))
        if not self.usage:
            self.span.set_attribute("gen_ai.usage.unavailable", True)
        if self.capture_content:
            self.span.set_attribute("output.value", self.output)
