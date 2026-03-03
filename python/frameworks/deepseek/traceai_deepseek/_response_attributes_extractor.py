import logging
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from fi_instrumentation import safe_json_dumps
from fi_instrumentation.fi_types import (
    MessageAttributes,
    SpanAttributes,
)
from opentelemetry.util.types import AttributeValue

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _ChatCompletionResponseAttributesExtractor:
    """Extract span attributes from DeepSeek chat completion response."""

    def get_attributes(
        self,
        response: Optional[Dict[str, Any]],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract primary attributes from response."""
        if response is None:
            return

        # Token usage
        usage = response.get("usage", {})
        if usage:
            if "prompt_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, usage["prompt_tokens"]
            if "completion_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, usage["completion_tokens"]
            if "total_tokens" in usage:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, usage["total_tokens"]

            # DeepSeek-specific: prompt cache tokens
            if "prompt_cache_hit_tokens" in usage:
                yield "deepseek.prompt_cache_hit_tokens", usage["prompt_cache_hit_tokens"]
            if "prompt_cache_miss_tokens" in usage:
                yield "deepseek.prompt_cache_miss_tokens", usage["prompt_cache_miss_tokens"]

            # DeepSeek R1 specific: reasoning tokens
            if "completion_tokens_details" in usage:
                details = usage["completion_tokens_details"]
                if "reasoning_tokens" in details:
                    yield "deepseek.reasoning_tokens", details["reasoning_tokens"]

        # Model info
        if "model" in response:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, response["model"]

        # Response ID
        if "id" in response:
            yield "deepseek.response_id", response["id"]

    def get_extra_attributes(
        self,
        response: Optional[Dict[str, Any]],
        request_parameters: Mapping[str, Any],
        is_streaming: bool = False,
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Extract extra attributes including output."""
        if response is None:
            return

        choices = response.get("choices", [])

        for i, choice in enumerate(choices):
            message = choice.get("message", {})

            # Role
            role = message.get("role", "assistant")
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_ROLE}", role

            # Content
            content = message.get("content", "")
            if content:
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.{MessageAttributes.MESSAGE_CONTENT}", content

            # DeepSeek R1 specific: reasoning_content
            reasoning_content = message.get("reasoning_content", "")
            if reasoning_content:
                yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.{i}.reasoning_content", reasoning_content
                yield "deepseek.reasoning_content", reasoning_content

            # Tool calls
            tool_calls = message.get("tool_calls", [])
            if tool_calls:
                yield f"deepseek.tool_calls_count", len(tool_calls)
                for j, tc in enumerate(tool_calls[:10]):  # Limit
                    yield f"deepseek.tool_calls.{j}.id", tc.get("id", "")
                    yield f"deepseek.tool_calls.{j}.type", tc.get("type", "function")
                    if "function" in tc:
                        yield f"deepseek.tool_calls.{j}.function.name", tc["function"].get("name", "")
                        yield f"deepseek.tool_calls.{j}.function.arguments", tc["function"].get("arguments", "")

            # Finish reason
            finish_reason = choice.get("finish_reason", "")
            if finish_reason:
                yield f"deepseek.finish_reason", finish_reason

            # Logprobs
            if "logprobs" in choice and choice["logprobs"]:
                yield f"deepseek.has_logprobs", True

        # Primary output
        if choices:
            first_choice = choices[0]
            message = first_choice.get("message", {})
            content = message.get("content", "")
            if content:
                yield SpanAttributes.OUTPUT_VALUE, content

            # If R1 model, also capture reasoning
            reasoning = message.get("reasoning_content", "")
            if reasoning:
                yield "deepseek.output_reasoning", reasoning

        # Raw output
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(response)


class _StreamingChatCompletionResponseExtractor:
    """Extract attributes from streaming DeepSeek response chunks."""

    def __init__(self):
        self._content_parts = []
        self._reasoning_parts = []
        self._tool_calls = {}
        self._finish_reason = None
        self._usage = {}
        self._model = None
        self._response_id = None

    def process_chunk(self, chunk: Dict[str, Any]) -> None:
        """Process a single streaming chunk."""
        if "id" in chunk and not self._response_id:
            self._response_id = chunk["id"]

        if "model" in chunk and not self._model:
            self._model = chunk["model"]

        # Handle usage in final chunk
        if "usage" in chunk:
            self._usage = chunk["usage"]

        choices = chunk.get("choices", [])
        for choice in choices:
            delta = choice.get("delta", {})

            # Content
            if "content" in delta and delta["content"]:
                self._content_parts.append(delta["content"])

            # Reasoning content (DeepSeek R1)
            if "reasoning_content" in delta and delta["reasoning_content"]:
                self._reasoning_parts.append(delta["reasoning_content"])

            # Tool calls
            if "tool_calls" in delta:
                for tc in delta["tool_calls"]:
                    idx = tc.get("index", 0)
                    if idx not in self._tool_calls:
                        self._tool_calls[idx] = {
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {"name": "", "arguments": ""}
                        }
                    if "id" in tc and tc["id"]:
                        self._tool_calls[idx]["id"] = tc["id"]
                    if "function" in tc:
                        if "name" in tc["function"] and tc["function"]["name"]:
                            self._tool_calls[idx]["function"]["name"] = tc["function"]["name"]
                        if "arguments" in tc["function"]:
                            self._tool_calls[idx]["function"]["arguments"] += tc["function"]["arguments"]

            # Finish reason
            if "finish_reason" in choice and choice["finish_reason"]:
                self._finish_reason = choice["finish_reason"]

    def get_attributes(self) -> Iterator[Tuple[str, AttributeValue]]:
        """Get attributes from accumulated stream data."""
        # Token usage
        if self._usage:
            if "prompt_tokens" in self._usage:
                yield SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS, self._usage["prompt_tokens"]
            if "completion_tokens" in self._usage:
                yield SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS, self._usage["completion_tokens"]
            if "total_tokens" in self._usage:
                yield SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS, self._usage["total_tokens"]

            # DeepSeek-specific cache tokens
            if "prompt_cache_hit_tokens" in self._usage:
                yield "deepseek.prompt_cache_hit_tokens", self._usage["prompt_cache_hit_tokens"]
            if "prompt_cache_miss_tokens" in self._usage:
                yield "deepseek.prompt_cache_miss_tokens", self._usage["prompt_cache_miss_tokens"]

            # Reasoning tokens
            if "completion_tokens_details" in self._usage:
                details = self._usage["completion_tokens_details"]
                if "reasoning_tokens" in details:
                    yield "deepseek.reasoning_tokens", details["reasoning_tokens"]

        if self._model:
            yield SpanAttributes.GEN_AI_REQUEST_MODEL, self._model

        if self._response_id:
            yield "deepseek.response_id", self._response_id

    def get_extra_attributes(
        self, request_parameters: Mapping[str, Any]
    ) -> Iterator[Tuple[str, AttributeValue]]:
        """Get extra attributes from accumulated stream."""
        content = "".join(self._content_parts)
        reasoning = "".join(self._reasoning_parts)

        # Output message
        yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_ROLE}", "assistant"
        if content:
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.{MessageAttributes.MESSAGE_CONTENT}", content
            yield SpanAttributes.OUTPUT_VALUE, content

        # Reasoning content (DeepSeek R1)
        if reasoning:
            yield f"{SpanAttributes.GEN_AI_OUTPUT_MESSAGES}.0.reasoning_content", reasoning
            yield "deepseek.reasoning_content", reasoning
            yield "deepseek.output_reasoning", reasoning

        # Tool calls
        if self._tool_calls:
            yield "deepseek.tool_calls_count", len(self._tool_calls)
            for idx, tc in self._tool_calls.items():
                yield f"deepseek.tool_calls.{idx}.id", tc["id"]
                yield f"deepseek.tool_calls.{idx}.type", tc["type"]
                yield f"deepseek.tool_calls.{idx}.function.name", tc["function"]["name"]
                yield f"deepseek.tool_calls.{idx}.function.arguments", tc["function"]["arguments"]

        # Finish reason
        if self._finish_reason:
            yield "deepseek.finish_reason", self._finish_reason

        # Raw output summary
        output_summary = {
            "content": content[:500] if len(content) > 500 else content,
            "reasoning_content": reasoning[:500] if len(reasoning) > 500 else reasoning,
            "tool_calls": list(self._tool_calls.values()),
            "finish_reason": self._finish_reason
        }
        yield SpanAttributes.OUTPUT_VALUE, safe_json_dumps(output_summary)
