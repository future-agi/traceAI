"""Span attributes for google.generativeai requests and responses.

Content leaves this module only as span attributes; the tracer's TraceConfig masks them on the way
out, so nothing here decides what is private. Binary payloads are never copied: an inline blob is
noted by its MIME type and size.
"""
import base64
import inspect
import json
import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    FiLLMProviderValues,
    FiSpanKindValues,
    MessageAttributes,
    SpanAttributes,
    ToolAttributes,
    ToolCallAttributes,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

Attrs = Iterator[Tuple[str, Any]]

_CONFIG_ATTRIBUTES = {
    "temperature": SpanAttributes.GEN_AI_REQUEST_TEMPERATURE,
    "top_p": SpanAttributes.GEN_AI_REQUEST_TOP_P,
    "top_k": SpanAttributes.GEN_AI_REQUEST_TOP_K,
    "max_output_tokens": SpanAttributes.GEN_AI_REQUEST_MAX_TOKENS,
    "presence_penalty": SpanAttributes.GEN_AI_REQUEST_PRESENCE_PENALTY,
    "frequency_penalty": SpanAttributes.GEN_AI_REQUEST_FREQUENCY_PENALTY,
    "seed": SpanAttributes.GEN_AI_REQUEST_SEED,
}


# proto-plus renamed the "leave unset fields out" switch with protobuf 5; use whichever it accepts.
_OMIT_DEFAULTS: Dict[str, Any] = {"always_print_fields_with_no_presence": False}


def _is_option_error(error: TypeError, option: str) -> bool:
    return "unexpected keyword argument" in str(error) and option in str(error)


def to_dict(message: Any) -> Dict[str, Any]:
    """A proto-plus message as a plain dict, enums by name, unset fields left out."""
    global _OMIT_DEFAULTS
    options = dict(use_integers_for_enums=False, preserving_proto_field_name=True)
    try:
        return type(message).to_dict(message, **options, **_OMIT_DEFAULTS)  # type: ignore[no-any-return]
    except TypeError as error:
        # Only the keyword rename is retried; any other TypeError is the caller's to see.
        (option,) = _OMIT_DEFAULTS
        if not _is_option_error(error, option):
            raise
        fallback = {"including_default_value_fields": False}
        result = type(message).to_dict(message, **options, **fallback)  # type: ignore[no-any-return]
        _OMIT_DEFAULTS = fallback
        return result


def _scrub_blobs(value: Any) -> Any:
    """A copy of a request/response dict with every inline_data blob replaced by its type and size note."""
    if isinstance(value, Mapping):
        # Under its own key: the core's image policy blanks any value found under an image-payload key.
        return {
            ("blob_note" if key == "inline_data" and isinstance(item, Mapping) else key):
            _blob_note("inline_data", item or {}) if key == "inline_data" and isinstance(item, Mapping) else _scrub_blobs(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_blobs(item) for item in value]
    return value


def _one_shot(value: Any) -> bool:
    """An iterator (a generator, say) can be read once: tracing must never be the one to read it."""
    return not isinstance(value, (str, bytes, Mapping)) and iter(value) is value if hasattr(value, "__iter__") else False


def bind(wrapped: Any, args: Tuple[Any, ...], kwargs: Mapping[str, Any]) -> Dict[str, Any]:
    try:
        bound = inspect.signature(wrapped).bind(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except Exception:
        return {}


def _bare_model(name: Any) -> Optional[str]:
    return name if isinstance(name, str) and name else None


def _blob_note(kind: str, blob: Mapping[str, Any]) -> str:
    size = ""
    data = blob.get("data")
    if isinstance(data, str):
        try:
            size = f", {len(base64.b64decode(data, validate=False))} bytes"
        except Exception:
            size = ""
    elif isinstance(data, (bytes, bytearray)):
        size = f", {len(data)} bytes"
    return f"[{kind}: {blob.get('mime_type', 'unknown')}{size}]"


def message_attributes(content: Mapping[str, Any], default_role: str = "user") -> Attrs:
    """Role, text and tool calls of one Content dict; function results become JSON content."""
    texts: List[str] = []
    calls = 0
    for part in content.get("parts") or []:
        if not isinstance(part, Mapping):
            continue
        if "text" in part:
            texts.append(str(part["text"]))
        elif "function_call" in part:
            call = part["function_call"] or {}
            prefix = f"{MessageAttributes.MESSAGE_TOOL_CALLS}.{calls}"
            if call.get("name"):
                yield f"{prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", call["name"]
            yield f"{prefix}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", safe_json_dumps(call.get("args") or {})
            calls += 1
        elif "function_response" in part:
            texts.append(safe_json_dumps(part["function_response"]))
        elif "inline_data" in part:
            texts.append(_blob_note("inline_data", part["inline_data"] or {}))
        elif "file_data" in part:
            texts.append(f"[file: {(part['file_data'] or {}).get('file_uri', '')}]")
        elif "executable_code" in part or "code_execution_result" in part:
            texts.append(safe_json_dumps(part))
    yield MessageAttributes.MESSAGE_ROLE, content.get("role") or default_role
    if texts or not calls:
        yield MessageAttributes.MESSAGE_CONTENT, "\n".join(texts)


def prepare_request(instance: Any, arguments: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The request the SDK will send, as a dict, built by the SDK's own request preparation."""
    if any(_one_shot(arguments.get(key)) for key in ("contents", "content", "tools", "safety_settings")):
        return None
    try:
        request = instance._prepare_request(
            contents=arguments.get("contents", arguments.get("content")),
            generation_config=arguments.get("generation_config"),
            safety_settings=arguments.get("safety_settings"),
            tools=arguments.get("tools"),
            tool_config=arguments.get("tool_config"),
        )
        return to_dict(request)
    except Exception:
        logger.debug("could not prepare the request for tracing", exc_info=True)
        return None


def request_attributes(instance: Any, arguments: Mapping[str, Any]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.LLM.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: FiLLMProviderValues.GOOGLE.value,
        SpanAttributes.GEN_AI_OPERATION_NAME: "generate_content",
    }
    model = _bare_model(getattr(instance, "model_name", None))
    if model:
        attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
    request = prepare_request(instance, arguments)
    if request is None:
        return attributes
    config = request.get("generation_config") or {}
    if config:
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = safe_json_dumps(config)
        for key, attribute in _CONFIG_ATTRIBUTES.items():
            if key in config:
                attributes[attribute] = config[key]
        if config.get("stop_sequences"):
            attributes[SpanAttributes.GEN_AI_REQUEST_STOP_SEQUENCES] = list(config["stop_sequences"])
    contents = request.get("contents") or []
    index = 0
    system = request.get("system_instruction")
    if system:
        for key, value in message_attributes(system, "system"):
            attributes[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{key}"] = value
        index += 1
    for content in contents:
        for key, value in message_attributes(content):
            attributes[f"{SpanAttributes.GEN_AI_INPUT_MESSAGES}.{index}.{key}"] = value
        index += 1
    tool_index = 0
    for tool in request.get("tools") or []:
        declarations = tool.get("function_declarations")
        for declaration in declarations if declarations else [tool]:
            attributes[
                f"{SpanAttributes.GEN_AI_TOOL_DEFINITIONS}.{tool_index}.{ToolAttributes.TOOL_JSON_SCHEMA}"
            ] = safe_json_dumps(declaration)
            tool_index += 1
    attributes[SpanAttributes.INPUT_VALUE] = safe_json_dumps(
        _scrub_blobs({"contents": contents, **({"system_instruction": system} if system else {})})
    )
    attributes[SpanAttributes.INPUT_MIME_TYPE] = "application/json"
    return attributes


def response_attributes(response: Any) -> Dict[str, Any]:
    """Output, usage and finish reasons of a (possibly aggregated streamed) response."""
    result = getattr(response, "_result", None)
    if result is None:
        return {}
    data = to_dict(result)
    attributes: Dict[str, Any] = {}
    if data.get("model_version"):
        attributes[SpanAttributes.GEN_AI_RESPONSE_MODEL] = data["model_version"]
    usage = data.get("usage_metadata") or {}
    if "prompt_token_count" in usage:
        attributes[SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS] = usage["prompt_token_count"]
    if "candidates_token_count" in usage:
        attributes[SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS] = usage["candidates_token_count"]
    if "total_token_count" in usage:
        attributes[SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS] = usage["total_token_count"]
    if usage.get("cached_content_token_count"):
        attributes[SpanAttributes.GEN_AI_USAGE_CACHE_READ_TOKENS] = usage["cached_content_token_count"]
    reasons: List[str] = []
    for position, candidate in enumerate(data.get("candidates") or []):
        index = candidate.get("index", position)
        for key, value in message_attributes(candidate.get("content") or {}, "model"):
            attributes[f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{index}.{key}"] = value
        if candidate.get("finish_reason"):
            reasons.append(str(candidate["finish_reason"]))
    if reasons:
        attributes[SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS] = reasons
    block = (data.get("prompt_feedback") or {}).get("block_reason")
    if block:
        attributes["gen_ai.response.block_reason"] = str(block)
    attributes[SpanAttributes.OUTPUT_VALUE] = safe_json_dumps(_scrub_blobs(data))
    attributes[SpanAttributes.OUTPUT_MIME_TYPE] = "application/json"
    return attributes


def _texts_of(content: Any) -> List[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, Mapping) or hasattr(content, "parts"):
        data = content if isinstance(content, Mapping) else to_dict(content)
        return ["\n".join(str(p["text"]) for p in data.get("parts") or [] if isinstance(p, Mapping) and "text" in p)]
    if isinstance(content, (list, tuple)):
        return [text for item in content for text in _texts_of(item)]
    return []


def embedding_request_attributes(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    attributes: Dict[str, Any] = {
        SpanAttributes.GEN_AI_SPAN_KIND: FiSpanKindValues.EMBEDDING.value,
        SpanAttributes.GEN_AI_PROVIDER_NAME: FiLLMProviderValues.GOOGLE.value,
        SpanAttributes.GEN_AI_OPERATION_NAME: "embeddings",
    }
    model = _bare_model(arguments.get("model"))
    if model:
        attributes[SpanAttributes.GEN_AI_REQUEST_MODEL] = model
        attributes[SpanAttributes.EMBEDDING_MODEL_NAME] = model
    parameters = {
        key: str(arguments[key]) if key == "task_type" else arguments[key]
        for key in ("task_type", "output_dimensionality")
        if arguments.get(key) is not None
    }
    if parameters:
        attributes[SpanAttributes.GEN_AI_REQUEST_PARAMETERS] = json.dumps(parameters, default=str)
    if arguments.get("title") is not None:
        # The title is input text: it sits under an embedding-text key so it is masked with the embedded text.
        attributes[f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.title.embedding.text"] = str(arguments["title"])
    for index, text in enumerate(_texts_of(arguments.get("content"))):
        attributes[f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{index}.embedding.text"] = text
    return attributes


def embedding_response_attributes(result: Any) -> Dict[str, Any]:
    """Only the vector size: the vectors themselves are never exported."""
    if not isinstance(result, Mapping):
        return {}
    vectors = result.get("embedding")
    if isinstance(vectors, (list, tuple)) and vectors:
        first = vectors[0]
        size = len(first) if isinstance(first, (list, tuple)) else len(vectors)
        return {SpanAttributes.GEN_AI_EMBEDDINGS_DIMENSION_COUNT: size}
    return {}
