import logging
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    EmbeddingAttributes,
    MessageAttributes,
    SpanAttributes,
    ToolCallAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ChatCompletionsResponseAttributesExtractor:
    """Extract span attributes from Together AI chat completions response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Model name from response
        if "model" in response:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, response["model"]

        # Token counts from usage
        usage = response.get("usage", {})
        if usage:
            if "prompt_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage["prompt_tokens"]
            if "completion_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage["completion_tokens"]
            if "total_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage["total_tokens"]
            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                if total:
                    yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        # Extract choices
        choices = response.get("choices", [])
        for i, choice in enumerate(choices):
            message = choice.get("message", {})
            if message:
                role = message.get("role", "assistant")
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role

                content = message.get("content", "")
                if content:
                    yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", content
                    if i == 0:
                        yield SpanAttributes.OUTPUT_VALUE, content

                # Tool calls
                tool_calls = message.get("tool_calls", [])
                if tool_calls:
                    for j, tc in enumerate(tool_calls):
                        if isinstance(tc, dict):
                            if "id" in tc:
                                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.{ToolCallAttributes.TOOL_CALL_ID}", tc["id"]
                            if "type" in tc:
                                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.tool_call.type", tc["type"]
                            if "function" in tc:
                                func = tc["function"]
                                if "name" in func:
                                    yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.{ToolCallAttributes.TOOL_CALL_FUNCTION_NAME}", func["name"]
                                if "arguments" in func:
                                    yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_TOOL_CALLS}.{j}.{ToolCallAttributes.TOOL_CALL_FUNCTION_ARGUMENTS_JSON}", func["arguments"]

            # Finish reason
            finish_reason = choice.get("finish_reason", "")
            if finish_reason:
                yield f"together.choices.{i}.finish_reason", finish_reason

        # Raw response
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)


class _CompletionsResponseAttributesExtractor:
    """Extract span attributes from Together AI completions response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Model name from response
        if "model" in response:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, response["model"]

        # Token counts from usage
        usage = response.get("usage", {})
        if usage:
            if "prompt_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage["prompt_tokens"]
            if "completion_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage["completion_tokens"]
            if "total_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage["total_tokens"]
            elif "prompt_tokens" in usage and "completion_tokens" in usage:
                total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                if total:
                    yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, total

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        # Extract choices
        choices = response.get("choices", [])
        for i, choice in enumerate(choices):
            text = choice.get("text", "")
            if text:
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", "assistant"
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", text
                if i == 0:
                    yield SpanAttributes.OUTPUT_VALUE, text

            # Finish reason
            finish_reason = choice.get("finish_reason", "")
            if finish_reason:
                yield f"together.choices.{i}.finish_reason", finish_reason

        # Raw response
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)


class _EmbeddingsResponseAttributesExtractor:
    """Extract span attributes from Together AI embeddings response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Model name from response
        if "model" in response:
            yield SpanAttributes.EMBEDDING_MODEL_NAME, response["model"]

        # Token counts from usage
        usage = response.get("usage", {})
        if usage:
            if "prompt_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage["prompt_tokens"]
            if "total_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage["total_tokens"]

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes."""
        if response is None:
            return

        # Embeddings data
        data = response.get("data", [])
        if data:
            yield "together.embeddings_count", len(data)

            for i, embedding_obj in enumerate(data):
                if isinstance(embedding_obj, dict):
                    embedding = embedding_obj.get("embedding", [])
                    if embedding and isinstance(embedding, list):
                        if i == 0:
                            yield "together.embedding_dimensions", len(embedding)
                        # Store first 10 dimensions as sample
                        yield f"{SpanAttributes.EMBEDDING_EMBEDDINGS}.{i}.{EmbeddingAttributes.EMBEDDING_VECTOR}", safe_json_dumps(embedding[:10])

        yield SpanAttributes.OUTPUT_VALUE, f"[{len(data)} embeddings]"
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps({"embeddings_count": len(data), "model": response.get("model", "")})


class _StreamingChatResponseAccumulator:
    """Accumulates streaming chat response chunks."""

    def __init__(self) -> None:
        self._content_parts: List[str] = []
        self._tool_calls: Dict[int, Dict[str, Any]] = {}
        self._model: Optional[str] = None
        self._finish_reason: Optional[str] = None
        self._usage: Dict[str, int] = {}
        self._raw_chunks: List[Dict[str, Any]] = []

    def process_chunk(self, chunk: Any) -> None:
        """Process a streaming chunk."""
        chunk_dict = {}
        if hasattr(chunk, "model_dump"):
            chunk_dict = chunk.model_dump()
        elif hasattr(chunk, "dict"):
            chunk_dict = chunk.dict()
        elif hasattr(chunk, "to_dict"):
            chunk_dict = chunk.to_dict()
        elif isinstance(chunk, dict):
            chunk_dict = chunk

        self._raw_chunks.append(chunk_dict)

        if "model" in chunk_dict:
            self._model = chunk_dict["model"]

        if "usage" in chunk_dict and chunk_dict["usage"]:
            self._usage = chunk_dict["usage"]

        choices = chunk_dict.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})

            # Accumulate content
            if "content" in delta and delta["content"]:
                self._content_parts.append(delta["content"])

            # Accumulate tool calls
            if "tool_calls" in delta and delta["tool_calls"]:
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    if idx not in self._tool_calls:
                        self._tool_calls[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}

                    if "id" in tc and tc["id"]:
                        self._tool_calls[idx]["id"] = tc["id"]
                    if "type" in tc:
                        self._tool_calls[idx]["type"] = tc["type"]
                    if "function" in tc:
                        func = tc["function"]
                        if "name" in func and func["name"]:
                            self._tool_calls[idx]["function"]["name"] += func["name"]
                        if "arguments" in func and func["arguments"]:
                            self._tool_calls[idx]["function"]["arguments"] += func["arguments"]

            # Capture finish reason
            if "finish_reason" in choice and choice["finish_reason"]:
                self._finish_reason = choice["finish_reason"]

    def get_accumulated_response(self) -> Dict[str, Any]:
        """Get the accumulated response as a dict."""
        content = "".join(self._content_parts)

        message: Dict[str, Any] = {
            "role": "assistant",
            "content": content,
        }

        if self._tool_calls:
            message["tool_calls"] = [self._tool_calls[i] for i in sorted(self._tool_calls.keys())]

        response = {
            "model": self._model,
            "choices": [{
                "message": message,
                "finish_reason": self._finish_reason,
            }],
            "usage": self._usage,
        }

        return response

    def get_content(self) -> str:
        """Get accumulated content."""
        return "".join(self._content_parts)

    def get_raw_chunks(self) -> List[Dict[str, Any]]:
        """Get raw chunks (limited)."""
        return self._raw_chunks[-5:]  # Last 5 chunks


class _StreamingCompletionsResponseAccumulator:
    """Accumulates streaming completions response chunks."""

    def __init__(self) -> None:
        self._text_parts: List[str] = []
        self._model: Optional[str] = None
        self._finish_reason: Optional[str] = None
        self._usage: Dict[str, int] = {}
        self._raw_chunks: List[Dict[str, Any]] = []

    def process_chunk(self, chunk: Any) -> None:
        """Process a streaming chunk."""
        chunk_dict = {}
        if hasattr(chunk, "model_dump"):
            chunk_dict = chunk.model_dump()
        elif hasattr(chunk, "dict"):
            chunk_dict = chunk.dict()
        elif hasattr(chunk, "to_dict"):
            chunk_dict = chunk.to_dict()
        elif isinstance(chunk, dict):
            chunk_dict = chunk

        self._raw_chunks.append(chunk_dict)

        if "model" in chunk_dict:
            self._model = chunk_dict["model"]

        if "usage" in chunk_dict and chunk_dict["usage"]:
            self._usage = chunk_dict["usage"]

        choices = chunk_dict.get("choices", [])
        for choice in choices:
            # For completions, text is directly in choice
            if "text" in choice and choice["text"]:
                self._text_parts.append(choice["text"])

            # Capture finish reason
            if "finish_reason" in choice and choice["finish_reason"]:
                self._finish_reason = choice["finish_reason"]

    def get_accumulated_response(self) -> Dict[str, Any]:
        """Get the accumulated response as a dict."""
        text = "".join(self._text_parts)

        response = {
            "model": self._model,
            "choices": [{
                "text": text,
                "finish_reason": self._finish_reason,
            }],
            "usage": self._usage,
        }

        return response

    def get_text(self) -> str:
        """Get accumulated text."""
        return "".join(self._text_parts)

    def get_raw_chunks(self) -> List[Dict[str, Any]]:
        """Get raw chunks (limited)."""
        return self._raw_chunks[-5:]  # Last 5 chunks
