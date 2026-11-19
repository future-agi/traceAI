"""Pydantic AI Instrumentor.

Main instrumentor class that provides OpenTelemetry tracing for Pydantic AI.
"""

import functools
import inspect
import logging
import sys
from typing import Any, Collection, Optional

from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from ._agent_wrapper import _record_output_value, wrap_agent_run, wrap_legacy_usage_incr
from ._attributes import PydanticAIAttributes, PydanticAISpanKind
from ._privacy import _PydanticTraceConfig
from ._execution_wrappers import wrap_model_request, wrap_tool_call

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
        """Initialize the singleton once; retain originals for later teardown."""
        if hasattr(self, "_original_methods"):
            return
        super().__init__()
        self._tracer: Optional[trace_api.Tracer] = None
        self._original_methods: dict = {}
        self._execution_patches = []
        self._legacy_usage_original = None
        self._use_builtin: bool = False

    def instrumentation_dependencies(self) -> Collection[str]:
        """Return the instrumentation dependencies."""
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        """Instrument Pydantic AI.

        Args:
            **kwargs: Configuration options
                - tracer_provider: OpenTelemetry tracer provider
                - config: TraceConfig; omitted fields resolve from the environment
                - use_builtin: Deprecated compatibility flag; FI-aware wrappers
                  are used because native instrumentation bypasses TraceConfig
        """
        if self._is_instrumented:
            logger.warning("Pydantic AI is already instrumented")
            return

        # Get tracer provider
        tracer_provider = kwargs.get("tracer_provider")
        if not tracer_provider:
            tracer_provider = trace_api.get_tracer_provider()

        # Resolve environment-backed defaults at instrumentation time, not import
        # time. Apply policy before SDK samplers/processors can observe content.
        config = kwargs.get("config")
        if config is None:
            config = TraceConfig()
        if not isinstance(config, TraceConfig):
            raise TypeError("config must be a TraceConfig")
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, "0.1.0", tracer_provider),
            config=_PydanticTraceConfig(config),
        )

        # Retain the option for compatibility, but never bypass FI's policy.
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
            # Native instrumentation creates its own raw tracers. It cannot
            # enforce this instrumentor's content, error or suppression policy.
            logger.warning(
                "use_builtin is deprecated; using FI-aware Pydantic AI wrappers "
                "to enforce TraceConfig"
            )

        # Before request-local usage lived on ModelResponse, the SDK updated
        # Usage in place. Observe its increments in the logical run's context.
        from pydantic_ai import usage as usage_module
        if not hasattr(usage_module, "RequestUsage"):
            usage_class = usage_module.Usage
            self._legacy_usage_original = (usage_class, usage_class.incr)
            usage_class.incr = wrap_legacy_usage_incr(usage_class.incr)

        # Store original methods
        self._original_methods = {
            "run": Agent.run,
            "run_sync": Agent.run_sync,
        }

        # Check for run_stream
        if hasattr(Agent, "run_stream"):
            self._original_methods["run_stream"] = Agent.run_stream

        # Patch methods
        capture_legacy = self._legacy_usage_original is not None
        Agent.run = wrap_agent_run(Agent.run, self._tracer, "run", capture_legacy)
        Agent.run_sync = wrap_agent_run(Agent.run_sync, self._tracer, "run_sync", capture_legacy)

        if hasattr(Agent, "run_stream"):
            Agent.run_stream = self._wrap_run_stream(Agent.run_stream)

        self._patch_execution_methods()

        # Also patch in any modules that have already imported Agent
        for module in list(sys.modules.values()):
            try:
                if module and getattr(module, "Agent", None) is pydantic_ai.Agent:
                    pass  # Already patched via class
            except Exception:
                continue

        logger.debug("Patched Pydantic AI Agent")

    def _patch_execution_methods(self) -> None:
        """Observe execution, including tools registered before instrument()."""
        try:
            from pydantic_ai import models
        except ImportError:
            # Agent-only implementations can still use the run wrappers.
            return

        # Model.request is abstract: patching only the base implementation does
        # not intercept concrete providers. Cover loaded classes and future
        # provider imports without importing every optional provider ourselves.
        Model = models.Model
        if not isinstance(Model, type):
            return
        seen = set()

        def patch_model(cls):
            if cls in seen:
                return
            seen.add(cls)
            for name in ("request", "request_stream"):
                original = cls.__dict__.get(name)
                if callable(original) and not getattr(original, "__isabstractmethod__", False):
                    self._install_execution_patch(
                        cls, name, wrap_model_request(
                            original, self._tracer, streaming=name == "request_stream"))
            for subclass in cls.__subclasses__():
                patch_model(subclass)

        original_init = Model.__dict__.get("__init_subclass__")

        def init_subclass(cls, **kwargs):
            if original_init is None:
                super(Model, cls).__init_subclass__(**kwargs)
            else:
                original_init.__get__(None, cls)(**kwargs)
            patch_model(cls)

        self._install_execution_patch(Model, "__init_subclass__", classmethod(init_subclass))
        patch_model(Model)

        # Toolsets dispatch both sync and async functions, preserving validation,
        # context injection and executor offloading. Older SDKs use Tool.run,
        # which may return RetryPromptPart rather than raising ModelRetry.
        try:
            from pydantic_ai.toolsets.function import FunctionToolset
        except ImportError:
            from pydantic_ai.tools import Tool
            if isinstance(Tool, type) and hasattr(Tool, "run"):
                self._install_execution_patch(
                    Tool, "run", wrap_tool_call(Tool.run, self._tracer, legacy=True))
        else:
            if isinstance(FunctionToolset, type):
                self._install_execution_patch(
                    FunctionToolset, "call_tool", wrap_tool_call(FunctionToolset.call_tool, self._tracer))

    def _install_execution_patch(self, cls, name, replacement):
        original = cls.__dict__.get(name)
        setattr(cls, name, replacement)
        self._execution_patches.append((cls, name, original, replacement))

    def _restore_execution_methods(self):
        for cls, name, original, replacement in reversed(self._execution_patches):
            if cls.__dict__.get(name) is replacement:
                if original is None:
                    delattr(cls, name)
                else:
                    setattr(cls, name, original)
        self._execution_patches.clear()

    def _wrap_run_stream(self, original_method):
        """Wrap run_stream method for async context manager.

        Args:
            original_method: Original run_stream method

        Returns:
            Wrapped method
        """
        import functools
        import inspect
        import time
        import uuid
        from contextlib import asynccontextmanager

        from ._agent_wrapper import (
            extract_model_info,
            safe_serialize,
        )

        tracer = self._tracer

        @functools.wraps(original_method)
        @asynccontextmanager
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
                PydanticAIAttributes.GEN_AI_SPAN_KIND: "AGENT",
                PydanticAIAttributes.RUN_ID: run_id,
                PydanticAIAttributes.RUN_METHOD: "run_stream",
                PydanticAIAttributes.RUN_PROMPT: prompt,
                PydanticAIAttributes.INPUT_VALUE: prompt,
                PydanticAIAttributes.INPUT_MIME_TYPE: "text/plain",
            }

            if model_info.get("model_name"):
                attributes[PydanticAIAttributes.MODEL_NAME] = model_info["model_name"]
            if model_info.get("model_provider"):
                attributes[PydanticAIAttributes.MODEL_PROVIDER] = model_info["model_provider"]

            start_time = time.time()

            # Keep the span current through entry, consumption, and cleanup.
            traced_stream = None
            with tracer.start_as_current_span(
                "pydantic_ai.agent.run_stream",
                kind=trace_api.SpanKind.CLIENT,
                attributes=attributes,
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                try:
                    # Retain the original manager and its exit/suppression
                    # rules, including factories that return awaitable managers.
                    manager = original_method(self, *args, **kwargs)
                    if inspect.isawaitable(manager):
                        manager = await manager
                    async with manager as stream:
                        traced_stream = _TracedStream(stream)
                        yield traced_stream
                except BaseException as e:
                    # Cancellation must also close the span without changing
                    # the exception delivered by the native context manager.
                    span.set_attribute(PydanticAIAttributes.IS_ERROR, True)
                    span.set_attribute(PydanticAIAttributes.ERROR_MESSAGE, str(e))
                    span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                    raise
                else:
                    span.set_attribute(PydanticAIAttributes.IS_ERROR, False)
                    span.set_status(trace_api.Status(trace_api.StatusCode.OK))
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    span.set_attribute(PydanticAIAttributes.DURATION_MS, duration_ms)
                    span.set_attribute(
                        PydanticAIAttributes.STREAM_CHUNK_COUNT,
                        traced_stream._chunk_count if traced_stream is not None else 0,
                    )
                    if traced_stream is not None:
                        traced_stream._record_final_output(span)

        if not inspect.iscoroutinefunction(original_method):
            return wrapper

        # Retain the awaitable-manager protocol used by older/custom agents.
        # Native factories above never become coroutines.
        @functools.wraps(original_method)
        async def coroutine_wrapper(self, *args, **kwargs):
            prompt = safe_serialize(args[0]) if args else safe_serialize(
                kwargs.get("user_prompt", "")
            )
            attributes = {
                PydanticAIAttributes.SPAN_KIND: PydanticAISpanKind.STREAM.value,
                PydanticAIAttributes.GEN_AI_SPAN_KIND: "AGENT",
                PydanticAIAttributes.RUN_ID: str(uuid.uuid4()),
                PydanticAIAttributes.RUN_METHOD: "run_stream",
                PydanticAIAttributes.RUN_PROMPT: prompt,
                PydanticAIAttributes.INPUT_VALUE: prompt,
                PydanticAIAttributes.INPUT_MIME_TYPE: "text/plain",
            }
            model_info = extract_model_info(self)
            if model_info.get("model_name"):
                attributes[PydanticAIAttributes.MODEL_NAME] = model_info["model_name"]
            if model_info.get("model_provider"):
                attributes[PydanticAIAttributes.MODEL_PROVIDER] = model_info["model_provider"]
            start_time = time.time()
            span = tracer.start_span(
                "pydantic_ai.agent.run_stream",
                kind=trace_api.SpanKind.CLIENT,
                attributes=attributes,
            )
            try:
                stream = await original_method(self, *args, **kwargs)
                return _AwaitableTracedStream(stream, span, start_time)
            except BaseException as error:
                _AwaitableTracedStream(None, span, start_time)._finish(error)
                raise

        return coroutine_wrapper

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
            if self._legacy_usage_original is not None:
                usage_class, original = self._legacy_usage_original
                usage_class.incr = original
                self._legacy_usage_original = None

        except ImportError:
            pass

        self._restore_execution_methods()
        self._is_instrumented = False
        self._tracer = None

        logger.info("Pydantic AI instrumentation disabled")

    @property
    def is_instrumented(self) -> bool:
        """Check if Pydantic AI is instrumented."""
        return self._is_instrumented


# Result methods that hand the run's final output to the caller. They are only
# observed: consuming or re-validating a stream here would repeat validators and
# message-history updates, so a stream the caller never finishes has no output.
_OUTPUT_STREAMS = ("stream_output", "stream_text", "stream")
_OUTPUT_GETTERS = ("get_output", "get_data")
_NO_OUTPUT = object()


class _TracedStream:
    """Proxy the entered result; its original manager owns stream cleanup."""

    def __init__(self, stream):
        self._stream = stream
        self._chunk_count = 0
        self._final_output = _NO_OUTPUT

    def __aiter__(self):
        return self

    async def __anext__(self):
        chunk = await self._stream.__anext__()
        self._chunk_count += 1
        return chunk

    # Forward other attributes to underlying stream
    def __getattr__(self, name):
        value = getattr(self._stream, name)
        if callable(value) and name in _OUTPUT_STREAMS:
            return self._observe_stream(value)
        if callable(value) and name in _OUTPUT_GETTERS:
            return self._observe_getter(value)
        return value

    def _observe_stream(self, method):
        @functools.wraps(method)
        def observed(*args, **kwargs):
            items = method(*args, **kwargs)
            if not hasattr(items, "__aiter__"):
                return items
            return self._observe_items(items, bool(kwargs.get("delta")))

        return observed

    async def _observe_items(self, items, delta):
        # A real async generator, so callers that close generators explicitly
        # still close the native one.
        parts = []
        try:
            async for item in items:
                if delta:
                    parts.append(item)
                else:
                    parts = [item]
                yield item
        finally:
            aclose = getattr(items, "aclose", None)
            if aclose is not None:
                await aclose()
        if delta:
            self._final_output = "".join(str(part) for part in parts)
        elif parts:
            self._final_output = parts[-1]

    def _observe_getter(self, method):
        @functools.wraps(method)
        def observed(*args, **kwargs):
            output = method(*args, **kwargs)
            if not inspect.isawaitable(output):
                self._final_output = output
                return output
            return self._await_output(output)

        return observed

    async def _await_output(self, awaitable):
        output = await awaitable
        self._final_output = output
        return output

    def _record_final_output(self, span):
        if self._final_output is not _NO_OUTPUT:
            _record_output_value(span, self._final_output)


class _AwaitableTracedStream(_TracedStream):
    """Manage cleanup for coroutine factories without changing native streams."""

    def __init__(self, stream, span, start_time):
        super().__init__(stream)
        self._context_manager = stream
        self._span = span
        self._start_time = start_time

    async def __aenter__(self):
        try:
            if hasattr(self._context_manager, "__aenter__"):
                self._stream = await self._context_manager.__aenter__()
            return self
        except BaseException as error:
            self._finish(error)
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        error = exc_val
        try:
            # Exit the manager, not a potentially different entered result.
            if hasattr(self._context_manager, "__aexit__"):
                return await self._context_manager.__aexit__(exc_type, exc_val, exc_tb)
        except BaseException as exit_error:
            error = exit_error
            raise
        finally:
            self._finish(error)

    def _finish(self, error=None):
        import time

        self._span.set_attribute(
            PydanticAIAttributes.DURATION_MS, (time.time() - self._start_time) * 1000
        )
        self._span.set_attribute(PydanticAIAttributes.STREAM_CHUNK_COUNT, self._chunk_count)
        if error is not None:
            self._span.set_attribute(PydanticAIAttributes.IS_ERROR, True)
            self._span.set_attribute(PydanticAIAttributes.ERROR_MESSAGE, str(error))
            self._span.set_status(trace_api.Status(trace_api.StatusCode.ERROR, str(error)))
            self._span.record_exception(error)
        else:
            self._span.set_attribute(PydanticAIAttributes.IS_ERROR, False)
            self._span.set_status(trace_api.Status(trace_api.StatusCode.OK))
        self._record_final_output(self._span)
        self._span.end()


# Convenience function for simple initialization
def instrument_pydantic_ai(
    tracer_provider: Optional[Any] = None,
    use_builtin: bool = False,
    config: Optional[TraceConfig] = None,
) -> PydanticAIInstrumentor:
    """Convenience function to instrument Pydantic AI.

    Args:
        tracer_provider: Optional OpenTelemetry tracer provider
        use_builtin: Deprecated compatibility flag; uses FI-aware wrappers
        config: Content privacy policy; defaults resolve from the environment

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
    if config is not None:
        kwargs["config"] = config

    instrumentor.instrument(**kwargs)
    return instrumentor
