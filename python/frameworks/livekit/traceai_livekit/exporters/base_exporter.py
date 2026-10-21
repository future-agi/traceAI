"""
Base exporter for LiveKit integration with Future AGI.

This module provides the base class for mapped span exporters that convert
LiveKit attributes to Future AGI conventions.
"""

import json
import logging
import threading
from typing import Any, Dict, Iterable, Tuple

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

# Import Future AGI span attribute keys
from fi_instrumentation.fi_types import SpanAttributes

logger = logging.getLogger(__name__)

# The mapping failure is reported once per process: a broken install fails on every span.
_mapping_failure_logged = False
_mapping_failure_lock = threading.Lock()


def _warn_mapping_failed_once(exc: BaseException) -> None:
    """Log, once per process, that the Future AGI mapping failed. Never logs attribute values."""
    global _mapping_failure_logged
    with _mapping_failure_lock:
        if _mapping_failure_logged:
            return
        _mapping_failure_logged = True
    logger.warning(
        "traceai-livekit could not map LiveKit span attributes to Future AGI conventions "
        "(%s); spans are exported without the Future AGI mapping. "
        "Check that fi-instrumentation-otel >= 1.1.0 is installed.",
        type(exc).__name__,
    )


def _first(attributes: Dict[str, Any], keys: Iterable[str]) -> Tuple[bool, Any]:
    """Return (found, value) for the first of `keys` present in `attributes`.

    LiveKit 1.7 renamed its content attributes (`lk.user_input` -> `lk.pii.user_input`, and so
    on). Callers pass the new name first and the earlier name after it; the new name wins when
    both are present.
    """
    for key in keys:
        if key in attributes:
            return True, attributes[key]
    return False, None


def _has(attributes: Dict[str, Any], keys: Iterable[str]) -> bool:
    return _first(attributes, keys)[0]


def _present(value: Any) -> bool:
    """Whether a content value holds something: not None and not an empty string.

    `_ensure_json_string("")` would turn an empty value into the JSON text `'""'`.
    """
    return value is not None and value != ""


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


def _is_empty_function_calls(value: Any) -> bool:
    """Whether a function-calls attribute holds no call: absent, empty, or an empty JSON list."""
    if value is None:
        return True
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    if isinstance(value, str):
        return value.strip() in ("", "[]", "null")
    return False


def _map_attributes_to_fi_conventions(attributes: Dict[str, Any]) -> Dict[str, Any]:
    """Map LiveKit attributes to Future AGI conventions."""
    if not attributes:
        return {}

    mapped: Dict[str, Any] = dict(attributes)  # start by preserving originals

    # --- LiveKit Constant Keys (from livekit-agents/telemetry/trace_types.py) ---
    
    # Common Keys
    LK_SPEECH_ID = "lk.speech_id"
    LK_AGENT_LABEL = "lk.agent_label"

    LK_PARTICIPANT_ID = "lk.participant_id"
    LK_PARTICIPANT_IDENTITY = ("lk.pii.participant_identity", "lk.participant_identity")
    LK_PARTICIPANT_KIND = "lk.participant_kind"
    
    # Session Start
    LK_JOB_ID = "lk.job_id"
    LK_AGENT_NAME = "lk.agent_name"
    LK_ROOM_NAME = ("lk.pii.room_name", "lk.room_name")
    LK_SESSION_OPTIONS = "lk.session_options"

    # Assistant Turn
    LK_USER_INPUT = ("lk.pii.user_input", "lk.user_input")
    LK_INSTRUCTIONS = ("lk.pii.instructions", "lk.instructions")
    LK_SPEECH_INTERRUPTED = "lk.interrupted"

    # LLM Node
    LK_CHAT_CTX = ("lk.pii.chat_ctx", "lk.chat_ctx")
    LK_FUNCTION_TOOLS = "lk.function_tools"
    LK_RESPONSE_TEXT = ("lk.pii.response.text", "lk.response.text")
    LK_RESPONSE_FUNCTION_CALLS = ("lk.pii.response.function_calls", "lk.response.function_calls")

    # Function Tool
    LK_FUNCTION_TOOL_NAME = "lk.function_tool.name"
    LK_FUNCTION_TOOL_ARGS = ("lk.pii.function_tool.arguments", "lk.function_tool.arguments")
    LK_FUNCTION_TOOL_IS_ERROR = "lk.function_tool.is_error"
    LK_FUNCTION_TOOL_OUTPUT = ("lk.pii.function_tool.output", "lk.function_tool.output")

    # TTS Node
    LK_TTS_INPUT_TEXT = ("lk.pii.input_text", "lk.input_text")
    LK_TTS_STREAMING = "lk.tts.streaming"
    LK_TTS_LABEL = "lk.tts.label"

    # EOU Detection & Metrics (used for Metadata)
    LK_EOU_DELAY = "lk.eou.endpointing_delay"
    LK_EOU_LANGUAGE = "lk.eou.language"
    LK_USER_TRANSCRIPT = ("lk.pii.user_transcript", "lk.user_transcript")
    LK_TRANSCRIPT_CONFIDENCE = "lk.transcript_confidence"
    LK_TRANSCRIPTION_DELAY = "lk.transcription_delay"
    LK_END_OF_TURN_DELAY = "lk.end_of_turn_delay"

    # Standard OTel GenAI
    ATTR_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
    ATTR_GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    ATTR_GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

    # --- MAPPING LOGIC ---

    # 1. LLM & Model Info
    if ATTR_GEN_AI_REQUEST_MODEL in attributes:
        mapped[SpanAttributes.GEN_AI_REQUEST_MODEL] = attributes.get(ATTR_GEN_AI_REQUEST_MODEL)

    # 2. Inputs & Outputs
    # LiveKit separates User Input (in Assistant Turn) vs LLM Response vs TTS Input
    
    input_val = None
    output_val = None
    raw_input_val = None
    raw_output_val = None

    # Case A: Assistant Turn (Top-level chain)
    # Input: User speech/text (LK_USER_INPUT)
    # Output: often not directly on this span, but might be aggregated or absent
    found, user_input = _first(attributes, LK_USER_INPUT)
    if found and _present(user_input):
        input_val = user_input
        raw_input_val = user_input

    # Case B: LLM Node
    # Input: Chat Context (LK_CHAT_CTX) - usually a list of messages
    # Output: Generated Text (LK_RESPONSE_TEXT) or Function Calls (LK_RESPONSE_FUNCTION_CALLS)
    found, chat_ctx = _first(attributes, LK_CHAT_CTX)
    if found:
        input_val = _ensure_json_string(chat_ctx)
        raw_input_val = chat_ctx

    found, response_text = _first(attributes, LK_RESPONSE_TEXT)
    if found and _present(response_text):
        output_val = response_text
        raw_output_val = output_val

    found, func_calls = _first(attributes, LK_RESPONSE_FUNCTION_CALLS)
    # LiveKit 1.8 sets the function calls on every LLM node, as "[]" when there were none; an empty
    # list is no output, so the reply alone stays the output.
    if found and not _is_empty_function_calls(func_calls):
        # If there are function calls, this is also an output
        if output_val:
             # If we already have text, append function calls
             output_val = f"{output_val}\nFunction Calls: {_ensure_json_string(func_calls)}"
             raw_output_val = {"text": raw_output_val, "function_calls": func_calls}
        else:
             output_val = _ensure_json_string(func_calls)
             raw_output_val = func_calls

    # Case C: TTS Node
    # Input: Text to speak (LK_TTS_INPUT_TEXT)
    # Output: Audio (not usually in attributes, maybe just success)
    found, tts_input = _first(attributes, LK_TTS_INPUT_TEXT)
    if found and _present(tts_input):
        input_val = tts_input
        raw_input_val = tts_input
        # TTS output is audio, we don't have the bytes here usually.

    # Case D: Function Tool Execution
    # Input: Arguments (LK_FUNCTION_TOOL_ARGS)
    # Output: Tool Output (LK_FUNCTION_TOOL_OUTPUT)
    found, tool_args = _first(attributes, LK_FUNCTION_TOOL_ARGS)
    if found and _present(tool_args):
         input_val = tool_args
         raw_input_val = tool_args

    found, tool_output = _first(attributes, LK_FUNCTION_TOOL_OUTPUT)
    if found and _present(tool_output):
         output_val = tool_output
         raw_output_val = tool_output

    # Case E: Session Start
    if LK_SESSION_OPTIONS in attributes and raw_input_val is None:
         raw_input_val = attributes.get(LK_SESSION_OPTIONS)

    # --- Set Input/Output Attributes ---
    if raw_input_val is not None:
        mapped[SpanAttributes.INPUT_VALUE] = _ensure_json_string(raw_input_val)

    if raw_output_val is not None:
        mapped[SpanAttributes.OUTPUT_VALUE] = _ensure_json_string(raw_output_val)

    if input_val is not None and SpanAttributes.INPUT_VALUE not in mapped:
        mime_type = _detect_mime_type(input_val)
        mapped[SpanAttributes.INPUT_VALUE] = (
            _ensure_json_string(input_val) if mime_type == "application/json" else input_val
        )
        mapped[SpanAttributes.INPUT_MIME_TYPE] = mime_type

    if output_val is not None and SpanAttributes.OUTPUT_VALUE not in mapped:
        mime_type = _detect_mime_type(output_val)
        mapped[SpanAttributes.OUTPUT_VALUE] = (
             _ensure_json_string(output_val) if mime_type == "application/json" else output_val
        )
        mapped[SpanAttributes.OUTPUT_MIME_TYPE] = mime_type


    # 3. Token Usage
    if ATTR_GEN_AI_USAGE_INPUT_TOKENS in attributes:
        mapped[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = attributes.get(ATTR_GEN_AI_USAGE_INPUT_TOKENS)
    if ATTR_GEN_AI_USAGE_OUTPUT_TOKENS in attributes:
        mapped[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = attributes.get(ATTR_GEN_AI_USAGE_OUTPUT_TOKENS)
    
    # Calculate total if not present
    try:
        prompt_tokens = mapped.get(SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS)
        completion_tokens = mapped.get(SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS)
        if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
            mapped[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = int(prompt_tokens) + int(completion_tokens)
    except Exception:
        pass

    # 4. Tools
    if LK_FUNCTION_TOOL_NAME in attributes:
        mapped[SpanAttributes.GEN_AI_TOOL_NAME] = attributes.get(LK_FUNCTION_TOOL_NAME)
        mapped["tool_call.function.name"] = attributes.get(LK_FUNCTION_TOOL_NAME) # Standardize

    if LK_FUNCTION_TOOLS in attributes:
        # List of available tools provided to LLM
        mapped[SpanAttributes.GEN_AI_TOOL_DEFINITIONS] = _ensure_json_string(attributes.get(LK_FUNCTION_TOOLS))

    # 5. Session & User Info
    found, room_name = _first(attributes, LK_ROOM_NAME)
    if found:
        mapped[SpanAttributes.GEN_AI_CONVERSATION_ID] = room_name

    found, participant_identity = _first(attributes, LK_PARTICIPANT_IDENTITY)
    if found:
        mapped[SpanAttributes.USER_ID] = participant_identity

    # 6. Metadata Aggregation
    metadata: Dict[str, Any] = {}
    
    # Collect all LiveKit specific keys into metadata
    metadata_keys = [
        LK_SPEECH_ID, LK_AGENT_LABEL, LK_JOB_ID, LK_AGENT_NAME, LK_SESSION_OPTIONS,
        LK_PARTICIPANT_ID, LK_PARTICIPANT_KIND,
        LK_INSTRUCTIONS, LK_SPEECH_INTERRUPTED,
        LK_TTS_STREAMING, LK_TTS_LABEL,
        LK_EOU_DELAY, LK_EOU_LANGUAGE, LK_TRANSCRIPT_CONFIDENCE, LK_TRANSCRIPTION_DELAY, LK_END_OF_TURN_DELAY,
        LK_USER_TRANSCRIPT, # Might be redundant with input, but good context
        LK_FUNCTION_TOOL_IS_ERROR
    ]

    for key in metadata_keys:
        # A renamed key is a (new name, earlier name) tuple; each name present is recorded as is.
        for name in (key if isinstance(key, tuple) else (key,)):
            if name in attributes:
                metadata[name] = attributes[name]

    # Also include any "metrics" attributes if they exist
    for key in attributes:
         if "metrics" in key or "lk.eou" in key:
              metadata[key] = attributes[key]

    # Merge with existing metadata
    if metadata:
        existing = attributes.get(SpanAttributes.METADATA)
        try:
            if isinstance(existing, str):
                existing = json.loads(existing)
            if isinstance(existing, dict):
                metadata = {**existing, **metadata}
        except Exception:
            pass
        mapped[SpanAttributes.METADATA] = _ensure_json_string(metadata)

    # 7. Span Kind Determination (fi.span.kind)
    if SpanAttributes.GEN_AI_SPAN_KIND not in mapped:
        span_kind = "UNKNOWN"
        
        # Check based on attribute presence or span name (if available in attributes? No, span name is on the Span object)
        # We infer from unique attributes.
        
        if LK_FUNCTION_TOOL_NAME in attributes:
            span_kind = "TOOL"
        elif _has(attributes, LK_CHAT_CTX) or ATTR_GEN_AI_REQUEST_MODEL in attributes:
            span_kind = "LLM"
        elif _has(attributes, LK_TTS_INPUT_TEXT):
             # TTS is technically model inference, map to LLM for now or generic
             span_kind = "LLM" 
        elif _has(attributes, LK_USER_INPUT) or LK_SPEECH_ID in attributes:
             # Assistant Turn -> CHAIN
             span_kind = "CHAIN"
        elif LK_JOB_ID in attributes and _has(attributes, LK_ROOM_NAME) and LK_SESSION_OPTIONS in attributes:
             # Session Start -> AGENT
             span_kind = "AGENT"

        mapped[SpanAttributes.GEN_AI_SPAN_KIND] = span_kind

    return mapped


class BaseMappedSpanExporter(SpanExporter):
    """Base class for span exporters that map LiveKit attributes to Future AGI conventions."""

    def _convert_attributes(self, attributes):
        """Convert attributes by mapping them to Future AGI conventions."""
        if attributes is None:
            base = {}
        elif not isinstance(attributes, dict):
            base = dict(attributes)
        else:
            base = attributes
        try:
            return _map_attributes_to_fi_conventions(base)
        except Exception as exc:
            # The span is still exported, without the Future AGI mapping.
            _warn_mapping_failed_once(exc)
            return base

    def export(self, spans) -> SpanExportResult:
        # The spans handed in are shared with every other processor on the provider (LiveKit
        # Cloud's exporter among them), and LiveKit redacts by attribute name. The mapping copies
        # conversation content under names with no "pii" segment (input.value, output.value, the
        # metadata JSON, gen_ai.conversation.id, user.id), so it must never be written back onto
        # a span we were handed: it goes on a copy, as LiveKit's own pii.redact() builds one.
        exported = []
        for span in spans:
            try:
                exported.append(self._mapped_copy(span))
            except Exception as exc:
                # The span is still exported, as it is, without the Future AGI mapping.
                _warn_mapping_failed_once(exc)
                exported.append(span)

        return super().export(exported)

    def _mapped_copy(self, span):
        """A copy of `span` carrying the mapped attributes; `span` itself when there is nothing to map."""
        attributes = span.attributes
        if not attributes:
            return span
        original = dict(attributes)
        mapped = self._convert_attributes(dict(original))
        if mapped == original:
            return span
        return ReadableSpan(
            name=span.name,
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=mapped,
            events=span.events,
            links=span.links,
            kind=span.kind,
            status=span.status,
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )
