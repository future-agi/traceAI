"""Client wrapper for Claude Agent SDK instrumentation.

This module provides the patching mechanism for ClaudeSDKClient to
automatically trace all conversations, turns, and tool executions.
"""

import json
import logging
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from opentelemetry import trace as trace_api
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from ._attributes import ClaudeAgentAttributes, ClaudeAgentSpanKind
from ._hooks import (
    create_pre_tool_use_hook,
    create_post_tool_use_hook,
    set_parent_span,
    clear_parent_span,
    set_mcp_servers,
    clear_active_tool_spans,
    safe_json_serialize,
)

logger = logging.getLogger(__name__)

# Span names
CONVERSATION_SPAN_NAME = "claude_agent.conversation"
ASSISTANT_TURN_SPAN_NAME = "claude_agent.assistant_turn"


class TurnTracker:
    """Track assistant turns within a conversation."""

    def __init__(
        self,
        tracer: trace_api.Tracer,
        parent_span: Span,
        query_start_time: Optional[float] = None,
    ):
        self.tracer = tracer
        self.parent_span = parent_span
        self.current_turn_span: Optional[Span] = None
        self.turn_count = 0
        self.next_start_time: Optional[float] = query_start_time

    def start_turn(self, message: Any) -> Optional[Span]:
        """Start a new assistant turn span.

        Args:
            message: AssistantMessage from the SDK

        Returns:
            The created span or None
        """
        # End previous turn if exists
        self.end_current_turn()

        self.turn_count += 1
        start_time = self.next_start_time or time.time()

        # Create turn span as child of conversation
        context = trace_api.set_span_in_context(self.parent_span)
        span = self.tracer.start_span(
            name=ASSISTANT_TURN_SPAN_NAME,
            kind=SpanKind.INTERNAL,
            context=context,
        )

        # Set attributes
        span.set_attribute(
            ClaudeAgentAttributes.SPAN_KIND,
            ClaudeAgentSpanKind.ASSISTANT_TURN.value,
        )
        span.set_attribute(ClaudeAgentAttributes.AGENT_NUM_TURNS, self.turn_count)

        # Extract model from message
        model = getattr(message, "model", None)
        if model:
            span.set_attribute(ClaudeAgentAttributes.AGENT_MODEL, model)

        # Extract content
        content = getattr(message, "content", None)
        if content:
            content_str = self._flatten_content(content)
            span.set_attribute(
                ClaudeAgentAttributes.MESSAGE_CONTENT,
                content_str[:2000] if content_str else "",
            )

            # Check for tool uses
            tool_use_count = sum(
                1 for block in content if type(block).__name__ == "ToolUseBlock"
            )
            span.set_attribute(
                ClaudeAgentAttributes.MESSAGE_HAS_TOOL_USE,
                tool_use_count > 0,
            )
            span.set_attribute(
                ClaudeAgentAttributes.MESSAGE_TOOL_USE_COUNT,
                tool_use_count,
            )

        self.current_turn_span = span
        self.next_start_time = None

        return span

    def end_current_turn(self, usage: Optional[Dict[str, Any]] = None) -> None:
        """End the current turn span.

        Args:
            usage: Optional usage metrics to attach
        """
        if not self.current_turn_span:
            return

        if usage:
            if "input_tokens" in usage:
                self.current_turn_span.set_attribute(
                    ClaudeAgentAttributes.USAGE_INPUT_TOKENS,
                    usage["input_tokens"],
                )
            if "output_tokens" in usage:
                self.current_turn_span.set_attribute(
                    ClaudeAgentAttributes.USAGE_OUTPUT_TOKENS,
                    usage["output_tokens"],
                )
            if "cache_read_input_tokens" in usage:
                self.current_turn_span.set_attribute(
                    ClaudeAgentAttributes.USAGE_CACHE_READ_TOKENS,
                    usage["cache_read_input_tokens"],
                )
            if "cache_creation_input_tokens" in usage:
                self.current_turn_span.set_attribute(
                    ClaudeAgentAttributes.USAGE_CACHE_CREATION_TOKENS,
                    usage["cache_creation_input_tokens"],
                )

        self.current_turn_span.set_status(Status(StatusCode.OK))
        self.current_turn_span.end()
        self.current_turn_span = None

    def mark_next_start(self) -> None:
        """Mark the start time for the next turn."""
        self.next_start_time = time.time()

    def _flatten_content(self, content: Any) -> str:
        """Flatten content blocks to string."""
        if isinstance(content, str):
            return content

        if isinstance(content, list):
            parts = []
            for block in content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                elif type(block).__name__ == "ToolUseBlock":
                    parts.append(f"[Tool: {getattr(block, 'name', 'unknown')}]")
            return " ".join(parts)

        return str(content)


def inject_tracing_hooks(options: Any, tracer: trace_api.Tracer) -> None:
    """Inject OpenTelemetry tracing hooks into ClaudeAgentOptions.

    Args:
        options: ClaudeAgentOptions instance
        tracer: OpenTelemetry tracer
    """
    if not hasattr(options, "hooks"):
        return

    # Initialize hooks dict if needed
    if options.hooks is None:
        options.hooks = {}

    # Add PreToolUse hook
    if "PreToolUse" not in options.hooks:
        options.hooks["PreToolUse"] = []

    # Add PostToolUse hook
    if "PostToolUse" not in options.hooks:
        options.hooks["PostToolUse"] = []

    try:
        from claude_agent_sdk import HookMatcher

        pre_hook = create_pre_tool_use_hook(tracer)
        post_hook = create_post_tool_use_hook(tracer)

        # Insert at beginning to ensure we trace before user hooks
        pre_matcher = HookMatcher(matcher=None, hooks=[pre_hook])
        post_matcher = HookMatcher(matcher=None, hooks=[post_hook])

        options.hooks["PreToolUse"].insert(0, pre_matcher)
        options.hooks["PostToolUse"].insert(0, post_matcher)

        logger.debug("Injected OpenTelemetry tracing hooks")
    except ImportError:
        logger.warning("Failed to import HookMatcher from claude_agent_sdk")
    except Exception as e:
        logger.warning(f"Failed to inject tracing hooks: {e}")


def wrap_claude_sdk_client(
    original_class: Any, tracer: trace_api.Tracer
) -> Any:
    """Wrap ClaudeSDKClient with tracing.

    Args:
        original_class: The original ClaudeSDKClient class
        tracer: OpenTelemetry tracer

    Returns:
        Wrapped class with tracing
    """

    class TracedClaudeSDKClient(original_class):
        """ClaudeSDKClient with OpenTelemetry tracing."""

        def __init__(self, *args: Any, **kwargs: Any):
            # Extract options for hook injection
            options = kwargs.get("options") or (args[0] if args else None)
            if options:
                inject_tracing_hooks(options, tracer)

                # Store MCP server configs for tool attribution
                mcp_servers = getattr(options, "mcp_servers", None)
                if mcp_servers:
                    set_mcp_servers(mcp_servers)

            super().__init__(*args, **kwargs)

            # Store for tracing
            self._tracer = tracer
            self._prompt: Optional[str] = None
            self._start_time: Optional[float] = None
            self._options = options

        async def query(self, *args: Any, **kwargs: Any) -> Any:
            """Capture prompt and timestamp when query starts."""
            self._start_time = time.time()
            self._prompt = str(kwargs.get("prompt") or (args[0] if args else ""))
            return await super().query(*args, **kwargs)

        async def receive_response(self) -> AsyncGenerator[Any, None]:
            """Intercept message stream and trace conversation."""
            messages = super().receive_response()

            # Build span attributes from options
            span_attributes = self._build_span_attributes()

            # Create conversation span
            with self._tracer.start_as_current_span(
                CONVERSATION_SPAN_NAME,
                kind=SpanKind.CLIENT,
                attributes=span_attributes,
            ) as conversation_span:
                set_parent_span(conversation_span)

                # Initialize turn tracker
                turn_tracker = TurnTracker(
                    self._tracer,
                    conversation_span,
                    self._start_time,
                )

                collected_messages: List[Dict[str, Any]] = []
                result_usage: Optional[Dict[str, Any]] = None

                try:
                    async for msg in messages:
                        msg_type = type(msg).__name__

                        if msg_type == "AssistantMessage":
                            # Start new turn
                            turn_tracker.start_turn(msg)

                            # Collect for output
                            content = self._extract_content(msg)
                            if content:
                                collected_messages.append({
                                    "role": "assistant",
                                    "content": content,
                                })

                        elif msg_type == "UserMessage":
                            # End current turn, mark next start
                            turn_tracker.end_current_turn()
                            turn_tracker.mark_next_start()

                            # Collect for output
                            content = self._extract_content(msg)
                            if content:
                                collected_messages.append({
                                    "role": "user",
                                    "content": content,
                                })

                        elif msg_type == "ResultMessage":
                            # Extract usage and metrics
                            result_usage = self._extract_usage(msg)

                            # Set conversation-level attributes
                            self._set_result_attributes(conversation_span, msg)

                        elif msg_type == "SystemMessage":
                            # Capture session initialization
                            subtype = getattr(msg, "subtype", None)
                            if subtype == "init":
                                session_id = getattr(msg, "session_id", None)
                                if session_id:
                                    conversation_span.set_attribute(
                                        ClaudeAgentAttributes.AGENT_SESSION_ID,
                                        session_id,
                                    )

                        yield msg

                    # End final turn
                    turn_tracker.end_current_turn(result_usage)

                    # Set final output
                    if collected_messages:
                        conversation_span.set_attribute(
                            "output",
                            safe_json_serialize(collected_messages[-1]),
                        )

                    conversation_span.set_status(Status(StatusCode.OK))

                except Exception as e:
                    conversation_span.set_status(
                        Status(StatusCode.ERROR, str(e))
                    )
                    conversation_span.record_exception(e)
                    raise

                finally:
                    turn_tracker.end_current_turn()
                    clear_parent_span()
                    clear_active_tool_spans()

        def _build_span_attributes(self) -> Dict[str, Any]:
            """Build span attributes from options and prompt."""
            attrs = {
                ClaudeAgentAttributes.SPAN_KIND: ClaudeAgentSpanKind.CONVERSATION.value,
            }

            if self._prompt:
                attrs[ClaudeAgentAttributes.AGENT_PROMPT] = self._prompt[:2000]

            if self._options:
                # Model
                model = getattr(self._options, "model", None)
                if model:
                    attrs[ClaudeAgentAttributes.AGENT_MODEL] = model

                # Permission mode
                perm_mode = getattr(self._options, "permission_mode", None)
                if perm_mode:
                    attrs[ClaudeAgentAttributes.AGENT_PERMISSION_MODE] = perm_mode

                # Allowed tools
                allowed_tools = getattr(self._options, "allowed_tools", None)
                if allowed_tools:
                    attrs[ClaudeAgentAttributes.AGENT_ALLOWED_TOOLS] = json.dumps(
                        allowed_tools
                    )

                # System prompt
                system_prompt = getattr(self._options, "system_prompt", None)
                if system_prompt:
                    if isinstance(system_prompt, str):
                        attrs[ClaudeAgentAttributes.AGENT_SYSTEM_PROMPT] = (
                            system_prompt[:1000]
                        )
                    elif isinstance(system_prompt, dict):
                        attrs[ClaudeAgentAttributes.AGENT_SYSTEM_PROMPT] = json.dumps(
                            system_prompt
                        )[:1000]

                # Resume session
                resume = getattr(self._options, "resume", None)
                if resume:
                    attrs[ClaudeAgentAttributes.AGENT_IS_RESUMED] = True
                    attrs[ClaudeAgentAttributes.AGENT_RESUME_SESSION_ID] = resume

            return attrs

        def _extract_content(self, msg: Any) -> Optional[str]:
            """Extract content from a message."""
            content = getattr(msg, "content", None)
            if not content:
                return None

            if isinstance(content, str):
                return content

            if isinstance(content, list):
                parts = []
                for block in content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                    elif type(block).__name__ == "ToolResultBlock":
                        result = getattr(block, "content", "")
                        parts.append(f"[Tool Result: {result[:100]}...]")
                return " ".join(parts) if parts else None

            return str(content)

        def _extract_usage(self, msg: Any) -> Optional[Dict[str, Any]]:
            """Extract usage metrics from ResultMessage."""
            usage = getattr(msg, "usage", None)
            if not usage:
                return None

            return {
                "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0),
                "cache_read_input_tokens": getattr(
                    usage, "cache_read_input_tokens", 0
                ),
                "cache_creation_input_tokens": getattr(
                    usage, "cache_creation_input_tokens", 0
                ),
            }

        def _set_result_attributes(self, span: Span, msg: Any) -> None:
            """Set conversation result attributes."""
            # Usage
            usage = self._extract_usage(msg)
            if usage:
                span.set_attribute(
                    ClaudeAgentAttributes.USAGE_INPUT_TOKENS,
                    usage.get("input_tokens", 0),
                )
                span.set_attribute(
                    ClaudeAgentAttributes.USAGE_OUTPUT_TOKENS,
                    usage.get("output_tokens", 0),
                )

                total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                span.set_attribute(ClaudeAgentAttributes.USAGE_TOTAL_TOKENS, total)

            # Cost
            total_cost = getattr(msg, "total_cost_usd", None)
            if total_cost is not None:
                span.set_attribute(ClaudeAgentAttributes.COST_TOTAL_USD, total_cost)

            # Performance
            duration_ms = getattr(msg, "duration_ms", None)
            if duration_ms is not None:
                span.set_attribute(ClaudeAgentAttributes.DURATION_MS, duration_ms)

            duration_api_ms = getattr(msg, "duration_api_ms", None)
            if duration_api_ms is not None:
                span.set_attribute(ClaudeAgentAttributes.DURATION_API_MS, duration_api_ms)

            # Session
            session_id = getattr(msg, "session_id", None)
            if session_id:
                span.set_attribute(ClaudeAgentAttributes.AGENT_SESSION_ID, session_id)

            # Turns
            num_turns = getattr(msg, "num_turns", None)
            if num_turns is not None:
                span.set_attribute(ClaudeAgentAttributes.AGENT_NUM_TURNS, num_turns)

            # Error
            is_error = getattr(msg, "is_error", False)
            span.set_attribute(ClaudeAgentAttributes.IS_ERROR, is_error)

        async def __aenter__(self) -> "TracedClaudeSDKClient":
            await super().__aenter__()
            return self

        async def __aexit__(self, *args: Any) -> None:
            await super().__aexit__(*args)

    return TracedClaudeSDKClient
