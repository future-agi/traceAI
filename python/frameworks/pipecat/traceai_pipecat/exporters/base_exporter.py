"""
Base exporter for Pipecat integration with Future AGI.

This module provides the base class for mapped span exporters that convert
Pipecat attributes to Future AGI conventions.
"""

import json
from typing import Any, Dict

from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult


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

    # Pipecat → FI LLM basics
    if "gen_ai.system" in attributes:
        mapped["llm.provider"] = attributes.get("gen_ai.system")

    if "gen_ai.request.model" in attributes:
        mapped["llm.model_name"] = attributes.get("gen_ai.request.model")

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
                    mapped[f"llm.input_messages.{index}.message.role"] = role
                if content is not None:
                    # Content may be str or structured; serialize non-str to JSON
                    mapped[f"llm.input_messages.{index}.message.content"] = (
                        content
                        if isinstance(content, str)
                        else _ensure_json_string(content)
                    )

    # STT transcript or LLM output
    out_val = attributes.get("output") or attributes.get("transcript")
    if out_val is not None and "output.value" not in mapped:
        mapped["output.value"] = out_val
        mapped["output.mime_type"] = _detect_mime_type(out_val)

        # Also expose output as enumerated LLM output message 0
        if "llm.output_messages.0.message.content" not in mapped:
            mapped["llm.output_messages.0.message.role"] = "assistant"
            mapped["llm.output_messages.0.message.content"] = (
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
        mapped["llm.invocation_parameters"] = _ensure_json_string(invocation_params)

    # Token usage
    if "gen_ai.usage.input_tokens" in attributes:
        mapped["llm.token_count.prompt"] = attributes.get("gen_ai.usage.input_tokens")
    if "gen_ai.usage.output_tokens" in attributes:
        mapped["llm.token_count.completion"] = attributes.get(
            "gen_ai.usage.output_tokens"
        )
    # Total tokens
    try:
        prompt_tokens = mapped.get("llm.token_count.prompt")
        completion_tokens = mapped.get("llm.token_count.completion")
        if isinstance(prompt_tokens, (int, float)) and isinstance(
            completion_tokens, (int, float)
        ):
            mapped["llm.token_count.total"] = int(prompt_tokens) + int(
                completion_tokens
            )
    except Exception:
        pass

    # Tools
    if "tools" in attributes:
        mapped["llm.tools"] = (
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
    if "conversation.id" in attributes and "session.id" not in mapped:
        mapped["session.id"] = attributes.get("conversation.id")

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

    # Determine fi.span.kind from context
    # Defaults: LLM for LLM/STT/TTS-like spans; TOOL for tool spans; AGENT for setup; CHAIN for turn/conversation
    if "fi.span.kind" not in mapped:
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
        mapped["fi.span.kind"] = span_kind

    return mapped


class BaseMappedSpanExporter(SpanExporter):
    """Base class for span exporters that map Pipecat attributes to Future AGI conventions."""

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
                    mapped_attributes = self._convert_attributes(base_attributes)

                    setattr(span, "_attributes", mapped_attributes)

            except Exception:
                continue

        return super().export(spans)
