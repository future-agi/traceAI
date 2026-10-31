"""Use native spans, adding only the missing metadata signal for tool errors."""

from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING, Any

from agentscope.message import ToolCallBlock, ToolResultBlock, ToolResultState
from agentscope.middleware import TracingMiddleware as NativeTracingMiddleware
from agentscope.tool import ToolChunk, ToolResponse
from opentelemetry import trace

from ._projection import TOOL_ERROR

if TYPE_CHECKING:
    from agentscope.agent import Agent

_ToolResult = ToolResultBlock | ToolChunk | ToolResponse


class TracingMiddleware(NativeTracingMiddleware):
    """Native AgentScope middleware with returned tool errors marked on its span.

    No new spans and no model SDK instrumentation. Use one instance in place of
    (not alongside) AgentScope's TracingMiddleware.
    """

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[_ToolResult, None]],
    ) -> AsyncGenerator[_ToolResult, None]:
        async def observe_result(**kwargs):
            gen = next_handler(**kwargs)
            try:
                async for item in gen:
                    if (
                        isinstance(input_kwargs.get("tool_call"), ToolCallBlock)
                        and isinstance(item, (ToolResultBlock, ToolChunk, ToolResponse))
                        and item.state == ToolResultState.ERROR
                    ):
                        # Native middleware advances this iterator with its tool span current.
                        # Do not mark an unrelated parent when native tracing short-circuits.
                        span = trace.get_current_span()
                        scope = getattr(span, "instrumentation_scope", None)
                        if (
                            scope is not None
                            and scope.name == "agentscope"
                            and (getattr(span, "attributes", None) or {}).get(
                                "gen_ai.operation.name"
                            )
                            == "execute_tool"
                        ):
                            span.set_attribute(TOOL_ERROR, True)
                    yield item
            finally:
                await gen.aclose()

        gen = super().on_acting(agent, input_kwargs, observe_result)
        try:
            async for item in gen:
                yield item
        finally:
            await gen.aclose()
