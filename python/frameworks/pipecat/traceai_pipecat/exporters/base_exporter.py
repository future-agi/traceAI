"""
Base exporter for Pipecat integration with Future AGI.

This module provides the base class for mapped span exporters that convert
Pipecat attributes to OpenTelemetry GenAI conventions (Future AGI standard).
"""

import json
import copy
import logging
import os
from typing import Any, Dict

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import MessageAttributes, SpanAttributes
from opentelemetry.sdk.trace import Event
from opentelemetry.trace import Status

logger = logging.getLogger(__name__)


def _ensure_json_string(value: Any) -> str:
    """Ensure a value is a valid JSON string."""
    try:
        if isinstance(value, str):
            # Validate if string is already JSON
            json.loads(value)
            return value
        return json.dumps(value)
    except Exception:
        return json.dumps(str(value))


def _detect_mime_type(value: Any) -> str:
    """Detect the MIME type of a value."""
    if isinstance(value, (dict, list)):
        return "application/json"
    if isinstance(value, str):
        stripped = value.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            return "application/json"
    return "text/plain"


def _map_attributes_to_fi_conventions(attributes: Dict[str, Any]) -> Dict[str, Any]:
    """Map Pipecat attributes to Future AGI conventions."""
    if not attributes:
        return {}

    mapped: Dict[str, Any] = dict(attributes)  # start by preserving originals

    # Pipecat → OTEL GenAI basics
    if "gen_ai.system" in attributes:
        mapped[SpanAttributes.GEN_AI_PROVIDER_NAME] = attributes.get("gen_ai.system")

    if "gen_ai.request.model" in attributes:
        mapped[SpanAttributes.GEN_AI_REQUEST_MODEL] = attributes.get("gen_ai.request.model")

    # Inputs / Outputs
    if "input" in attributes and "input.value" not in mapped:
        input_val = attributes.get("input")
        mime_type = _detect_mime_type(input_val)
        mapped["input.value"] = (
            _ensure_json_string(input_val)
            if mime_type == "application/json"
            else input_val
        )
        mapped["input.mime_type"] = mime_type

        # Expand chat-style input messages into enumerated attributes if we detect a list of message dicts
        messages_obj = None
        try:
            if isinstance(input_val, str) and mime_type == "application/json":
                parsed = json.loads(input_val)
                if isinstance(parsed, list):
                    messages_obj = parsed
            elif isinstance(input_val, list):
                messages_obj = input_val
        except Exception:
            messages_obj = None

        if isinstance(messages_obj, list):
            for index, message in enumerate(messages_obj):
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")
                if role is not None:
                    mapped[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_ROLE}"] = role
                if content is not None:
                    # Content may be str or structured; serialize non-str to JSON
                    mapped[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{MessageAttributes.MESSAGE_CONTENT}"] = (
                        content
                        if isinstance(content, str)
                        else _ensure_json_string(content)
                    )

    # Realtime transcription explicitly identifies its direction. Do not make
    # user speech an assistant output (or mask it using the opposite policy).
    transcript = attributes.get("transcript")
    input_transcript = attributes.get("transcript.is_input") is True
    if transcript is not None and input_transcript:
        mapped.setdefault("input.value", transcript)
        mapped.setdefault("input.mime_type", _detect_mime_type(transcript))
        mapped.setdefault(f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "user")
        mapped.setdefault(f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", transcript)

    # Native Gemini text output is independent of its transcript alias.
    out_val = attributes.get("output")
    if out_val is None:
        out_val = attributes.get("text_output")
    if out_val is None and not input_transcript:
        out_val = transcript
    if out_val is not None and "output.value" not in mapped:
        mapped["output.value"] = out_val
        mapped["output.mime_type"] = _detect_mime_type(out_val)

        output_content_key = f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}"
        if output_content_key not in mapped:
            mapped[f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}"] = "assistant"
            mapped[output_content_key] = (
                out_val if isinstance(out_val, str) else _ensure_json_string(out_val)
            )

    # TTS text
    if "text" in attributes and "input.value" not in mapped:
        mapped["input.value"] = attributes.get("text")
        mapped["input.mime_type"] = "text/plain"

    # Invocation parameters (temperature, max_tokens, etc.)
    invocation_params: Dict[str, Any] = {}
    for key, val in attributes.items():
        if key.startswith("gen_ai.request.") and key not in ("gen_ai.request.model",):
            invocation_params[key.split("gen_ai.request.", 1)[1]] = val
    if invocation_params:
        mapped[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = _ensure_json_string(invocation_params)

    # Token usage
    if "gen_ai.usage.input_tokens" in attributes:
        mapped[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = attributes.get("gen_ai.usage.input_tokens")
    if "gen_ai.usage.output_tokens" in attributes:
        mapped[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = attributes.get(
            "gen_ai.usage.output_tokens"
        )
    # Total tokens
    try:
        prompt_tokens = mapped.get(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS)
        completion_tokens = mapped.get(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS)
        if isinstance(prompt_tokens, (int, float)) and isinstance(
            completion_tokens, (int, float)
        ):
            mapped[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = int(prompt_tokens) + int(
                completion_tokens
            )
    except Exception:
        pass

    # Tools
    if "tools" in attributes:
        mapped[SpanAttributes.GEN_AI_TOOL_DEFINITIONS] = (
            attributes.get("tools")
            if isinstance(attributes.get("tools"), str)
            else _ensure_json_string(attributes.get("tools"))
        )
    if "tools.definitions" in attributes:
        mapped["tool.json_schema"] = (
            attributes.get("tools.definitions")
            if isinstance(attributes.get("tools.definitions"), str)
            else _ensure_json_string(attributes.get("tools.definitions"))
        )

    # Tool call details
    if "tool.function_name" in attributes:
        mapped["tool_call.function.name"] = attributes.get("tool.function_name")
    if "tool.call_id" in attributes:
        mapped["tool_call.id"] = attributes.get("tool.call_id")
    if "tool.arguments" in attributes:
        mapped["tool_call.function.arguments"] = (
            attributes.get("tool.arguments")
            if isinstance(attributes.get("tool.arguments"), str)
            else _ensure_json_string(attributes.get("tool.arguments"))
        )

    # Raw tool result for debugging
    if "tool.result" in attributes and "raw.output" not in mapped:
        mapped["raw.output"] = (
            attributes.get("tool.result")
            if isinstance(attributes.get("tool.result"), str)
            else _ensure_json_string(attributes.get("tool.result"))
        )

    # Session / conversation mapping
    if "conversation.id" in attributes and SpanAttributes.GEN_AI_CONVERSATION_ID not in mapped:
        mapped[SpanAttributes.GEN_AI_CONVERSATION_ID] = attributes.get("conversation.id")

    # Consolidate assorted fields under metadata as a JSON string
    metadata: Dict[str, Any] = {}
    for k in (
        "gen_ai.operation.name",
        "metrics.ttfb",
        "tools.count",
        "tools.names",
        "system",
        "output_modality",
        "function_calls.count",
        "function_calls.names",
        "is_final",
        "language",
        "vad_enabled",
        "voice_id",
        "turn.number",
        "turn.type",
        "turn.duration_seconds",
        "turn.was_interrupted",
        "conversation.type",
        "system_instruction",
        "tool.result_status",
    ):
        if k in attributes:
            metadata[k] = attributes[k]

    # Include raw nested settings.* keys
    settings_obj: Dict[str, Any] = {}
    for key, val in attributes.items():
        if key.startswith("settings."):
            settings_obj[key.split("settings.", 1)[1]] = val
    if settings_obj:
        metadata["settings"] = settings_obj

    if metadata:
        # If an existing 'metadata' attribute exists and is a JSON string or dict, merge
        existing = attributes.get("metadata")
        try:
            if isinstance(existing, str):
                existing = json.loads(existing)
            if isinstance(existing, dict):
                metadata = {**existing, **metadata}
        except Exception:
            pass
        mapped["metadata"] = _ensure_json_string(metadata)

    # Determine gen_ai.span.kind from context
    # Defaults: LLM for LLM/STT/TTS-like spans; TOOL for tool spans; AGENT for setup; CHAIN for turn/conversation
    if "gen_ai.span.kind" not in mapped:
        span_kind = None
        # Tool call/result spans
        if any(
            k in attributes
            for k in (
                "tool.function_name",
                "tool.call_id",
                "tool.arguments",
                "tool.result",
            )
        ):
            span_kind = "TOOL"
        # Turn or conversation spans
        elif any(
            k in attributes for k in ("turn.number", "turn.type", "conversation.type")
        ):
            span_kind = "CHAIN"
        # Setup spans (Gemini Live/OpenAI Realtime session setup)
        elif any(
            k in attributes for k in ("tools.definitions", "system_instruction")
        ) or any(str(k).startswith("session.") for k in attributes.keys()):
            span_kind = "AGENT"
        else:
            # Treat gen ai operations, STT, TTS as LLM by convention
            span_kind = "LLM"
        mapped["gen_ai.span.kind"] = span_kind

    return mapped


# The legacy supported cores do not expose the error policy or its mask.
# Keep this small compatibility boundary package-owned, without importing new
# private core helpers or increasing the integration's runtime requirement.
_CONTENT_HIDE_FLAGS = (
    "hide_inputs", "hide_outputs", "hide_input_text", "hide_output_text",
    "hide_input_messages", "hide_output_messages",
)
_INPUT_ALIASES = frozenset({
    "input", "text", "system", "system_instruction", "context_system_instruction",
    "instructions", "tool.arguments", "input.value", "input.mime_type", "raw.input",
    "tool_call.function.arguments",
})
_OUTPUT_ALIASES = frozenset({
    "output", "text_output", "tool.result", "output.value", "output.mime_type", "raw.output",
})


def _is_error_detail(key):
    return key in ("exception.message", "exception.stacktrace", "error.message") or key.endswith((
        ".exception.message", ".exception.stacktrace", ".error.message", ".error_message",
    ))


def _hidden_content_key(key, hide_input, hide_output, input_transcript):
    # Both native setup hooks flatten their configuration under session/settings.
    native_key = key
    if key.startswith(("session.", "settings.")):
        native_key = key.split(".", 1)[1]
    if hide_input and (native_key in _INPUT_ALIASES or key.startswith("gen_ai.input.messages.")):
        return True
    if hide_output and (native_key in _OUTPUT_ALIASES or key.startswith("gen_ai.output.messages.")):
        return True
    return key == "transcript" and (hide_input if input_transcript else hide_output)


def _sanitize_metadata(value, hide_input, hide_output, input_transcript):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return None
    # OTel accepts strings, arrays and scalars too. Opaque content cannot be
    # classified safely: discard that field, never the completed operation.
    if not isinstance(value, dict):
        return None

    def sanitize(obj):
        if isinstance(obj, dict):
            return {key: sanitize(item) for key, item in obj.items()
                    if not _hidden_content_key(key, hide_input, hide_output, input_transcript)}
        if isinstance(obj, list):
            return [sanitize(item) for item in obj]
        return obj

    try:
        return _ensure_json_string(sanitize(value))
    except (TypeError, ValueError, RecursionError):
        return None


class BaseMappedSpanExporter(SpanExporter):
    """Map native SDK spans and apply the same privacy policy as FITracer.

    Pipecat uses the ordinary OTel API, not FITracer; mapping alone is therefore
    not a privacy boundary. Remove native aliases as well as masking canonical
    attributes so hidden content cannot survive in a second representation.
    """

    def __init__(self, *args, config=None, **kwargs):
        self._config = config if config is not None else TraceConfig()
        if not isinstance(self._config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        if hasattr(self._config, "hide_error_details"):
            self._hide_error_details = self._config.hide_error_details
        else:
            explicit = os.getenv("FI_HIDE_ERROR_DETAILS", "").lower()
            self._hide_error_details = (
                explicit == "true" if explicit in ("true", "false")
                else any(getattr(self._config, key, False) for key in _CONTENT_HIDE_FLAGS)
            )
        super().__init__(*args, **kwargs)

    def _mask_attributes(self, attributes):
        return {key: masked for key, value in attributes.items()
                if not (self._hide_error_details and _is_error_detail(key))
                and (masked := self._config.mask(key, value)) is not None}

    def _convert_attributes(self, attributes):
        mapped = _map_attributes_to_fi_conventions(dict(attributes or {}))
        config = self._config
        hide_input = config.hide_inputs or config.hide_input_text or config.hide_input_messages
        hide_output = config.hide_outputs or config.hide_output_text or config.hide_output_messages
        input_transcript = mapped.get("transcript.is_input") is True
        mapped = {key: value for key, value in mapped.items()
                  if not _hidden_content_key(key, hide_input, hide_output, input_transcript)}
        if (hide_input or hide_output) and "metadata" in mapped:
            metadata = _sanitize_metadata(mapped["metadata"], hide_input, hide_output, input_transcript)
            if metadata is None:
                mapped.pop("metadata")
            else:
                mapped["metadata"] = metadata
        return self._mask_attributes(mapped)

    def export(self, spans) -> SpanExportResult:
        converted = []
        for span in spans:
            try:
                # Do not mutate a ReadableSpan shared with another processor.
                mapped = copy.copy(span)
                mapped._attributes = self._convert_attributes(span.attributes)
                if self._hide_error_details:
                    mapped._status = Status(span.status.status_code)
                events = []
                for event in span.events:
                    attributes = self._mask_attributes(event.attributes or {})
                    events.append(Event(event.name, attributes=attributes, timestamp=event.timestamp))
                mapped._events = tuple(events)
                converted.append(mapped)
            except Exception:
                # Never export original plaintext when conversion/redaction fails.
                # No exception text: it may contain the very payload being hidden.
                logger.warning("Dropping Pipecat span: attribute mapping or privacy policy failed")
        return super().export(converted)
