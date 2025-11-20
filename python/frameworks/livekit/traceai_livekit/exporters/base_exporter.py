"""
Base exporter for LiveKit integration with Future AGI.

This module provides the base class for mapped span exporters that convert
LiveKit attributes to Future AGI conventions.
"""

import json
from typing import Any, Dict

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

# Import Future AGI span attribute keys
from fi_instrumentation.fi_types import SpanAttributes


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
    """Map LiveKit attributes to Future AGI conventions."""
    if not attributes:
        return {}

    mapped: Dict[str, Any] = dict(attributes)  # start by preserving originals

    # --- LiveKit Constant Keys (from livekit-agents/telemetry/trace_types.py) ---
    
    # Common Keys
    LK_SPEECH_ID = "lk.speech_id"
    LK_AGENT_LABEL = "lk.agent_label"
    LK_START_TIME = "lk.start_time"
    LK_END_TIME = "lk.end_time"
    LK_RETRY_COUNT = "lk.retry_count"

    LK_PARTICIPANT_ID = "lk.participant_id"
    LK_PARTICIPANT_IDENTITY = "lk.participant_identity"
    LK_PARTICIPANT_KIND = "lk.participant_kind"
    
    # Session Start
    LK_JOB_ID = "lk.job_id"
    LK_AGENT_NAME = "lk.agent_name"
    LK_ROOM_NAME = "lk.room_name"
    LK_SESSION_OPTIONS = "lk.session_options"

    # Assistant Turn
    LK_USER_INPUT = "lk.user_input"
    LK_INSTRUCTIONS = "lk.instructions"
    LK_SPEECH_INTERRUPTED = "lk.interrupted"

    # LLM Node
    LK_CHAT_CTX = "lk.chat_ctx"
    LK_FUNCTION_TOOLS = "lk.function_tools"
    LK_RESPONSE_TEXT = "lk.response.text"
    LK_RESPONSE_FUNCTION_CALLS = "lk.response.function_calls"

    # Function Tool
    LK_FUNCTION_TOOL_NAME = "lk.function_tool.name"
    LK_FUNCTION_TOOL_ARGS = "lk.function_tool.arguments"
    LK_FUNCTION_TOOL_IS_ERROR = "lk.function_tool.is_error"
    LK_FUNCTION_TOOL_OUTPUT = "lk.function_tool.output"

    # TTS Node
    LK_TTS_INPUT_TEXT = "lk.input_text"
    LK_TTS_STREAMING = "lk.tts.streaming"
    LK_TTS_LABEL = "lk.tts.label"

    # EOU Detection & Metrics (used for Metadata)
    LK_EOU_DELAY = "lk.eou.endpointing_delay"
    LK_EOU_LANGUAGE = "lk.eou.language"
    LK_USER_TRANSCRIPT = "lk.user_transcript"
    LK_TRANSCRIPT_CONFIDENCE = "lk.transcript_confidence"
    LK_TRANSCRIPTION_DELAY = "lk.transcription_delay"
    LK_END_OF_TURN_DELAY = "lk.end_of_turn_delay"

    # Standard OTel GenAI
    ATTR_GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
    ATTR_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
    ATTR_GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    ATTR_GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    
    # --- MAPPING LOGIC ---

    # 1. LLM & Model Info
    if ATTR_GEN_AI_REQUEST_MODEL in attributes:
        mapped[SpanAttributes.LLM_MODEL_NAME] = attributes.get(ATTR_GEN_AI_REQUEST_MODEL)

    # 2. Inputs & Outputs
    # LiveKit separates User Input (in Assistant Turn) vs LLM Response vs TTS Input
    
    input_val = None
    output_val = None

    # Case A: Assistant Turn (Top-level chain)
    # Input: User speech/text (LK_USER_INPUT)
    # Output: often not directly on this span, but might be aggregated or absent
    if LK_USER_INPUT in attributes:
        input_val = attributes.get(LK_USER_INPUT)

    # Case B: LLM Node
    # Input: Chat Context (LK_CHAT_CTX) - usually a list of messages
    # Output: Generated Text (LK_RESPONSE_TEXT) or Function Calls (LK_RESPONSE_FUNCTION_CALLS)
    if LK_CHAT_CTX in attributes:

        chat_ctx = attributes.get(LK_CHAT_CTX)
        input_val = _ensure_json_string(chat_ctx)

    if LK_RESPONSE_TEXT in attributes:
        output_val = attributes.get(LK_RESPONSE_TEXT)
    
    if LK_RESPONSE_FUNCTION_CALLS in attributes:
        # If there are function calls, this is also an output
        func_calls = attributes.get(LK_RESPONSE_FUNCTION_CALLS)
        if output_val:
             # If we already have text, append function calls
             output_val = f"{output_val}\nFunction Calls: {_ensure_json_string(func_calls)}"
        else:
             output_val = _ensure_json_string(func_calls)

    # Case C: TTS Node
    # Input: Text to speak (LK_TTS_INPUT_TEXT)
    # Output: Audio (not usually in attributes, maybe just success)
    if LK_TTS_INPUT_TEXT in attributes:
        input_val = attributes.get(LK_TTS_INPUT_TEXT)
        # TTS output is audio, we don't have the bytes here usually.

    # Case D: Function Tool Execution
    # Input: Arguments (LK_FUNCTION_TOOL_ARGS)
    # Output: Tool Output (LK_FUNCTION_TOOL_OUTPUT)
    if LK_FUNCTION_TOOL_ARGS in attributes:
         input_val = attributes.get(LK_FUNCTION_TOOL_ARGS)
    
    if LK_FUNCTION_TOOL_OUTPUT in attributes:
         output_val = attributes.get(LK_FUNCTION_TOOL_OUTPUT)

    # --- Set Input/Output Attributes ---
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
        mapped[SpanAttributes.LLM_TOKEN_COUNT_PROMPT] = attributes.get(ATTR_GEN_AI_USAGE_INPUT_TOKENS)
    if ATTR_GEN_AI_USAGE_OUTPUT_TOKENS in attributes:
        mapped[SpanAttributes.LLM_TOKEN_COUNT_COMPLETION] = attributes.get(ATTR_GEN_AI_USAGE_OUTPUT_TOKENS)
    
    # Calculate total if not present
    try:
        prompt_tokens = mapped.get(SpanAttributes.LLM_TOKEN_COUNT_PROMPT)
        completion_tokens = mapped.get(SpanAttributes.LLM_TOKEN_COUNT_COMPLETION)
        if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
            mapped[SpanAttributes.LLM_TOKEN_COUNT_TOTAL] = int(prompt_tokens) + int(completion_tokens)
    except Exception:
        pass

    # 4. Tools
    if LK_FUNCTION_TOOL_NAME in attributes:
        mapped[SpanAttributes.TOOL_NAME] = attributes.get(LK_FUNCTION_TOOL_NAME)
        mapped["tool_call.function.name"] = attributes.get(LK_FUNCTION_TOOL_NAME) # Standardize

    if LK_FUNCTION_TOOLS in attributes:
        # List of available tools provided to LLM
        mapped[SpanAttributes.LLM_TOOLS] = _ensure_json_string(attributes.get(LK_FUNCTION_TOOLS))

    # 5. Session & User Info
    if LK_ROOM_NAME in attributes:
        mapped[SpanAttributes.SESSION_ID] = attributes.get(LK_ROOM_NAME)
    
    if LK_PARTICIPANT_IDENTITY in attributes:
        mapped[SpanAttributes.USER_ID] = attributes.get(LK_PARTICIPANT_IDENTITY)

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
        if key in attributes:
            metadata[key] = attributes[key]

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
    if SpanAttributes.FI_SPAN_KIND not in mapped:
        span_kind = "UNKNOWN"
        
        # Check based on attribute presence or span name (if available in attributes? No, span name is on the Span object)
        # We infer from unique attributes.
        
        if LK_FUNCTION_TOOL_NAME in attributes:
            span_kind = "TOOL"
        elif LK_CHAT_CTX in attributes or ATTR_GEN_AI_REQUEST_MODEL in attributes:
            span_kind = "LLM"
        elif LK_TTS_INPUT_TEXT in attributes:
             # TTS is technically model inference, map to LLM for now or generic
             span_kind = "LLM" 
        elif LK_USER_INPUT in attributes or LK_SPEECH_ID in attributes:
             # Assistant Turn -> CHAIN
             span_kind = "CHAIN"
        elif LK_JOB_ID in attributes and LK_ROOM_NAME in attributes and LK_SESSION_OPTIONS in attributes:
             # Session Start -> AGENT
             span_kind = "AGENT"

        mapped[SpanAttributes.FI_SPAN_KIND] = span_kind

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
        except Exception:
            return base

    def export(self, spans) -> SpanExportResult:
        for span in spans:
            try:
                if (
                    hasattr(span, "_attributes")
                    and getattr(span, "_attributes") is not None
                ):
                    original_attributes = getattr(span, "_attributes")

                    base_attributes = dict(original_attributes)
                    
                    # Include span name in mapping logic if needed, though _convert_attributes currently only takes dict
                    # We could pass span name to _map_attributes_to_fi_conventions if we refactored, 
                    # but attribute-based detection is usually robust enough for LiveKit.
                    
                    mapped_attributes = self._convert_attributes(base_attributes)

                    setattr(span, "_attributes", mapped_attributes)

            except Exception:
                continue

        return super().export(spans)

