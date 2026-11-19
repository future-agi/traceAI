"""Assemble a SageMaker response event stream while handing every event to the caller untouched."""
from __future__ import annotations

import codecs
import json
import logging
from typing import Any, List, Optional

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from . import _payload as payload

logger = logging.getLogger(__name__)

# One unterminated line may not grow without bound.
_LINE_LIMIT = payload.PARSE_LIMIT


class StreamAccumulator:
    """Reassemble PayloadPart bytes into lines and fold the recognised chunk shapes."""

    def __init__(self, content_type: Any, capture_output: bool) -> None:
        self.kind = payload.classify(content_type)
        base = payload.base_type(content_type)
        # Plain text carries no line structure worth parsing; everything else textual is line-delimited.
        self.raw_text = self.kind == payload.TEXT and base != "text/event-stream"
        self.capture_output = capture_output
        self.content_type = content_type
        self.size = 0
        self.usage = payload.Usage()
        self._buffer = bytearray()
        self._overflow = False
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pieces: List[str] = []
        self._length = 0
        self._final: Optional[str] = None
        self._parsed_any = False

    def feed(self, data: bytes) -> None:
        self.size += len(data)
        if self.kind == payload.BINARY:
            return
        if self.raw_text:
            self._add(self._decoder.decode(data))
            return
        self._buffer.extend(data)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if self._overflow:
                self._overflow = False
                continue
            self._line(line)
        if len(self._buffer) > _LINE_LIMIT:
            self._buffer.clear()
            self._overflow = True

    def _add(self, text: str) -> None:
        if not self.capture_output or not text or self._length >= payload.TEXT_LIMIT:
            return
        text = text[: payload.TEXT_LIMIT - self._length]
        self._pieces.append(text)
        self._length += len(text)

    def _line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith((":", "event:", "id:", "retry:")):
            return
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            return
        try:
            parsed = json.loads(line)
        except ValueError:
            return
        chunks = parsed if isinstance(parsed, list) else [parsed]
        for chunk in chunks:
            if isinstance(chunk, dict):
                self._chunk(chunk)

    def _chunk(self, chunk: dict) -> None:
        self._parsed_any = True
        self.usage.merge(payload.usage_of(chunk))
        token = chunk.get("token")
        generated = chunk.get("generated_text")
        if isinstance(token, dict):
            # Hugging Face TGI: one token per chunk, the whole text on the last one.
            if isinstance(token.get("text"), str):
                self._add(token["text"])
            if isinstance(generated, str) and generated:
                self._final = payload.clip(generated)
            return
        text = payload.text_of(chunk)
        if text:
            self._add(text)

    def finish(self) -> None:
        """Flush a trailing unterminated line and fold derived totals."""
        if self.kind == payload.BINARY:
            return
        if self.raw_text:
            self._add(self._decoder.decode(b"", final=True))
        elif self._buffer and not self._overflow:
            line = bytes(self._buffer)
            self._buffer.clear()
            self._line(line)
        self.usage.complete()

    def output(self) -> Optional[str]:
        if not self.capture_output:
            return None
        if self.kind == payload.BINARY:
            return payload.binary_note(self.content_type, self.size)
        if self._final is not None:
            return self._final
        return "".join(self._pieces) or None


class StreamBody:
    """Proxy for the botocore EventStream in ``response["Body"]``.

    Events are forwarded unchanged. The span ends when the stream is exhausted, fails, or is
    closed, whichever comes first.
    """

    def __init__(self, source: Any, span: Any, accumulator: StreamAccumulator, on_end: Any) -> None:
        self._source = source
        self._span = span
        self._acc = accumulator
        self._on_end = on_end
        self._iterator: Optional[Any] = None
        self._ended = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._source, name)

    def __iter__(self) -> "StreamBody":
        return self

    def __next__(self) -> Any:
        if self._iterator is None:
            self._iterator = iter(self._source)
        try:
            with trace.use_span(self._span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                event = next(self._iterator)
        except StopIteration:
            self._finish(complete=True)
            raise
        except BaseException as exc:
            self._fail(exc)
            raise
        self._observe(event)
        return event

    def _observe(self, event: Any) -> None:
        try:
            data = event.get("PayloadPart", {}).get("Bytes") if isinstance(event, dict) else None
            if isinstance(data, (bytes, bytearray)):
                self._acc.feed(bytes(data))
        except Exception:  # never let capture break the caller's stream
            logger.debug("sagemaker stream capture failed", exc_info=True)

    def _fail(self, exc: BaseException) -> None:
        if self._ended:
            return
        self._ended = True
        # Exception text can carry request or response content: record the class only.
        self._span.set_attribute("error.type", type(exc).__name__)
        self._span.set_status(Status(StatusCode.ERROR))
        self._span.end()

    def _finish(self, complete: bool) -> None:
        if self._ended:
            return
        self._ended = True
        try:
            if not complete:
                self._span.set_attribute("aws.sagemaker.stream.incomplete", True)
            self._acc.finish()
            self._on_end(self._span, self._acc)
        except Exception:
            logger.debug("sagemaker stream finalisation failed", exc_info=True)
        finally:
            self._span.end()

    def close(self) -> Any:
        try:
            return self._source.close()
        finally:
            self._finish(complete=False)

    def __enter__(self) -> "StreamBody":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._finish(complete=False)
        except Exception:
            pass
