"""Bounded, opt-in Exa response previews and vendor-reported USD costs."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping
from itertools import islice
from typing import Any

logger = logging.getLogger(__name__)

_MAX_DOCUMENTS = 20
_MAX_HIGHLIGHTS = 10
_MAX_FIELD_BYTES = 1024
_MAX_ANSWER_BYTES = 8192
_MAX_OUTPUT_BYTES = 16384


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def cost_attributes(response: Any) -> dict[str, float]:
    """Copy only finite, non-negative USD amounts; never estimate token costs."""
    attributes = {}
    try:
        cost = _field(response, "cost_dollars")
        candidates = {"gen_ai.cost.total": _field(cost, "total")}
        for group, names in (
            ("search", ("neural", "keyword")),
            ("contents", ("text", "summary")),
        ):
            breakdown = _field(cost, group)
            for name in names:
                candidates[f"exa.cost.{group}.{name}"] = _field(breakdown, name)
        for key, value in candidates.items():
            if type(value) in (int, float) and value >= 0 and math.isfinite(value):
                attributes[key] = float(value)
    except Exception:  # noqa: BLE001 - vendor metadata cannot break user calls
        # Do not log exception text or stack traces: vendor fields may hold secrets.
        logger.debug("Exa cost inspection failed; unavailable costs omitted")
    return attributes


class ResponseCapture:
    """Retain a small allowlisted preview, never the full vendor response.

    Streams retain at most 8192 text characters until completion, so redaction
    also catches secrets/PII split between chunks. Documents are bounded as
    they arrive. Partial outputs are not exported on error or cancellation.
    """

    def __init__(self, scrub: Callable[[str], str], capture_urls: bool) -> None:
        self._scrub = scrub
        self._capture_urls = capture_urls
        self._data: dict = {}
        self._stream_text = ""
        self.truncated = False

    def _text(self, value: str, limit: int = _MAX_FIELD_BYTES) -> str:
        value = self._scrub(value)
        # Slice characters first to avoid encoding arbitrarily large strings.
        encoded = value[:limit].encode("utf-8", "replace")
        result = encoded[:limit].decode("utf-8", "ignore")
        self.truncated |= len(result) < len(value)
        return result

    def _structured(self, value: Any, remaining: list[int], depth: int = 0) -> Any:
        """Bound structured answers before serializing them into a text preview."""
        if depth > 4 or remaining[0] <= 0:
            self.truncated = True
            return None
        remaining[0] -= 1
        if isinstance(value, str):
            return self._text(value)
        if value is None or type(value) in (bool, int):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, (dict, list)):
            self.truncated |= len(value) > 20
            if isinstance(value, dict):
                return {
                    self._text(key): self._structured(item, remaining, depth + 1)
                    for key, item in islice(value.items(), 20)
                    if isinstance(key, str)
                }
            return [self._structured(item, remaining, depth + 1) for item in value[:20]]
        return None  # Never stringify arbitrary response objects.

    def _documents(self, key: str, documents: Any) -> None:
        if not isinstance(documents, (list, tuple)):
            return
        captured = self._data.setdefault(key, [])
        available = _MAX_DOCUMENTS - len(captured)
        self.truncated |= len(documents) > available
        for document in documents[:available]:
            item: dict[str, str | list[str]] = {}
            fields = ("title", "text", "summary") + (
                ("url",) if self._capture_urls else ()
            )
            for name in fields:
                value = _field(document, name)
                if isinstance(value, str):
                    item[name] = self._text(value)
            highlights = _field(document, "highlights")
            if isinstance(highlights, (list, tuple)):
                self.truncated |= len(highlights) > _MAX_HIGHLIGHTS
                item["highlights"] = [
                    self._text(value)
                    for value in highlights[:_MAX_HIGHLIGHTS]
                    if isinstance(value, str)
                ]
            captured.append(item)
            if len(_json(self._data).encode("utf-8")) > _MAX_OUTPUT_BYTES:
                captured.pop()
                self.truncated = True
                break

    def response(self, response: Any) -> None:
        answer = _field(response, "answer")
        if isinstance(answer, dict):
            answer = _json(self._structured(answer, [128]))
        if isinstance(answer, str):
            self._data["answer"] = self._text(answer, _MAX_ANSWER_BYTES)
        for key in ("results", "citations"):
            self._documents(key, _field(response, key))

    def chunk(self, chunk: Any) -> None:
        text = _field(chunk, "content")
        if isinstance(text, str):
            available = _MAX_ANSWER_BYTES - len(self._stream_text)
            self.truncated |= len(text) > available
            self._stream_text += text[:available]
        self._documents("citations", _field(chunk, "citations"))

    def attributes(self, *, stream: bool = False) -> dict[str, str | bool]:
        if stream:
            self._data["answer"] = self._text(self._stream_text, _MAX_ANSWER_BYTES)
        if not self._data:
            return {}
        # JSON escaping and stream text added after citations can exceed the
        # overall budget. Drop trailing documents, then shorten the answer.
        output = _json(self._data)
        while len(output.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            self.truncated = True
            documents = self._data.get("results") or self._data.get("citations")
            if documents:
                documents.pop()
            else:
                answer = self._data["answer"]
                self._data["answer"] = answer[: len(answer) // 2]
            output = _json(self._data)
        attributes: dict[str, str | bool] = {
            "output.value": output,
            "output.mime_type": "application/json",
        }
        if self.truncated:
            attributes["exa.output_truncated"] = True
        return attributes
