"""Pydantic AI Instrumentor.

Main instrumentor class that provides OpenTelemetry tracing for Pydantic AI.
"""

import logging
import sys
from typing import Any, Collection, Optional

from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from ._agent_wrapper import wrap_agent_run, wrap_tool_function
from ._attributes import PydanticAIAttributes, PydanticAISpanKind

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_instruments = ("pydantic-ai >= 0.0.1",)


class PydanticAIInstrumentor(BaseInstrumentor):
    """OpenTelemetry Instrumentor for Pydantic AI.

    This instrumentor provides comprehensive tracing for Pydantic AI including:
    - Agent run execution (run, run_sync, run_stream)
    - Tool function calls
    - Model requests with usage tracking
    - Structured output validation
    - Retry attempts

    Usage:
        from traceai_pydantic_ai import PydanticAIInstrumentor

        # Initialize with tracer provider
        PydanticAIInstrumentor().instrument(tracer_provider=provider)

        # Or use default provider
        PydanticAIInstrumentor().instrument()

        # Use Pydantic AI as normal - traces are automatic
        from pydantic_ai import Agent

        agent = Agent('openai:gpt-4o', instructions='Be helpful.')
        result = agent.run_sync('Hello!')
    """

    _instance: Optional["PydanticAIInstrumentor"] = None
    _is_instrumented: bool = False

    def __new__(cls) -> "PydanticAIInstrumentor":
        """Singleton pattern to ensure single instrumentation."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the instrumentor."""
        super().__init__()
        self._tracer: Optional[trace_api.Tracer] = None
        self._original_methods: dict = {}
        self._use_builtin: bool = False

    def instrumentation_dependencies(self) -> Collection[str]:
        """Return the instrumentation dependencies."""
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        """Instrument Pydantic AI.

        Args:
            **kwargs: Configuration options
                - tracer_provider: OpenTelemetry tracer provider
                - use_builtin: If True, use Pydantic AI's built-in instrumentation
        """
        if self._is_instrumented:
            logger.warning("Pydantic AI is already instrumented")
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

        # Check if we should use built-in instrumentation
        self._use_builtin = kwargs.get("use_builtin", False)

        # Try to import and patch Pydantic AI
        try:
            self._patch_pydantic_ai()
            self._is_instrumented = True
            logger.info("Pydantic AI instrumentation enabled")
        except ImportError as e:
            logger.warning(
                f"Pydantic AI not installed, instrumentation skipped: {e}"
            )
        except Exception as e:
            logger.error(f"Failed to instrument Pydantic AI: {e}")
            raise

    def _patch_pydantic_ai(self) -> None:
        """Patch Pydantic AI classes and methods."""
        try:
            import pydantic_ai
            from pydantic_ai import Agent
        except ImportError:
            raise ImportError("Could not import pydantic_ai")

        if self._use_builtin:
            # Use Pydantic AI's built-in instrumentation
            self._enable_builtin_instrumentation()
            return

        # Store original methods
        self._original_methods = {
            "run": Agent.run,
            "run_sync": Agent.run_sync,
        }

        # Check for run_stream
        if hasattr(Agent, "run_stream"):
            self._original_methods["run_stream"] = Agent.run_stream

        # Patch methods
        Agent.run = wrap_agent_run(Agent.run, self._tracer, "run")
        Agent.run_sync = wrap_agent_run(Agent.run_sync, self._tracer, "run_sync")

        if hasattr(Agent, "run_stream"):
            Agent.run_stream = self._wrap_run_stream(Agent.run_stream)

        # Also patch in any modules that have already imported Agent
        for module in list(sys.modules.values()):
            try:
                if module and getattr(module, "Agent", None) is pydantic_ai.Agent:
                    pass  # Already patched via class
            except Exception:
                continue

        logger.debug("Patched Pydantic AI Agent")

    def _enable_builtin_instrumentation(self) -> None:
        """Enable Pydantic AI's built-in OpenTelemetry instrumentation."""
        try:
            from pydantic_ai import Agent

            # Use Agent.instrument_all() if available
            if hasattr(Agent, "instrument_all"):
                Agent.instrument_all()
                logger.debug("Enabled Pydantic AI built-in instrumentation via Agent.instrument_all()")
            else:
                logger.warning("Agent.instrument_all() not available")
        except Exception as e:
            logger.warning(f"Failed to enable built-in instrumentation: {e}")

    def _wrap_run_stream(self, original_method):
        """Wrap run_stream method for async context manager.

        Args:
            original_method: Original run_stream method

        Returns:
            Wrapped method
        """
        import functools
        import time
        import uuid

        from ._agent_wrapper import (
            extract_model_info,
            safe_serialize,
        )

        tracer = self._tracer

        @functools.wraps(original_method)
        async def wrapper(self, *args, **kwargs):
            from ._attributes import PydanticAIAttributes, PydanticAISpanKind

            run_id = str(uuid.uuid4())

            # Extract prompt
            prompt = ""
            if args:
                prompt = safe_serialize(args[0])
            elif "user_prompt" in kwargs:
                prompt = safe_serialize(kwargs["user_prompt"])

            # Get model info
            model_info = extract_model_info(self)

            attributes = {
                PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.STREAM.value,
                PydanticAIAttributes.RUN_ID: run_id,
                PydanticAIAttributes.RUN_METHOD: "run_stream",
                PydanticAIAttributes.RUN_PROMPT: prompt,
            }

            if model_info.get("model_name"):
                attributes[PydanticAIAttributes.MODEL_NAME] = model_info["model_name"]
            if model_info.get("model_provider"):
                attributes[PydanticAIAttributes.MODEL_PROVIDER] = model_info["model_provider"]

            start_time = time.time()

            # Create span for the entire stream
            span = tracer.start_span(
                "pydantic_ai.agent.run_stream",
                kind=trace_api.SpanKind.CLIENT,
                attributes=attributes,
            )

            try:
                # Call original and return wrapped context manager
                stream = await original_method(self, *args, **kwargs)

                # Wrap the stream to track completion
                return _TracedStream(stream, span, start_time, tracer)

            except Exception as e:
                duration_ms = (time.time() - start_time) * 1000
                span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)
                span.set_attribute(PydanticAIAttributes.IS_ERROR, True)
                span.set_attribute(PydanticAIAttributes.ERROR_MESSAGE, str(e))
                span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(e)))
                span.record_exception(e)
                span.end()
                raise

        return wrapper

    def _uninstrument(self, **kwargs: Any) -> None:
        """Remove Pydantic AI instrumentation."""
        if not self._is_instrumented:
            return

        try:
            from pydantic_ai import Agent

            # Restore original methods
            for method_name, original in self._original_methods.items():
                if hasattr(Agent, method_name):
                    setattr(Agent, method_name, original)

            self._original_methods.clear()

        except ImportError:
            pass

        self._is_instrumented = False
        self._tracer = None

        logger.info("Pydantic AI instrumentation disabled")

    @property
    def is_instrumented(self) -> bool:
        """Check if Pydantic AI is instrumented."""
        return self._is_instrumented


class _TracedStream:
    """Wrapper for streaming responses that tracks completion."""

    def __init__(self, stream, span, start_time, tracer):
        self._stream = stream
        self._span = span
        self._start_time = start_time
        self._tracer = tracer
        self._chunk_count = 0

    async def __aenter__(self):
        if hasattr(self._stream, "__aenter__"):
            self._stream = await self._stream.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        import time

        from ._attributes import PydanticAIAttributes

        duration_ms = (time.time() - self._start_time) * 1000
        self._span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)
        self._span.set_attribute(
            PydanticAIAttributes.STREAM_CHUNK_COUNT,
            self._chunk_count,
        )

        if exc_type:
            self._span.set_attribute(PydanticAIAttributes.IS_ERROR, True)
            self._span.set_attribute(PydanticAIAttributes.ERROR_MESSAGE, str(exc_val))
            self._span.set_status(
                trace_api.Status(trace_api.StatusCode.ERROR, str(exc_val))
            )
            if exc_val:
                self._span.record_exception(exc_val)
        else:
            self._span.set_attribute(PydanticAIAttributes.IS_ERROR, False)
            self._span.set_status(trace_api.Status(trace_api.StatusCode.OK))

        self._span.end()

        if hasattr(self._stream, "__aexit__"):
            await self._stream.__aexit__(exc_type, exc_val, exc_tb)

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self._stream.__anext__()
        self._chunk_count += 1
        return chunk

    # Forward other attributes to underlying stream
    def __getattr__(self, name):
        return getattr(self._stream, name)


# Convenience function for simple initialization
def instrument_pydantic_ai(
    tracer_provider: Optional[Any] = None,
    use_builtin: bool = False,
) -> PydanticAIInstrumentor:
    """Convenience function to instrument Pydantic AI.

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider
        use_builtin: If True, use Pydantic AI's built-in instrumentation

    Returns:
        The instrumentor instance

    Example:
        from traceai_pydantic_ai import instrument_pydantic_ai

        instrument_pydantic_ai()

        # Now use Pydantic AI - tracing is automatic
    """
    instrumentor = PydanticAIInstrumentor()

    kwargs = {}
    if tracer_provider:
        kwargs["tracer_provider"] = tracer_provider
    if use_builtin:
        kwargs["use_builtin"] = use_builtin

    instrumentor.instrument(**kwargs)
    return instrumentor
