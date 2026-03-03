"""Claude Agent SDK Instrumentor.

Main instrumentor class that provides OpenTelemetry tracing for the
Claude Agent SDK (formerly Claude Code SDK).
"""

import logging
import sys
from typing import Any, Collection, Optional

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from ._attributes import ClaudeAgentAttributes, ClaudeAgentSpanKind
from ._client_wrapper import wrap_claude_sdk_client

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("claude-agent-sdk >= 0.1.0",)


class ClaudeAgentInstrumentor(BaseInstrumentor):
    """OpenTelemetry Instrumentor for Claude Agent SDK.

    This instrumentor provides comprehensive tracing for Claude Agent SDK
    including:
    - Conversation/query execution
    - Assistant turns with content
    - Tool executions (built-in, MCP, custom)
    - Subagent coordination
    - Session continuity
    - Usage and cost tracking

    Usage:
        from traceai_claude_agent_sdk import ClaudeAgentInstrumentor

        # Initialize with tracer provider
        ClaudeAgentInstrumentor().instrument(tracer_provider=provider)

        # Or use default provider
        ClaudeAgentInstrumentor().instrument()

        # Use Claude Agent SDK as normal - traces are automatic
        import asyncio
        from claude_agent_sdk import query, ClaudeAgentOptions

        async def main():
            async for message in query(
                prompt="What files are in this directory?",
                options=ClaudeAgentOptions(allowed_tools=["Glob", "Read"])
            ):
                print(message)

        asyncio.run(main())
    """

    _instance: Optional["ClaudeAgentInstrumentor"] = None
    _is_instrumented: bool = False

    def __new__(cls) -> "ClaudeAgentInstrumentor":
        """Singleton pattern to ensure single instrumentation."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the instrumentor."""
        super().__init__()
        self._tracer: Optional[trace_api.Tracer] = None
        self._original_client_class = None

    def instrumentation_dependencies(self) -> Collection[str]:
        """Return the instrumentation dependencies."""
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        """Instrument the Claude Agent SDK.

        Args:
            **kwargs: Configuration options
                - tracer_provider: OpenTelemetry tracer provider
        """
        if self._is_instrumented:
            logger.warning("Claude Agent SDK is already instrumented")
            return

        # Get tracer provider
        tracer_provider = kwargs.get("tracer_provider")
        if not tracer_provider:
            tracer_provider = trace_api.get_tracer_provider()

        # Create tracer
        self._tracer = trace_api.get_tracer(
            __name__,
            "0.1.0",
            tracer_provider,
        )

        # Try to import and patch Claude Agent SDK
        try:
            self._patch_claude_agent_sdk()
            self._is_instrumented = True
            logger.info("Claude Agent SDK instrumentation enabled")
        except ImportError as e:
            logger.warning(
                f"Claude Agent SDK not installed, instrumentation skipped: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to instrument Claude Agent SDK: {e}")
            raise

    def _patch_claude_agent_sdk(self) -> None:
        """Patch Claude Agent SDK classes and methods."""
        try:
            import claude_agent_sdk
        except ImportError:
            raise ImportError("Could not import claude_agent_sdk")

        # Check for ClaudeSDKClient
        if not hasattr(claude_agent_sdk, "ClaudeSDKClient"):
            logger.warning("Claude Agent SDK missing ClaudeSDKClient")
            return

        # Store original class
        self._original_client_class = claude_agent_sdk.ClaudeSDKClient

        # Create wrapped class
        wrapped_class = wrap_claude_sdk_client(
            self._original_client_class,
            self._tracer,
        )

        # Replace in module
        claude_agent_sdk.ClaudeSDKClient = wrapped_class

        # Also patch in any modules that have already imported it
        for module in list(sys.modules.values()):
            try:
                if module and getattr(module, "ClaudeSDKClient", None) is self._original_client_class:
                    setattr(module, "ClaudeSDKClient", wrapped_class)
            except Exception:
                continue

        logger.debug("Patched ClaudeSDKClient")

    def _uninstrument(self, **kwargs: Any) -> None:
        """Remove Claude Agent SDK instrumentation."""
        if not self._is_instrumented:
            return

        try:
            import claude_agent_sdk

            # Restore original class
            if self._original_client_class:
                claude_agent_sdk.ClaudeSDKClient = self._original_client_class

                # Restore in other modules
                for module in list(sys.modules.values()):
                    try:
                        if module and hasattr(module, "ClaudeSDKClient"):
                            setattr(module, "ClaudeSDKClient", self._original_client_class)
                    except Exception:
                        continue

                self._original_client_class = None

        except ImportError:
            pass

        self._is_instrumented = False
        self._tracer = None

        logger.info("Claude Agent SDK instrumentation disabled")

    @property
    def is_instrumented(self) -> bool:
        """Check if Claude Agent SDK is instrumented."""
        return self._is_instrumented


# Convenience function for simple initialization
def instrument_claude_agent_sdk(tracer_provider: Optional[Any] = None) -> ClaudeAgentInstrumentor:
    """Convenience function to instrument Claude Agent SDK.

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider

    Returns:
        The instrumentor instance

    Example:
        from traceai_claude_agent_sdk import instrument_claude_agent_sdk

        instrument_claude_agent_sdk()

        # Now use Claude Agent SDK - tracing is automatic
    """
    instrumentor = ClaudeAgentInstrumentor()

    kwargs = {}
    if tracer_provider:
        kwargs["tracer_provider"] = tracer_provider

    instrumentor.instrument(**kwargs)
    return instrumentor
