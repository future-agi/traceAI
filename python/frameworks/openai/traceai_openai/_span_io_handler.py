import json
import logging
from typing import Any, Optional

from fi_instrumentation.fi_types import FiMimeTypeValues, SpanAttributes
from traceai_openai._with_span import _WithSpan

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _process_input_data(input_data: Any, span: _WithSpan) -> None:
    """Process the input data and add relevant attributes to the span.

    Args:
        input_data: Input data (messages or prompt)
        span: The span to add attributes to
    """
    if isinstance(input_data, list):
        input_content = []
        input_images = []
        eval_input = []
        for msg in input_data:
            if isinstance(msg, dict):
                msg_content = msg.get("content", "")
                if isinstance(msg_content, list):
                    filtered_content = []
                    for item in msg_content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                filtered_content.append(item)
                                eval_input.append(item.get("text", ""))
                            elif item.get("type") == "image_url":
                                url_data = item.get("image_url", {})
                                url = url_data.get("url")
                                if url:
                                    input_images.append(url)
                                    image_index = len(input_images) - 1
                                    eval_input.append(
                                        "{{"
                                        + f"{SpanAttributes.INPUT_IMAGES}.{image_index}"
                                        + "}}"
                                    )
                    if filtered_content:
                        msg_dict = msg.copy()
                        msg_dict["content"] = filtered_content
                        input_content.append(msg_dict)
                    else:
                        continue
                else:
                    input_content.append(msg)
                    eval_input.append(msg_content)
        if input_content:
            input_value = json.dumps(input_content, ensure_ascii=False)
            span.set_attribute(SpanAttributes.INPUT_VALUE, input_value)
        if input_images:
            images_value = json.dumps(input_images, ensure_ascii=False)
            span.set_attribute(SpanAttributes.INPUT_IMAGES, images_value)
        if eval_input:
            eval_input_str = " \n ".join(map(str, eval_input))
            span.set_attribute(SpanAttributes.INPUT_VALUE, eval_input_str)
        if eval_input and len(eval_input) > 0:
            span.set_attribute(SpanAttributes.INPUT_VALUE, eval_input[0])
            _set_mime_type(span, SpanAttributes.INPUT_MIME_TYPE, eval_input[0])
    elif isinstance(input_data, str):
        # A bare prompt (e.g. Responses API ``input="..."``) is already the input
        # text; JSON-encoding it would wrap it in quotes.
        span.set_attribute(SpanAttributes.INPUT_VALUE, input_data)
        span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, FiMimeTypeValues.TEXT.value)
    else:
        try:
            input_str = json.dumps(input_data, ensure_ascii=False).strip()
        except (TypeError, ValueError):
            input_str = str(input_data).strip()
        span.set_attribute(SpanAttributes.INPUT_VALUE, input_str)
        _set_mime_type(span, SpanAttributes.INPUT_MIME_TYPE, input_str)


def _set_mime_type(span: _WithSpan, key: str, value: Any) -> None:
    """Label a value JSON only when it parses as a JSON object or array."""
    mime = FiMimeTypeValues.TEXT.value
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            json.loads(value)
            mime = FiMimeTypeValues.JSON.value
        except ValueError:
            pass
    span.set_attribute(key, mime)


def _json_output_for(output_data: Any) -> Optional[str]:
    """JSON for responses whose output is not a chat message: the Responses API
    object as JSON, and a compact summary for embeddings (never the vectors)."""
    try:
        from openai.types import CreateEmbeddingResponse
        from openai.types.responses import Response
    except ImportError:  # pragma: no cover - SDKs older than the floor
        return None
    if isinstance(output_data, Response):
        # Lead with the answer text (the SDK's `output_text`) so previews show it.
        body = json.loads(output_data.model_dump_json(exclude_none=True))
        output_text = getattr(output_data, "output_text", None)
        if output_text:
            body = {"output_text": output_text, **body}
        return json.dumps(body, ensure_ascii=False)
    if isinstance(output_data, CreateEmbeddingResponse):
        data = list(output_data.data or [])
        first = getattr(data[0], "embedding", None) if data else None
        summary = {
            "model": output_data.model,
            "embeddings": len(data),
            "dimensions": len(first) if isinstance(first, list) else None,
        }
        return json.dumps({k: v for k, v in summary.items() if v is not None})
    return None


def add_io_to_span_attributes(
    span: _WithSpan, input_data: Any, output_data: Any, is_streaming: bool = False
) -> None:
    """Add input/output data to span attributes.

    Args:
        span: The span to add attributes to
        input_data: Input data (messages or prompt)
        output_data: Output data from the response
        is_streaming: Whether this is a streaming response
    """
    try:
        # Process input data
        if input_data is not None:
            _process_input_data(input_data, span)

        # Process output data if not streaming
        if output_data is not None and not is_streaming:
            if hasattr(output_data, "parse") and not hasattr(output_data, "model_dump"):
                output_data = output_data.parse()
            if (json_output := _json_output_for(output_data)) is not None:
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, json_output)
                span.set_attribute(
                    SpanAttributes.OUTPUT_MIME_TYPE, FiMimeTypeValues.JSON.value
                )
                return
            # Handle APIResponse object
            if hasattr(output_data, "model_dump"):
                output_data = output_data.model_dump()
            elif hasattr(output_data, "parse"):
                output_data = output_data.parse()

            # Extract output content based on response type
            output_content = None
            if hasattr(output_data, "choices") and output_data.choices:
                first_choice = output_data.choices[0]
                if hasattr(first_choice, "message"):
                    message = first_choice.message
                    if hasattr(message, "tool_calls") and message.tool_calls:
                        # Handle tool calls
                        tool_calls = [
                            f"Function: {tc.function.name}({tc.function.arguments})"
                            for tc in message.tool_calls
                            if tc.type == "function"
                        ]
                        output_content = " \n ".join(tool_calls)
                    elif getattr(message, "content", None):
                        output_content = message.content
                elif hasattr(first_choice, "text"):
                    output_content = first_choice.text
                elif hasattr(first_choice, "delta"):
                    output_content = getattr(first_choice.delta, "content", "")
                else:
                    output_content = str(first_choice)
            elif isinstance(output_data, dict) and "choices" in output_data:
                first_choice = output_data["choices"][0]
                message = first_choice.get("message", {})
                if "tool_calls" in message and message["tool_calls"]:
                    tool_calls = [
                        f"Function: {tc['function']['name']}({tc['function']['arguments']})"
                        for tc in message["tool_calls"]
                        if tc.get("type") == "function"
                    ]
                    output_content = " \n ".join(tool_calls)
                elif "content" in message:
                    output_content = message["content"]
            elif isinstance(output_data, (dict, list)):
                output_content = json.dumps(output_data, ensure_ascii=False, default=str)
            else:
                output_content = str(output_data)

            # Set output.value to just the assistant's response content
            if output_content:
                span.set_attribute(SpanAttributes.OUTPUT_VALUE, output_content)
                _set_mime_type(span, SpanAttributes.OUTPUT_MIME_TYPE, output_content)

    except Exception as e:
        logger.exception(f"Error adding I/O to span attributes: {e}")


def add_stream_output_to_span(span: _WithSpan, chunk_data: Any) -> None:
    """Add streaming output chunk to span attributes.

    Args:
        span: The span to add attributes to
        chunk_data: The streaming chunk data to process
    """
    try:
        output_content = None

        # Extract content from chunk
        if hasattr(chunk_data, "choices") and chunk_data.choices:
            first_choice = chunk_data.choices[0]
            if hasattr(first_choice, "delta"):
                output_content = getattr(first_choice.delta, "content", "")
            elif hasattr(first_choice, "text"):
                output_content = first_choice.text
            else:
                output_content = str(first_choice)

        # Only append non-empty content
        if output_content and output_content.strip():
            existing_output = span.get_attribute(SpanAttributes.OUTPUT_VALUE) or ""
            updated_output = (existing_output + output_content).strip()
            span.set_attribute(SpanAttributes.OUTPUT_VALUE, updated_output)
    except Exception as e:
        logger.exception(f"Error adding streaming output to span attributes: {e}")
