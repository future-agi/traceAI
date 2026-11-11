"""Pydantic AI typed media through the shared, provider-owned attachment manager.

Observed at SDK execution boundaries only:

- model request span, input: ``BinaryContent`` in ``UserPromptPart.content``,
  in ``ToolReturnPart.content`` (with ``tool_call_id``) and in re-sent
  ``FilePart`` history; ``ImageUrl`` / ``AudioUrl`` / ``DocumentUrl`` go to the
  core's ``RemoteUrl`` policy and are never fetched or resolved here;
- model request span, output: ``FilePart`` content of a successful,
  non-streaming response (never an error body);
- tool call span: ``BinaryContent`` a tool returned.

``BinaryContent.data`` must be complete bytes whose magic bytes match its
declared ``media_type``. Videos, uploaded provider files and unknown content
objects are not captured, and nothing is serialized to look for media. Bytes
identical to ones a provider instrumentor (or the tool span) already uploaded in
the same trace are not uploaded again.
"""

import dataclasses
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Optional

from fi_instrumentation.attachments.media_capture import (
    AUDIO_MIMES,
    DOCUMENT_MIMES,
    EMPTY,
    IMAGE_MIMES,
    MAX_FRAMEWORK_BYTES,
    MAX_FRAMEWORK_PARTS,
    MediaPart,
    Occurrence,
    capture_parts,
)

__all__ = ("PydanticAIMedia", "pydantic_ai_attachment_capabilities")

pydantic_ai_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": ("model.request.input", "model.request.output", "tool.call.result",
                   "model.request_stream.input"),
    "sources": ("binary_content",),
    "unsupported": ("remote_url_fetch", "uploaded_file", "video", "streamed_output", "error_body"),
    "max_part_bytes": MAX_FRAMEWORK_BYTES,
    "max_parts_per_span": MAX_FRAMEWORK_PARTS,
})

_URL_FAMILIES = {"image-url": IMAGE_MIMES, "audio-url": AUDIO_MIMES, "document-url": DOCUMENT_MIMES}


def _field(value: Any, name: str) -> Any:
    """A declared field only: never a property, resolver or ``__getattr__``."""
    try:
        return vars(value).get(name)
    except TypeError:
        pass
    try:
        if dataclasses.is_dataclass(value) and not isinstance(value, type) and \
                any(f.name == name for f in dataclasses.fields(value)):
            return getattr(value, name)
    except Exception:
        pass
    return None


def _family(mime: Any) -> Optional[tuple]:
    for family in (IMAGE_MIMES, AUDIO_MIMES, DOCUMENT_MIMES):
        if mime in family:
            return (mime,) + tuple(m for m in family if m != mime)
    return None


def _content_parts(content: Any, message_index: int, start: int, role: str, purpose: str,
                   tool_call_id: Optional[str]) -> Iterator[MediaPart]:
    values = content if isinstance(content, (list, tuple)) else [content]
    index = start
    for value in values[:MAX_FRAMEWORK_PARTS]:
        kind = _field(value, "kind")
        if kind == "binary":
            data = _field(value, "data")
            mimes = _family(_field(value, "media_type"))
            if mimes and isinstance(data, (bytes, bytearray)):
                part_purpose = "document" if purpose == "model_input" and mimes[0] in DOCUMENT_MIMES else purpose
                yield MediaPart("bytes", lambda d=data: d, mimes, part_purpose, message_index, index, role=role,
                                tool_call_id=tool_call_id)
            index += 1
        elif kind in _URL_FAMILIES:
            url = _field(value, "url")
            if isinstance(url, str):
                yield MediaPart("url", lambda u=url: u, _URL_FAMILIES[kind], purpose, message_index, index,
                                role=role, tool_call_id=tool_call_id)
            index += 1
        elif kind is not None:
            index += 1  # video-url, uploaded files, ...: never captured


def _request_parts(messages: Any) -> Iterator[MediaPart]:
    for message_index, message in enumerate(list(messages)[:MAX_FRAMEWORK_PARTS]):
        index = 0
        for part in list(_field(message, "parts") or ())[:MAX_FRAMEWORK_PARTS]:
            part_kind = _field(part, "part_kind")
            if part_kind == "user-prompt":
                content = _field(part, "content")
                if isinstance(content, str):
                    continue
                parts = list(_content_parts(content, message_index, index, "user", "model_input", None))
            elif part_kind == "tool-return":
                tool_call_id = _field(part, "tool_call_id")
                parts = list(_content_parts(_field(part, "content"), message_index, index, "tool", "tool_result",
                                            tool_call_id if isinstance(tool_call_id, str) else None))
            elif part_kind == "file":
                # A generated file re-sent as history is model input now.
                parts = list(_content_parts(_field(part, "content"), message_index, index, "assistant",
                                            "model_input", None))
            else:
                continue
            yield from parts
            index += max(len(parts), 1)


def _response_parts(response: Any) -> Iterator[MediaPart]:
    index = 0
    for part in list(_field(response, "parts") or ())[:MAX_FRAMEWORK_PARTS]:
        if _field(part, "part_kind") == "file":
            yield from _content_parts(_field(part, "content"), 0, index, "assistant", "model_output", None)
        index += 1


class PydanticAIMedia:
    """The instrumentor's binding: manager plus the caller's TraceConfig (deny-only)."""

    __slots__ = ("manager", "trace_config")

    def __init__(self, manager: Any, trace_config: Any) -> None:
        self.manager, self.trace_config = manager, trace_config

    def _capture(self, span, parts, direction: str) -> Mapping[str, Occurrence]:
        if span is None:
            return EMPTY
        try:
            return capture_parts(self.manager, span, "pydantic_ai", parts, direction=direction,
                                 trace_configs=(self.trace_config,))
        except Exception:
            return EMPTY

    def request(self, span, messages: Any) -> Mapping[str, Occurrence]:
        if not isinstance(messages, (list, tuple)):
            return EMPTY
        return self._capture(span, _request_parts(messages), "input")

    def response(self, span, response: Any) -> Mapping[str, Occurrence]:
        # Historical Model.request returns (ModelResponse, Usage).
        response = response[0] if isinstance(response, tuple) and response else response
        return self._capture(span, _response_parts(response), "output")

    def tool_result(self, span, result: Any, tool_call_id: Any) -> Mapping[str, Occurrence]:
        tool_call_id = tool_call_id if isinstance(tool_call_id, str) else None
        return self._capture(span, _content_parts(result, 0, 0, "tool", "tool_result", tool_call_id), "input")
