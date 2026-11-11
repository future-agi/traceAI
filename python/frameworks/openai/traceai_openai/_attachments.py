"""OpenAI media capture through the shared, provider-owned attachment manager.

Mirrors ``@traceai/openai`` (``captureOpenAIAttachments`` / ``projectOpenAIMedia``):
typed inline parts only, preflight before any read or decode, one shared
registry per manager so a second observation of the same span and position
returns the first occurrence and uploads nothing. Speech output is captured
from bytes the SDK already buffered, or from chunks the application consumed.
Nothing here fetches URLs, resolves provider file IDs, or reads open files.

This module imports ``fi_instrumentation.attachments``; the instrumentor imports
it only when an attachment manager is passed.
"""

from types import MappingProxyType
from typing import Any, Mapping, Optional

from fi_instrumentation.attachments.media_capture import (
    EMPTY,
    FILE_MIMES,
    IMAGE_MIMES,
    MAX_INLINE_BYTES,
    ConsumedAudio,
    Observation,
    Occurrence,
    active,
    attach_audio,
    attach_inline,
    get,
    items,
    local_capture_policy,
    request_files,
)

__all__ = (
    "capture_openai_attachments",
    "project_openai_media",
    "capture_openai_speech",
    "observe_openai_speech",
    "capture_openai_audio_input",
    "openai_attachment_capabilities",
)

#: Declarative handoff for auto/wire owners and reconciliation.
openai_attachment_capabilities = MappingProxyType({
    "schema_version": 1,
    "operations": (
        "chat.completions.create.input", "responses.create.input", "responses.create.output",
        "responses.stream.output", "images.generate.output", "audio.speech.create.output",
        "audio.transcriptions.create.input", "audio.translations.create.input",
    ),
    "sources": ("inline_base64", "sdk_buffered_bytes", "consumed_reader"),
    # Fail closed: nothing is attempted for these. Streamed Responses output is captured only from the
    # terminal response.completed event the application consumed; partial (preview) images never are.
    "unsupported": (
        "remote_url", "provider_file", "partial_image", "images.generate.stream", "images.edit",
        "images.create_variation", "open_file_object", "speech.stream_format.sse",
    ),
    "max_inline_bytes": MAX_INLINE_BYTES,
})

_ROLES = ("user", "assistant", "tool", "system")


def _key(operation: str, direction: str, message_index: int, part_index: int) -> str:
    return "%s/%s/%d/%d" % (operation, direction, message_index, part_index)


def _image_mime(fmt: Any) -> str:
    # The Images API and image_generation_call default to PNG; a typed format wins.
    return "image/jpeg" if fmt == "jpeg" else "image/webp" if fmt == "webp" else "image/png"


def capture_openai_attachments(manager, span, payload: Any, *, operation: str, direction: str,
                               trace_config=None, capture=None,
                               image_format: Optional[str] = None) -> Mapping[str, Occurrence]:
    """Capture typed inline media of one OpenAI request or response.

    ``operation`` is ``"chat"``, ``"responses"`` or ``"images"``; ``direction``
    ``"input"`` or ``"output"``. ``span`` is the span the instrumentor emitted
    for this call. Returns the admitted positions (position key -> occurrence)
    so the caller can project them out of span telemetry; empty when no manager,
    disabled, unsampled or denied. Never raises and never changes ``payload``.
    """
    observation = Observation(manager, span, "openai")
    if not observation.usable:
        return EMPTY
    try:
        def attach(read, mimes, message_index, part_index, purpose, *, raw=False, role="user", tool_call_id=None):
            policy = local_capture_policy(trace_config, direction=direction, base=capture,
                                          image=mimes[0].startswith("image/"))
            observation.visit(_key(operation, direction, message_index, part_index), lambda: attach_inline(
                manager, span, read, mimes, raw=raw, direction=direction, purpose=purpose, capture=policy,
                message_index=message_index, part_index=part_index, role=role, tool_call_id=tool_call_id))

        def parts(content, message_index, role):
            for part_index, part in enumerate(items(content)):
                kind = get(part, "type")
                if kind == "image_url":
                    attach(lambda p=part: get(get(p, "image_url"), "url"), IMAGE_MIMES, message_index, part_index,
                           "model_input", role=role)
                elif kind == "input_image":
                    attach(lambda p=part: get(p, "image_url"), IMAGE_MIMES, message_index, part_index,
                           "model_input", role=role)
                elif kind == "input_file":
                    attach(lambda p=part: get(p, "file_data"), FILE_MIMES, message_index, part_index,
                           "document", role=role)
                elif kind == "file":
                    attach(lambda p=part: get(get(p, "file"), "file_data"), FILE_MIMES, message_index, part_index,
                           "document", role=role)

        if direction == "output" and get(payload, "error"):
            return EMPTY
        if direction == "input":
            messages = get(payload, "messages" if operation == "chat" else "input") \
                if operation in ("chat", "responses") else None
            for message_index, message in enumerate(items(messages)):
                kind = get(message, "type")
                if kind == "computer_call_output":
                    output = get(message, "output")
                    # A screenshot is model-bound input; its call id keeps the causal link to the computer_call.
                    if get(output, "type") == "computer_screenshot":
                        attach(lambda o=output: get(o, "image_url"), IMAGE_MIMES, message_index, 0,
                               "computer_screenshot", role="tool", tool_call_id=get(message, "call_id"))
                elif kind in (None, "message"):
                    role = get(message, "role")
                    parts(get(message, "content"), message_index, role if role in _ROLES else "unknown")
        elif operation == "images":
            mime = _image_mime(get(payload, "output_format") or image_format)
            for index, item in enumerate(items(get(payload, "data"))):
                attach(lambda i=item: get(i, "b64_json"), (mime,), index, 0, "model_output", raw=True,
                       role="assistant")
        elif operation == "responses":
            for index, item in enumerate(items(get(payload, "output"))):
                if get(item, "type") == "image_generation_call" and get(item, "status") == "completed":
                    attach(lambda i=item: get(i, "result"), (_image_mime(get(item, "output_format")),), index, 0,
                           "model_output", raw=True, role="assistant")
    except Exception:
        pass  # Instrumentation must not throw or log source-bearing errors.
    return MappingProxyType(dict(observation.captured)) if observation.captured else EMPTY


def _uri(occurrence: Occurrence) -> str:
    return "fi-attachment:" + occurrence.occurrence_id


def project_openai_media(payload: Any, captured: Mapping[str, Occurrence], operation: str, direction: str) -> Any:
    """Telemetry-only copy: ONLY admitted positions become ``fi-attachment:<occurrence_id>``.

    With nothing admitted the SAME object is returned. The SDK request and
    response are never mutated; only plain mappings are copied.
    """
    if not captured or not isinstance(payload, Mapping):
        return payload
    try:
        field = ("messages" if operation == "chat" else "input") if direction == "input" else "output"
        values = payload.get(field)
        if not isinstance(values, list):
            return payload

        def at(message_index, part_index):
            return captured.get(_key(operation, direction, message_index, part_index))

        def message(value, message_index):
            if not isinstance(value, Mapping):
                return value
            kind = value.get("type")
            if kind == "computer_call_output":
                occurrence = at(message_index, 0)
                output = value.get("output")
                if occurrence and isinstance(output, Mapping):
                    return {**value, "output": {**output, "image_url": _uri(occurrence)}}
                return value
            if kind == "image_generation_call":
                occurrence = at(message_index, 0)
                return {**value, "result": _uri(occurrence)} if occurrence else value
            content = value.get("content")
            if not isinstance(content, list):
                return value
            return {**value, "content": [part(item, message_index, index) for index, item in enumerate(content)]}

        def part(value, message_index, part_index):
            occurrence = at(message_index, part_index)
            if not occurrence or not isinstance(value, Mapping):
                return value
            kind = value.get("type")
            if kind == "image_url" and isinstance(value.get("image_url"), Mapping):
                return {**value, "image_url": {**value["image_url"], "url": _uri(occurrence)}}
            if kind == "input_image":
                return {**value, "image_url": _uri(occurrence)}
            if kind == "input_file":
                return {**value, "file_data": _uri(occurrence)}
            if kind == "file" and isinstance(value.get("file"), Mapping):
                return {**value, "file": {**value["file"], "file_data": _uri(occurrence)}}
            return value

        return {**payload, field: [message(value, index) for index, value in enumerate(values)]}
    except Exception:
        return payload


def _speech_policy(trace_config, capture):
    return local_capture_policy(trace_config, direction="output", base=capture)


def capture_openai_speech(manager, span, data: Any, *, request: Any = None, trace_config=None,
                          capture=None) -> Optional[Occurrence]:
    """Speech audio the SDK already buffered in memory (never a stream or file)."""
    observation = Observation(manager, span, "openai")
    if not observation.usable:
        return None
    observation.visit(_key("speech", "output", 0, 0), lambda: attach_audio(
        manager, span, data, direction="output", purpose="model_output", role="assistant",
        capture=_speech_policy(trace_config, capture), declared_format=get(request, "response_format")))
    return observation.captured.get(_key("speech", "output", 0, 0))


def observe_openai_speech(manager, span, *, request: Any = None, trace_config=None,
                          capture=None) -> Optional[ConsumedAudio]:
    """Observer for speech chunks the application consumes; None when nothing can be captured."""
    if not active(manager) or Observation(manager, span, "openai").key is None:
        return None
    return ConsumedAudio(manager, span, direction="output", purpose="model_output", role="assistant",
                         capture=_speech_policy(trace_config, capture),
                         declared_format=get(request, "response_format"))


def capture_openai_audio_input(manager, span, files: Any, *, operation: str = "transcriptions",
                               trace_config=None, capture=None) -> Mapping[str, Occurrence]:
    """Transcription/translation input from multipart entries the SDK already holds as bytes.

    The SDK reads a path into bytes before sending, so those bytes are captured.
    Open file objects (including BytesIO) are the application's stream and are
    never read: they report nothing.
    """
    observation = Observation(manager, span, "openai")
    if not observation.usable:
        return EMPTY
    policy = local_capture_policy(trace_config, direction="input", base=capture)
    for index, content in request_files(files):
        if isinstance(content, (bytes, bytearray)):
            observation.visit(_key(operation, "input", index, 0), lambda c=content, i=index: attach_audio(
                manager, span, c, direction="input", purpose="model_input", role="user", capture=policy,
                message_index=i, part_index=0))
    return MappingProxyType(dict(observation.captured)) if observation.captured else EMPTY


# ----------------------------------------------------------- instrumentor glue
_ROUTES = (("/chat/completions", "chat"), ("/responses", "responses"), ("/images/generations", "images"),
           ("/audio/speech", "speech"), ("/audio/transcriptions", "transcriptions"),
           ("/audio/translations", "translations"))


def _route(options: Any) -> Optional[str]:
    url = getattr(options, "url", None)
    if not isinstance(url, str):
        return None
    path = url.split("?", 1)[0].rstrip("/")
    for suffix, route in _ROUTES:
        if path.endswith(suffix):
            return route
    return None


class RequestMedia:
    """Per-call capture state the instrumentor threads through one request."""

    __slots__ = ("manager", "route", "options", "config", "request", "span", "output_captured")

    def __init__(self, manager, route: str, options: Any, config) -> None:
        self.manager, self.route, self.options, self.config = manager, route, options, config
        self.request: Any = None
        self.span = None
        self.output_captured: Mapping[str, Occurrence] = EMPTY

    def capture_input(self, span, request_parameters: Mapping) -> Mapping:
        """Capture request media on the emitted span; return the telemetry-only projected parameters."""
        self.span, self.request = span, request_parameters
        if self.route in ("chat", "responses"):
            captured = capture_openai_attachments(self.manager, span, request_parameters, operation=self.route,
                                                  direction="input", trace_config=self.config)
            return project_openai_media(request_parameters, captured, self.route, "input")
        if self.route in ("transcriptions", "translations"):
            capture_openai_audio_input(self.manager, span, getattr(self.options, "files", None),
                                       operation=self.route, trace_config=self.config)
        return request_parameters

    def capture_output(self, response: Any) -> Mapping[str, Occurrence]:
        if self.span is None:
            return EMPTY
        if self.route == "responses":
            self.output_captured = capture_openai_attachments(self.manager, self.span, response,
                                                              operation="responses", direction="output",
                                                              trace_config=self.config)
        elif self.route == "images":
            capture_openai_attachments(self.manager, self.span, response, operation="images", direction="output",
                                       trace_config=self.config, image_format=get(self.request, "output_format"))
        elif self.route == "speech":
            data = None
            try:
                # Only a body the SDK already read; an unread streamed body raises instead of reading.
                data = response.content
            except Exception:
                data = None
            capture_openai_speech(self.manager, self.span, data, request=self.request, trace_config=self.config)
        return self.output_captured

    def project_output(self, output: Any) -> Any:
        return project_openai_media(output, self.output_captured, "responses", "output")

    def speech_bytes(self, data: Any) -> None:
        if self.route == "speech" and self.span is not None:
            capture_openai_speech(self.manager, self.span, data, request=self.request, trace_config=self.config)

    def speech_observer(self) -> Optional[ConsumedAudio]:
        if self.route != "speech" or self.span is None:
            return None
        return observe_openai_speech(self.manager, self.span, request=self.request, trace_config=self.config)


def request_media(manager, options: Any, config) -> Optional[RequestMedia]:
    """None unless an active manager is bound and this request is a media route."""
    if not active(manager):
        return None
    route = _route(options)
    return RequestMedia(manager, route, options, config) if route else None
