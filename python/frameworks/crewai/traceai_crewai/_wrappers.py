import json
from contextlib import contextmanager
from enum import Enum
from inspect import signature
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

from crewai.tools.tool_calling import ToolCalling
from fi_instrumentation import get_attributes_from_context
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.util.types import AttributeValue

from ._privacy import _credential_key, _safe_data


def _flatten(
    mapping: Optional[Mapping[str, Any]],
) -> Iterator[Tuple[str, AttributeValue]]:
    if not mapping:
        return
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            for sub_key, sub_value in _flatten(value):
                yield f"{key}.{sub_key}", sub_value
        elif isinstance(value, List) and any(
            isinstance(item, Mapping) for item in value
        ):
            for index, sub_mapping in enumerate(value):
                for sub_key, sub_value in _flatten(sub_mapping):
                    yield f"{key}.{index}.{sub_key}", sub_value
        else:
            if isinstance(value, Enum):
                value = value.value
            yield key, value


def _hide_input_content(tracer: Any) -> bool:
    config = getattr(tracer, "_self_config", None)
    return any(
        getattr(config, key, False) is True
        for key in ("hide_inputs", "hide_input_text", "hide_input_messages")
    )


def _safe_context_attributes() -> dict:
    return {
        key: ("__REDACTED__" if _credential_key(key) else _safe_data(value))
        for key, value in get_attributes_from_context()
    }


def _safe_output(value: Any) -> Any:
    cls = type(value)
    if cls.__module__.startswith("crewai.") and cls.__name__ == "LiteAgentOutput":
        return _safe_data(value.raw)
    if cls.__module__.startswith("crewai.") and cls.__name__ in (
        "TaskOutput",
        "CrewOutput",
    ):
        content = value.json_dict if type(value.json_dict) is dict else value.raw
        return _safe_data(content)
    return _safe_data(value)


def _bound_call(
    method: Callable[..., Any], args: Tuple[Any, ...], kwargs: Mapping[str, Any]
) -> dict:
    try:
        sig = signature(method)
        needs_self = next(iter(sig.parameters), None) == "self"
        return dict(
            sig.bind_partial(
                *((None,) if needs_self else ()), *args, **kwargs
            ).arguments
        )
    except (TypeError, ValueError):
        return {}


def _task_input(
    method: Callable[..., Any],
    task: Any,
    args: Tuple[Any, ...],
    kwargs: Mapping[str, Any],
) -> str:
    bound = _bound_call(method, args, kwargs)
    return json.dumps(
        {
            "description": _safe_data(getattr(task, "description", None)),
            "expected_output": _safe_data(getattr(task, "expected_output", None)),
            "context": _safe_data(bound.get("context")),
        }
    )


def _get_input_value(method: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
    """Project only ToolCalling's documented application fields.

    ToolUsage also receives the runtime tool and parsing state: none of those
    arguments may enter a generic bound-arguments/object serialization fallback.
    """
    bound = _bound_call(method, args, kwargs)
    calling = bound.get("calling", kwargs.get("calling"))
    if isinstance(calling, ToolCalling):
        return json.dumps(
            {
                "tool_name": _safe_data(calling.tool_name),
                "tool_args": _safe_data(calling.arguments),
            }
        )
    return json.dumps({})


class _ExecuteCoreWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def _suppressed(self) -> bool:
        return bool(context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY))

    @contextmanager
    def _span(self, wrapped, instance, args, kwargs):
        if instance:
            span_name = f"{instance.__class__.__name__}.{wrapped.__name__}"
        else:
            span_name = wrapped.__name__
        with self._tracer.start_as_current_span(
            span_name,
            attributes=dict(
                _flatten(
                    {
                        GEN_AI_SPAN_KIND: FiSpanKindValues.AGENT,
                        SpanAttributes.INPUT_VALUE: (
                            "__REDACTED__"
                            if _hide_input_content(self._tracer)
                            else _task_input(wrapped, instance, args, kwargs)
                        ),
                    }
                )
            ),
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            bound = _bound_call(wrapped, args, kwargs)
            agent = bound.get("agent", args[0] if args else None)
            crew = getattr(agent, "crew", None) if agent is not None else None
            task = instance

            if crew:
                span.set_attribute("crew_key", crew.key)
                span.set_attribute("crew_id", str(crew.id))
            span.set_attribute("task_key", task.key)
            span.set_attribute("task_id", str(task.id))

            if crew and crew.share_crew and not _hide_input_content(self._tracer):
                span.set_attribute(
                    "formatted_description", _safe_data(task.description)
                )
                span.set_attribute(
                    "formatted_expected_output", _safe_data(task.expected_output)
                )
            try:
                yield span
            except BaseException:
                # Cancellation must end the span, without exporting exception text.
                span.set_status(trace_api.StatusCode.ERROR)
                raise

    def _finish(self, span, response):
        span.set_status(trace_api.StatusCode.OK)
        span.set_attribute(OUTPUT_VALUE, json.dumps(_safe_output(response)))
        span.set_attributes(_safe_context_attributes())

    def __call__(self, wrapped, instance, args, kwargs):
        if self._suppressed():
            return wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            response = wrapped(*args, **kwargs)
            self._finish(span, response)
            return response


class _AsyncExecuteCoreWrapper(_ExecuteCoreWrapper):
    async def __call__(self, wrapped, instance, args, kwargs):
        if self._suppressed():
            return await wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            response = await wrapped(*args, **kwargs)
            self._finish(span, response)
            return response


class _KickoffWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def _deferred_or_suppressed(self, instance) -> bool:
        # CrewAI's streaming factories are lazy. The native iterator starts a
        # worker/task which calls kickoff (or akickoff) again with stream=False.
        # Trace that actual execution, not the factory: one span, final usage,
        # native close/cancellation semantics, and no span for an unconsumed run.
        # CrewAI propagates the consuming context into its worker/task.
        return bool(
            context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY)
            or getattr(instance, "stream", False)
        )

    @contextmanager
    def _span(self, wrapped, instance, args, kwargs):
        span_name = f"{instance.__class__.__name__}.{wrapped.__name__}"
        with self._tracer.start_as_current_span(
            span_name,
            record_exception=False,
            set_status_on_exception=False,
            attributes=dict(
                _flatten(
                    {
                        GEN_AI_SPAN_KIND: FiSpanKindValues.CHAIN,
                    }
                )
            ),
        ) as span:
            crew = instance
            inputs = kwargs.get("inputs", None) or (args[0] if args else None)
            span.set_attribute("crew_key", crew.key)
            span.set_attribute("crew_id", str(crew.id))
            # Only explicit kickoff inputs are content; checkpoint/file objects are runtime state.
            span.set_attribute(
                SpanAttributes.INPUT_VALUE,
                (
                    "__REDACTED__"
                    if _hide_input_content(self._tracer)
                    else json.dumps(_safe_data(inputs))
                ),
            )
            if not _hide_input_content(self._tracer):
                span.set_attribute("crew_inputs", json.dumps(_safe_data(inputs)))
            if not _hide_input_content(self._tracer):
                span.set_attribute(
                    "crew_agents",
                    json.dumps(
                        _safe_data(
                            [
                                {
                                    "key": agent.key,
                                    "id": str(agent.id),
                                    "role": agent.role,
                                    "goal": agent.goal,
                                    "backstory": agent.backstory,
                                    "verbose?": agent.verbose,
                                    "max_iter": agent.max_iter,
                                    "max_rpm": agent.max_rpm,
                                    "delegation_enabled": agent.allow_delegation,
                                    "tools_names": [
                                        tool.name.casefold()
                                        for tool in agent.tools or []
                                    ],
                                }
                                for agent in crew.agents
                            ]
                        )
                    ),
                )
                span.set_attribute(
                    "crew_tasks",
                    json.dumps(
                        _safe_data(
                            [
                                {
                                    "id": str(task.id),
                                    "description": task.description,
                                    "expected_output": task.expected_output,
                                    "async_execution?": task.async_execution,
                                    "human_input?": task.human_input,
                                    "agent_role": (
                                        task.agent.role if task.agent else "None"
                                    ),
                                    "agent_key": task.agent.key if task.agent else None,
                                    # CrewAI defaults Task.context to a NOT_SPECIFIED
                                    # sentinel that is truthy but not iterable, so this
                                    # must guard on type before iterating.
                                    "context": (
                                        [
                                            context_task.description
                                            for context_task in task.context
                                        ]
                                        if isinstance(task.context, (list, tuple))
                                        and task.context
                                        else None
                                    ),
                                    "tools_names": [
                                        tool.name.casefold()
                                        for tool in task.tools or []
                                    ],
                                }
                                for task in crew.tasks
                            ]
                        )
                    ),
                )
            try:
                yield span
            except BaseException:
                span.set_status(trace_api.StatusCode.ERROR)
                raise

    def _finish(self, span, crew, output):
        usage = crew.usage_metrics
        for field, attribute in (
            ("prompt_tokens", GEN_AI_USAGE_INPUT_TOKENS),
            ("completion_tokens", GEN_AI_USAGE_OUTPUT_TOKENS),
            ("total_tokens", GEN_AI_USAGE_TOTAL_TOKENS),
        ):
            value = (
                usage.get(field)
                if isinstance(usage, dict)
                else getattr(usage, field, None)
            )
            if value is not None:
                span.set_attribute(attribute, int(value))
        span.set_status(trace_api.StatusCode.OK)
        span.set_attribute(OUTPUT_VALUE, json.dumps(_safe_output(output)))
        span.set_attributes(_safe_context_attributes())

    def __call__(self, wrapped, instance, args, kwargs):
        if self._deferred_or_suppressed(instance):
            return wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            output = wrapped(*args, **kwargs)
            self._finish(span, instance, output)
            return output


class _AsyncKickoffWrapper(_KickoffWrapper):
    async def __call__(self, wrapped, instance, args, kwargs):
        if self._deferred_or_suppressed(instance):
            return await wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs) as span:
            output = await wrapped(*args, **kwargs)
            self._finish(span, instance, output)
            return output


_ACTIVE_TOOL = context_api.create_key("traceai-crewai-active-tool")


def _tool_identity(tool):
    original = getattr(tool, "_original_tool", None)
    return original if original is not None else tool


class _ToolUseWrapper:
    def __init__(self, tracer: trace_api.Tracer) -> None:
        self._tracer = tracer

    def _tool(self, wrapped, instance, args, kwargs):
        return _bound_call(wrapped, args, kwargs).get("tool", kwargs.get("tool"))

    def _input(self, wrapped, instance, args, kwargs):
        return _get_input_value(wrapped, *args, **kwargs)

    def _suppressed(self, tool):
        return bool(
            context_api.get_value(context_api._SUPPRESS_INSTRUMENTATION_KEY)
            or (
                tool is not None
                and context_api.get_value(_ACTIVE_TOOL) is _tool_identity(tool)
            )
        )

    @contextmanager
    def _span(self, wrapped, instance, args, kwargs, tool):
        span_name = f"{instance.__class__.__name__}.{wrapped.__name__}"
        with self._tracer.start_as_current_span(
            span_name,
            attributes={
                GEN_AI_SPAN_KIND: FiSpanKindValues.TOOL.value,
                INPUT_VALUE: (
                    "__REDACTED__"
                    if _hide_input_content(self._tracer)
                    else self._input(wrapped, instance, args, kwargs)
                ),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            tool_name = getattr(tool, "name", "")
            if type(tool_name) is str:
                span.set_attribute(SpanAttributes.GEN_AI_TOOL_NAME, tool_name)
            # Legacy ToolUsage can delegate to modern invoke. Deduplicate only
            # that same tool; a different tool invoked inside it gets a child.
            token = context_api.attach(
                context_api.set_value(_ACTIVE_TOOL, _tool_identity(tool))
            )
            try:
                yield span
            except BaseException:
                span.set_status(trace_api.StatusCode.ERROR)
                raise
            finally:
                context_api.detach(token)

    def _finish(self, span, response):
        span.set_status(trace_api.StatusCode.OK)
        span.set_attribute(OUTPUT_VALUE, json.dumps(_safe_output(response)))
        span.set_attributes(_safe_context_attributes())

    def __call__(self, wrapped, instance, args, kwargs):
        tool = self._tool(wrapped, instance, args, kwargs)
        if self._suppressed(tool):
            return wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs, tool) as span:
            response = wrapped(*args, **kwargs)
            self._finish(span, response)
            return response


class _StructuredToolWrapper(_ToolUseWrapper):
    def _tool(self, wrapped, instance, args, kwargs):
        return instance

    def _input(self, wrapped, instance, args, kwargs):
        # Only the documented application input, never config, func, schemas,
        # callbacks or other runtime state. Reuse the existing privacy projector.
        bound = _bound_call(wrapped, args, kwargs)
        return json.dumps(
            {
                "tool_name": _safe_data(instance.name),
                "tool_args": _safe_data(bound.get("input")),
            }
        )


class _AsyncStructuredToolWrapper(_StructuredToolWrapper):
    async def __call__(self, wrapped, instance, args, kwargs):
        if self._suppressed(instance):
            return await wrapped(*args, **kwargs)
        with self._span(wrapped, instance, args, kwargs, instance) as span:
            response = await wrapped(*args, **kwargs)
            self._finish(span, response)
            return response


class _NativeToolWrapper(_StructuredToolWrapper):
    def _input(self, wrapped, instance, args, kwargs):
        # run/arun pass application arguments directly, unlike invoke(input,
        # config). Bind against the callable solely to name positional inputs;
        # never serialize the callable or the tool's runtime fields.
        function = getattr(instance, "func", None)
        if function is None:
            function = instance._run
        bound = _bound_call(function, args, kwargs)
        bound.pop("self", None)
        return json.dumps(
            {
                "tool_name": _safe_data(instance.name),
                "tool_args": _safe_data(bound),
            }
        )


class _AsyncNativeToolWrapper(_NativeToolWrapper, _AsyncStructuredToolWrapper):
    """Use run/arun argument projection with the awaited tool lifecycle."""


INPUT_VALUE = SpanAttributes.INPUT_VALUE
GEN_AI_SPAN_KIND = SpanAttributes.GEN_AI_SPAN_KIND
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
OUTPUT_MIME_TYPE = SpanAttributes.OUTPUT_MIME_TYPE
GEN_AI_USAGE_INPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_INPUT_TOKENS
GEN_AI_USAGE_OUTPUT_TOKENS = SpanAttributes.GEN_AI_USAGE_OUTPUT_TOKENS
GEN_AI_USAGE_TOTAL_TOKENS = SpanAttributes.GEN_AI_USAGE_TOTAL_TOKENS
