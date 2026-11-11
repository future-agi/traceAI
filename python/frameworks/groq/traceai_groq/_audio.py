"""Groq speech and transcription/translation spans with attachment capture (opt-in).

Installed only when an enabled attachment manager is passed to ``instrument()``,
so plain tracing is unchanged. Capture goes through the same shared helper as
``traceai_openai`` (``fi_instrumentation.attachments.media_capture``):

- speech output: only bytes the SDK already buffered (a non-streamed response);
  a streamed body the application has not read is never read here;
- transcription/translation input: ``bytes`` or ``(name, bytes, ...)`` file
  values only. Paths and open file objects are the application's files and
  streams and are never opened or read.
"""

import logging
from typing import Any, Callable, Mapping, Tuple

from fi_instrumentation.attachments.media_capture import (
    Observation,
    attach_audio,
    get,
    local_capture_policy,
    request_files,
)
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

__all__ = ("capture_groq_speech", "capture_groq_audio_input", "groq_attachment_capabilities")

groq_attachment_capabilities = {
    "schema_version": 1,
    "operations": ("audio.speech.create.output", "audio.transcriptions.create.input",
                   "audio.translations.create.input"),
    "sources": ("sdk_buffered_bytes", "inline_bytes"),
    "unsupported": ("streamed_speech", "file_path", "open_file_object", "chat_media"),
}


def _buffered(response: Any) -> Any:
    """Body bytes the SDK already read; an unread streamed body raises instead of reading."""
    http_response = getattr(response, "http_response", None)
    if http_response is None:
        http_response = getattr(response, "response", None)
    try:
        return http_response.content
    except Exception:
        return None


def capture_groq_speech(manager, span, response: Any, *, request: Any = None, trace_config=None, capture=None):
    """Capture a non-streamed Groq speech response on its span. Never raises."""
    observation = Observation(manager, span, "groq")
    if observation.usable:
        observation.visit("speech/output/0/0", lambda: attach_audio(
            manager, span, _buffered(response), direction="output", purpose="model_output", role="assistant",
            capture=local_capture_policy(trace_config, direction="output", base=capture),
            declared_format=get(request, "response_format")))
    return observation.captured.get("speech/output/0/0")


def capture_groq_audio_input(manager, span, file: Any, *, operation: str = "transcriptions", trace_config=None,
                             capture=None):
    """Capture an in-memory transcription/translation input file. Never raises."""
    observation = Observation(manager, span, "groq")
    if observation.usable:
        policy = local_capture_policy(trace_config, direction="input", base=capture)
        for index, content in request_files([("file", file)]):
            if isinstance(content, (bytes, bytearray)):
                observation.visit("%s/input/%d/0" % (operation, index), lambda c=content, i=index: attach_audio(
                    manager, span, c, direction="input", purpose="model_input", role="user", capture=policy,
                    message_index=i, part_index=0))
    return dict(observation.captured)


class _AudioWrapper:
    def __init__(self, tracer: trace_api.Tracer, manager: Any, config: Any, operation: str) -> None:
        self._tracer, self._manager, self._config, self._operation = tracer, manager, config, operation

    def _start(self, kwargs: Mapping[str, Any]) -> Any:
        attributes = {
            SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
            SpanAttributes.GEN_AI_PROVIDER_NAME: "groq",
        }
        if isinstance(model := kwargs.get("model"), str) and model:
            attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
        return self._tracer.start_span("Groq Audio " + self._operation.capitalize(), attributes=attributes)

    def _before(self, span: Any, kwargs: Mapping[str, Any]) -> None:
        if self._operation in ("transcriptions", "translations"):
            capture_groq_audio_input(self._manager, span, kwargs.get("file"), operation=self._operation,
                                     trace_config=self._config)

    def _after(self, span: Any, kwargs: Mapping[str, Any], response: Any) -> None:
        if self._operation == "speech":
            capture_groq_speech(self._manager, span, response, request=kwargs, trace_config=self._config)

    @staticmethod
    def _fail(span: Any, error: BaseException) -> None:
        try:
            span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, type(error).__name__))
            span.end()
        except Exception:
            logger.debug("Failed to end span", exc_info=True)

    @staticmethod
    def _ok(span: Any) -> None:
        try:
            span.set_status(trace_api.Status(trace_api.StatusCode.OK))
            span.end()
        except Exception:
            logger.debug("Failed to end span", exc_info=True)


class _SyncAudioWrapper(_AudioWrapper):
    def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...],
                 kwargs: Mapping[str, Any]) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return wrapped(*args, **kwargs)
        span = self._start(kwargs)
        with trace_api.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            self._before(span, kwargs)
            try:
                response = wrapped(*args, **kwargs)
            except BaseException as error:
                self._fail(span, error)
                raise
        self._after(span, kwargs, response)
        self._ok(span)
        return response


class _AsyncAudioWrapper(_AudioWrapper):
    async def __call__(self, wrapped: Callable[..., Any], instance: Any, args: Tuple[Any, ...],
                       kwargs: Mapping[str, Any]) -> Any:
        if context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY):
            return await wrapped(*args, **kwargs)
        span = self._start(kwargs)
        with trace_api.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
            self._before(span, kwargs)
            try:
                response = await wrapped(*args, **kwargs)
            except BaseException as error:
                self._fail(span, error)
                raise
        self._after(span, kwargs, response)
        self._ok(span)
        return response


# (module, class, operation) for each audio method; missing modules (older SDKs) are skipped.
AUDIO_METHODS = (
    ("groq.resources.audio.speech", "Speech", "speech", False),
    ("groq.resources.audio.speech", "AsyncSpeech", "speech", True),
    ("groq.resources.audio.transcriptions", "Transcriptions", "transcriptions", False),
    ("groq.resources.audio.transcriptions", "AsyncTranscriptions", "transcriptions", True),
    ("groq.resources.audio.translations", "Translations", "translations", False),
    ("groq.resources.audio.translations", "AsyncTranslations", "translations", True),
)


def wrapper_for(tracer: Any, manager: Any, config: Any, operation: str, is_async: bool) -> Any:
    return (_AsyncAudioWrapper if is_async else _SyncAudioWrapper)(tracer, manager, config, operation)
