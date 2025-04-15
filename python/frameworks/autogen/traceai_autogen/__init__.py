import json
import logging
from importlib import import_module
from typing import Any, Callable, Collection, Dict, Optional, Union

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from traceai_autogen.utils import _to_dict
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.trace import Link, SpanContext, Status, StatusCode


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

_MODULE = "autogen"
__version__ = "0.1.0"


class AutogenInstrumentor(BaseInstrumentor):
    """
    An instrumentor for autogen
    """

    __slots__ = (
        "_original_generate",
        "_original_initiate_chat",
        "_original_execute_function",
    )

    def __init__(self) -> None:
        super().__init__()
        self._original_generate: Optional[Callable[..., Any]] = None
        self._original_initiate_chat: Optional[Callable[..., Any]] = None
        self._original_execute_function: Optional[Callable[..., Any]] = None
        self.tracer = None

    def _safe_json_dumps(self, obj: Any) -> str:
        try:
            return json.dumps(obj)
        except (TypeError, ValueError):
            return json.dumps(str(obj))

    def instrumentation_dependencies(self) -> Collection[str]:
        return [_MODULE]

    def _instrument(self, **kwargs: Any) -> None:
        # Get tracer provider and config
        if not (tracer_provider := kwargs.get("tracer_provider")):
            tracer_provider = trace_api.get_tracer_provider()
        if not (config := kwargs.get("config")):
            config = TraceConfig()
        else:
            assert isinstance(config, TraceConfig)
            
        # Create tracer
        self.tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider),
            config=config,
        )
        
        autogen = import_module(_MODULE)
        ConversableAgent = autogen.ConversableAgent
        
        # Save original methods
        self._original_generate = ConversableAgent.generate_reply
        self._original_initiate_chat = ConversableAgent.initiate_chat
        self._original_execute_function = ConversableAgent.execute_function

        instrumentor = self

        def wrapped_generate(
            agent_self: Any,
            messages: Optional[Any] = None,
            sender: Optional[str] = None,
            **kwargs: Any,
        ) -> Any:
            try:
                current_span = trace_api.get_current_span()
                current_context: SpanContext = current_span.get_span_context()

                with instrumentor.tracer.start_as_current_span(
                    agent_self.__class__.__name__,
                    context=trace_api.set_span_in_context(current_span),
                    links=[Link(current_context)],
                ) as span:
                    span.set_attribute(SpanAttributes.FI_SPAN_KIND, "AGENT")
                    span.set_attribute(
                        SpanAttributes.RAW_INPUT,
                        instrumentor._safe_json_dumps(messages),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(messages),
                    )
                    span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")
                    span.set_attribute("agent.type", agent_self.__class__.__name__)

                    response = instrumentor._original_generate(
                        agent_self, messages=messages, sender=sender, **kwargs
                    )

                    span.set_attribute(
                        SpanAttributes.RAW_OUTPUT,
                        instrumentor._safe_json_dumps(response),
                    )
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(response),
                    )
                    span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

                    span.set_status(Status(StatusCode.OK))
                    return response
            except Exception as e:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(e)
                raise

        def wrapped_initiate_chat(
            agent_self: Any, recipient: Any, *args: Any, **kwargs: Any
        ) -> Any:
            try:
                message = kwargs.get("message", args[0] if args else None)
                current_span = trace_api.get_current_span()
                current_context: SpanContext = current_span.get_span_context()

                with instrumentor.tracer.start_as_current_span(
                    "Autogen",
                    context=trace_api.set_span_in_context(current_span),
                    links=[Link(current_context)],
                ) as span:
                    span.set_attribute(SpanAttributes.FI_SPAN_KIND, "AGENT")
                    span.set_attribute(
                        SpanAttributes.RAW_INPUT,
                        instrumentor._safe_json_dumps({
                            "args": args,
                             **kwargs,
                        }),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(message),
                    )
                    span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

                    result = instrumentor._original_initiate_chat(
                        agent_self, recipient, *args, **kwargs
                    )

                    span.set_attribute(
                        SpanAttributes.RAW_OUTPUT,
                        instrumentor._safe_json_dumps(_to_dict(result)),
                    )
                    if hasattr(result, "chat_history") and result.chat_history:
                        last_message = result.chat_history[-1]["content"]
                        span.set_attribute(
                            SpanAttributes.OUTPUT_VALUE,
                            instrumentor._safe_json_dumps(last_message),
                        )
                    else:
                        span.set_attribute(
                            SpanAttributes.OUTPUT_VALUE,
                            instrumentor._safe_json_dumps(result),
                        )

                    span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

                    span.set_status(Status(StatusCode.OK))
                    return result
            except Exception as e:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(e)
                raise

        def wrapped_execute_function(
            agent_self: Any,
            func_call: Union[str, Dict[str, Any]],
            call_id: Optional[str] = None,
            verbose: bool = False,
        ) -> Any:
            try:
                current_span = trace_api.get_current_span()
                current_context: SpanContext = current_span.get_span_context()

                # Handle both dictionary and string inputs
                if isinstance(func_call, str):
                    function_name = func_call
                    func_call = {"name": function_name}
                else:
                    function_name = func_call.get("name", "unknown")

                with instrumentor.tracer.start_as_current_span(
                    f"{function_name}",
                    context=trace_api.set_span_in_context(current_span),
                    links=[Link(current_context)],
                ) as span:
                    span.set_attribute(SpanAttributes.FI_SPAN_KIND, "TOOL")
                    span.set_attribute(SpanAttributes.TOOL_NAME, function_name)

                    # Record input
                    span.set_attribute(
                        SpanAttributes.RAW_INPUT,
                        instrumentor._safe_json_dumps(func_call),
                    )
                    span.set_attribute(
                        SpanAttributes.INPUT_VALUE,
                        instrumentor._safe_json_dumps(func_call),
                    )
                    span.set_attribute(SpanAttributes.INPUT_MIME_TYPE, "application/json")

                    # If the agent stores a function map, you can store annotations
                    if hasattr(agent_self, "_function_map"):
                        function_map = getattr(agent_self, "_function_map", {})
                        if function_name in function_map:
                            func = function_map[function_name]
                            if hasattr(func, "__annotations__"):
                                span.set_attribute(
                                    SpanAttributes.TOOL_PARAMETERS,
                                    instrumentor._safe_json_dumps(func.__annotations__),
                                )

                    # Record function call details
                    if isinstance(func_call, dict):
                        # Record function arguments
                        if "arguments" in func_call:
                            span.set_attribute(
                                SpanAttributes.TOOL_CALL_FUNCTION_ARGUMENTS,
                                instrumentor._safe_json_dumps(func_call["arguments"]),
                            )

                        # Record function name
                        span.set_attribute(SpanAttributes.TOOL_CALL_FUNCTION_NAME, function_name)

                    # Execute function
                    result = instrumentor._original_execute_function(
                        agent_self, func_call, call_id=call_id, verbose=verbose
                    )

                    # Record output
                    span.set_attribute(
                        SpanAttributes.RAW_OUTPUT,
                        instrumentor._safe_json_dumps(result),
                    )
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        instrumentor._safe_json_dumps(result),
                    )
                    span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, "application/json")

                    span.set_status(Status(StatusCode.OK))
                    return result

            except Exception as e:
                if span is not None:
                    span.set_status(Status(StatusCode.ERROR))
                    span.record_exception(e)
                raise

        # Replace methods on ConversableAgent with wrapped versions
        ConversableAgent.generate_reply = wrapped_generate
        ConversableAgent.initiate_chat = wrapped_initiate_chat
        ConversableAgent.execute_function = wrapped_execute_function

    def _uninstrument(self, **kwargs: Any) -> None:
        """Restore original behavior."""
        if (
            self._original_generate
            and self._original_initiate_chat
            and self._original_execute_function
        ):
            # Import autogen module safely to avoid circular imports
            autogen = import_module(_MODULE)
            ConversableAgent = autogen.ConversableAgent
            
            ConversableAgent.generate_reply = self._original_generate
            ConversableAgent.initiate_chat = self._original_initiate_chat
            ConversableAgent.execute_function = self._original_execute_function
            self._original_generate = None
            self._original_initiate_chat = None
            self._original_execute_function = None

