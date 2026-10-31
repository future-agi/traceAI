import asyncio
import collections
import inspect
from collections.abc import Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import partial
from secrets import randbits
from types import ModuleType
from typing import (
    TYPE_CHECKING,
    Any,  # type: ignore[attr-defined]
    AsyncGenerator,
    Awaitable,
    Callable,
    Dict,
    Generator,
    Iterator,
    Literal,
    Optional,
    Tuple,
    Union,
    _TypedDictMeta,
    cast,
    get_args,
    get_origin,
)

import wrapt  # type: ignore[import-untyped]
from fi_instrumentation.fi_types import FiSpanKindValues, SpanAttributes
from opentelemetry.attributes import BoundedAttributes
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY, Context, get_value
from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator
from opentelemetry.sdk.trace.sampling import Sampler, SamplingResult
from opentelemetry.trace import (
    INVALID_SPAN,
    INVALID_SPAN_ID,
    INVALID_TRACE_ID,
    Link,
    Span,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    use_span,
)
from opentelemetry.util.types import Attributes
from typing_extensions import ParamSpec, TypeVar, _AnnotatedAlias, overload

from ._attributes import (
    get_input_attributes,
    get_span_kind_attributes,
    get_tool_attributes,
)
from ._spans import FiSpan
from .config import TraceConfig
from .context_attributes import get_attributes_from_context

if TYPE_CHECKING:
    from ._types import FiSpanKind

ParametersType = ParamSpec("ParametersType")
ReturnType = TypeVar("ReturnType")

pydantic: Optional[ModuleType]
try:
    import pydantic  # try to import without adding a dependency
except ImportError:
    pydantic = None


class _IdGenerator(IdGenerator):
    """
    An IdGenerator that uses a different source of randomness to
    avoid being affected by seeds set by user application.
    """

    def generate_span_id(self) -> int:
        while (span_id := randbits(64)) == INVALID_SPAN_ID:
            continue
        return span_id

    def generate_trace_id(self) -> int:
        while (trace_id := randbits(128)) == INVALID_TRACE_ID:
            continue
        return trace_id


class _OpaqueSamplerKey:
    """Probe SDK key handling without putting sampler data into its warnings."""

    def __init__(self, key: Any) -> None:
        self.key = key

    def __bool__(self) -> bool:
        return bool(self.key)


class _StringableSamplerKey(_OpaqueSamplerKey):
    def __str__(self) -> str:
        return "<non-string sampler key>"


class _PrivacySampler(Sampler):
    """Filter sampler-owned attributes before the SDK calls processors.

    The delegate still receives the real sampling inputs and controls the
    decision, result attribute selection and trace state. This wrapper is local
    to the FI tracer; it never changes the provider's sampler.
    """

    def __init__(self, delegate: Sampler, config: TraceConfig) -> None:
        self._delegate = delegate
        self._config = config
        self.selected_kind = None

    def should_sample(self, *args: Any, **kwargs: Any) -> SamplingResult:
        result = self._delegate.should_sample(*args, **kwargs)
        attributes = result.attributes
        if attributes is not None:
            # Ask the installed SDK how it handles malformed keys, using only
            # an opaque key and a harmless value in its validation diagnostics.
            # Normalize before masking and before resolving collisions, as the
            # SDK's mapping validation does. Never log an application's key.
            normalized = {}
            for key, value in attributes.items():
                if isinstance(key, str) and key:
                    normalized[key] = value
                    continue
                if key is None or isinstance(key, str):
                    probe = key
                elif (type(key).__str__ is not object.__str__ or
                      type(key).__repr__ is not object.__repr__):
                    probe = _StringableSamplerKey(key)
                else:
                    probe = _OpaqueSamplerKey(key)
                if BoundedAttributes(attributes={probe: 0}):
                    normalized[str(key)] = value
            attributes = {}
            none_value = object()
            for key, value in normalized.items():
                # Sampler results are values, not FI lazy-value callbacks. A
                # sentinel distinguishes an SDK-valid None from a policy drop.
                candidate = none_value if value is None else ((lambda: value) if callable(value) else value)
                masked = self._config.mask(key, candidate)
                if masked is not None:
                    attributes[key] = None if masked is none_value else masked
            self.selected_kind = attributes.get(SpanAttributes.GEN_AI_SPAN_KIND)
        return SamplingResult(result.decision, attributes, result.trace_state)

    def get_description(self) -> str:
        return self._delegate.get_description()


class FITracer(wrapt.ObjectProxy):  # type: ignore[misc]
    def __init__(self, wrapped: Tracer, config: TraceConfig = TraceConfig()) -> None:
        super().__init__(wrapped)
        self._self_config = config
        self._self_id_generator = _IdGenerator()
        self._self_active_sampler: ContextVar[Optional[_PrivacySampler]] = ContextVar(
            "fi_active_sampler", default=None
        )

    @property
    def sampler(self) -> Sampler:
        active = self._self_active_sampler.get()
        return active if active is not None else _PrivacySampler(self.__wrapped__.sampler, self._self_config)

    @property
    def id_generator(self) -> IdGenerator:
        ans = getattr(self.__wrapped__, "id_generator", None)
        if ans and ans.__class__ is RandomIdGenerator:
            return self._self_id_generator
        return cast(IdGenerator, ans)

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Optional[Context] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Optional["Sequence[Link]"] = (),
        start_time: Optional[int] = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
        *,
        fi_span_kind: Optional["FiSpanKind"] = None,
    ) -> Iterator[FiSpan]:
        span = self.start_span(
            name=name,
            fi_span_kind=fi_span_kind,
            context=context,
            kind=kind,
            attributes=attributes,
            links=links,
            start_time=start_time,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        )
        with use_span(
            span,  # type: ignore[arg-type]
            end_on_exit=end_on_exit,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        ) as current_span:
            yield cast(FiSpan, current_span)

    def start_span(
        self,
        name: str,
        context: Optional[Context] = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Optional["Sequence[Link]"] = (),
        start_time: Optional[int] = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        *,
        fi_span_kind: Optional["FiSpanKind"] = None,
    ) -> FiSpan:
        if get_value(_SUPPRESS_INSTRUMENTATION_KEY):
            logical_kind = (
                get_span_kind_attributes(fi_span_kind).get(SpanAttributes.GEN_AI_SPAN_KIND)
                if fi_span_kind is not None else (attributes or {}).get(SpanAttributes.GEN_AI_SPAN_KIND)
            )
            return FiSpan(INVALID_SPAN, config=self._self_config, span_kind=logical_kind)

        # A sampler runs inside start_span, so it must see the creation
        # attributes. Apply privacy policy before passing anything to OTel.
        creation_attributes = dict(attributes or {})
        if fi_span_kind is not None:
            creation_attributes.update(get_span_kind_attributes(fi_span_kind))
        creation_attributes.update(dict(get_attributes_from_context()))
        masked_attributes = {}
        for key, value in creation_attributes.items():
            masked = self._self_config.mask(key, value)
            if masked is not None:
                masked_attributes[key] = masked

        # Links are observed by samplers and processors as well as exporters.
        # Copy them so neither the caller's Link nor its attributes are changed.
        masked_links = None if links is None else [
            Link(link.context, {
                key: masked
                for key, value in (link.attributes or {}).items()
                if (masked := self._self_config.mask(key, value)) is not None
            } if link.attributes is not None else None)
            for link in links
        ]

        tracer = cast(Tracer, self.__wrapped__)
        delegate_sampler = getattr(tracer, "sampler", None)
        privacy_sampler = (
            _PrivacySampler(delegate_sampler, self._self_config)
            if delegate_sampler is not None else None
        )
        token = self._self_active_sampler.set(privacy_sampler)
        try:
            otel_span = tracer.__class__.start_span(
                self,
                name=name,
                context=context,
                kind=kind,
                attributes=masked_attributes,
                links=masked_links,
                start_time=start_time,
                record_exception=record_exception,
                set_status_on_exception=set_status_on_exception,
            )
        finally:
            self._self_active_sampler.reset(token)
        fi_span = FiSpan(
            otel_span,
            config=self._self_config,
            span_kind=masked_attributes.get(SpanAttributes.GEN_AI_SPAN_KIND),
        )
        # Remember only the kind actually selected by the sampler. Reapplying
        # caller attributes would overwrite or resurrect sampler-owned fields.
        sampled_attributes = getattr(otel_span, "attributes", None) or {}
        sampled_kind = sampled_attributes.get(SpanAttributes.GEN_AI_SPAN_KIND)
        if sampled_kind is None and privacy_sampler is not None and otel_span.is_recording():
            # The SDK's attribute limit may evict the selected kind before we
            # wrap the span. Retain only a kind the sampler actually returned.
            sampled_kind = privacy_sampler.selected_kind
        # Only a caller-declared kind has FI's retention priority. A kind added
        # solely by the sampler must obey the SDK's normal attribute limits.
        if sampled_kind is not None and SpanAttributes.GEN_AI_SPAN_KIND in masked_attributes:
            fi_span._remember_sampled_kind(sampled_kind)
        return fi_span

    @overload  # for @tracer.agent usage (no parameters)
    def agent(
        self,
        wrapped_function: Callable[ParametersType, ReturnType],
        /,
        *,
        name: None = None,
    ) -> Callable[ParametersType, ReturnType]: ...

    @overload  # for @tracer.agent(name="name") usage (with parameters)
    def agent(
        self,
        wrapped_function: None = None,
        /,
        *,
        name: Optional[str] = None,
    ) -> Callable[
        [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
    ]: ...

    def agent(
        self,
        wrapped_function: Optional[Callable[ParametersType, ReturnType]] = None,
        /,
        *,
        name: Optional[str] = None,
    ) -> Union[
        Callable[ParametersType, ReturnType],
        Callable[
            [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
        ],
    ]:
        return self._chain(
            wrapped_function,
            kind=FiSpanKindValues.AGENT,  # chains and agents differ only in span kind
            name=name,
        )

    @overload  # for @tracer.chain usage (no parameters)
    def chain(
        self,
        wrapped_function: Callable[ParametersType, ReturnType],
        /,
        *,
        name: None = None,
    ) -> Callable[ParametersType, ReturnType]: ...

    @overload  # for @tracer.chain(name="name") usage (with parameters)
    def chain(
        self,
        wrapped_function: None = None,
        /,
        *,
        name: Optional[str] = None,
    ) -> Callable[
        [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
    ]: ...

    def chain(
        self,
        wrapped_function: Optional[Callable[ParametersType, ReturnType]] = None,
        /,
        *,
        name: Optional[str] = None,
    ) -> Union[
        Callable[ParametersType, ReturnType],
        Callable[
            [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
        ],
    ]:
        return self._chain(wrapped_function, kind=FiSpanKindValues.CHAIN, name=name)

    def _chain(
        self,
        wrapped_function: Optional[Callable[ParametersType, ReturnType]] = None,
        /,
        *,
        kind: FiSpanKindValues,
        name: Optional[str] = None,
    ) -> Union[
        Callable[ParametersType, ReturnType],
        Callable[
            [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
        ],
    ]:
        @wrapt.decorator  # type: ignore[misc]
        def sync_wrapper(
            wrapped: Callable[ParametersType, ReturnType],
            instance: Any,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
        ) -> ReturnType:
            is_generator = (
                inspect.isgeneratorfunction(wrapped)
                or inspect.isasyncgenfunction(wrapped)
            )
            context = _chain_context(
                tracer=self,
                name=name,
                kind=kind,
                wrapped=wrapped,
                instance=instance,
                args=args,
                kwargs=kwargs,
                end_on_exit=not is_generator,
            )
            if is_generator:
                return cast(ReturnType, _trace_generator(wrapped(*args, **kwargs), context))
            with context as chain_context:
                output = wrapped(*args, **kwargs)
                return chain_context.process_output(output)

        @wrapt.decorator  #  type: ignore[misc]
        async def async_wrapper(
            wrapped: Callable[ParametersType, Awaitable[ReturnType]],
            instance: Any,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
        ) -> ReturnType:
            tracer = self
            with _chain_context(
                tracer=tracer,
                name=name,
                kind=kind,
                wrapped=wrapped,
                instance=instance,
                args=args,
                kwargs=kwargs,
            ) as chain_context:
                output = await wrapped(*args, **kwargs)
                return chain_context.process_output(output)

        if wrapped_function is not None:
            if asyncio.iscoroutinefunction(wrapped_function):
                return async_wrapper(wrapped_function)  # type: ignore[no-any-return]
            return sync_wrapper(wrapped_function)  # type: ignore[no-any-return]
        return lambda f: (
            async_wrapper(f) if asyncio.iscoroutinefunction(f) else sync_wrapper(f)
        )

    @overload  # for @tracer.tool usage (no parameters)
    def tool(
        self,
        wrapped_function: Callable[ParametersType, ReturnType],
        /,
        *,
        name: None = None,
        description: Optional[str] = None,
        parameters: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Callable[ParametersType, ReturnType]: ...

    @overload  # for @tracer.tool(name="name") usage (with parameters)
    def tool(
        self,
        wrapped_function: None = None,
        /,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Callable[
        [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
    ]: ...

    def tool(
        self,
        wrapped_function: Optional[Callable[ParametersType, ReturnType]] = None,
        /,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Union[str, Dict[str, Any]]] = None,
    ) -> Union[
        Callable[ParametersType, ReturnType],
        Callable[
            [Callable[ParametersType, ReturnType]], Callable[ParametersType, ReturnType]
        ],
    ]:
        @wrapt.decorator  # type: ignore[misc]
        def sync_wrapper(
            wrapped: Callable[ParametersType, ReturnType],
            instance: Any,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
        ) -> ReturnType:
            is_generator = (
                inspect.isgeneratorfunction(wrapped)
                or inspect.isasyncgenfunction(wrapped)
            )
            context = _tool_context(
                tracer=self,
                name=name,
                description=description,
                parameters=parameters,
                wrapped=wrapped,
                instance=instance,
                args=args,
                kwargs=kwargs,
                end_on_exit=not is_generator,
            )
            if is_generator:
                return cast(ReturnType, _trace_generator(wrapped(*args, **kwargs), context))
            with context as tool_context:
                output = wrapped(*args, **kwargs)
                return tool_context.process_output(output)

        @wrapt.decorator  #  type: ignore[misc]
        async def async_wrapper(
            wrapped: Callable[ParametersType, Awaitable[ReturnType]],
            instance: Any,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
        ) -> ReturnType:
            tracer = self
            with _tool_context(
                tracer=tracer,
                name=name,
                description=description,
                parameters=parameters,
                wrapped=wrapped,
                instance=instance,
                args=args,
                kwargs=kwargs,
            ) as tool_context:
                output = await wrapped(*args, **kwargs)
                return tool_context.process_output(output)

        if wrapped_function is not None:
            if asyncio.iscoroutinefunction(wrapped_function):
                return async_wrapper(wrapped_function)  # type: ignore[no-any-return]
            return sync_wrapper(wrapped_function)  # type: ignore[no-any-return]
        return lambda f: (
            async_wrapper(f) if asyncio.iscoroutinefunction(f) else sync_wrapper(f)
        )


class _ChainContext:
    def __init__(self, span: "FiSpan") -> None:
        self._span = span

    def process_output(self, output: ReturnType) -> ReturnType:
        attributes = getattr(self._span, "attributes", {})
        has_output = OUTPUT_VALUE in attributes
        if not has_output:
            self._span.set_output(value=output)
        return output


@contextmanager
def _chain_context(
    *,
    tracer: "FITracer",
    name: Optional[str],
    kind: FiSpanKindValues,
    wrapped: Callable[ParametersType, ReturnType],
    instance: Any,
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    end_on_exit: bool = True,
) -> Iterator[_ChainContext]:
    span_name = name or _infer_span_name(instance=instance, callable=wrapped)
    bound_args = inspect.signature(wrapped).bind(*args, **kwargs)
    bound_args.apply_defaults()
    arguments = bound_args.arguments

    if len(arguments) == 1:
        argument = next(iter(arguments.values()))
        input_attributes = get_input_attributes(argument)
    else:
        input_attributes = get_input_attributes(arguments)

    with tracer.start_as_current_span(
        span_name,
        fi_span_kind=kind,
        attributes=input_attributes,
        end_on_exit=end_on_exit,
    ) as span:
        context = _ChainContext(span=span)
        yield context


class _ToolContext:
    def __init__(self, span: "FiSpan") -> None:
        self._span = span

    def process_output(self, output: ReturnType) -> ReturnType:
        attributes = getattr(self._span, "attributes", {})
        has_output = OUTPUT_VALUE in attributes
        if not has_output:
            self._span.set_output(value=output)
        return output


@contextmanager
def _tool_context(
    *,
    tracer: "FITracer",
    name: Optional[str],
    description: Optional[str],
    parameters: Optional[Union[str, Dict[str, Any]]],
    wrapped: Callable[ParametersType, ReturnType],
    instance: Any,
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    end_on_exit: bool = True,
) -> Iterator[_ToolContext]:
    tool_name = name or _infer_span_name(instance=instance, callable=wrapped)
    bound_args = inspect.signature(wrapped).bind(*args, **kwargs)
    bound_args.apply_defaults()
    arguments = bound_args.arguments
    input_attributes = get_input_attributes(arguments)
    tool_description = description or _infer_tool_description(wrapped)
    tool_parameters = parameters or _infer_tool_parameters(
        callable=wrapped,
        tool_name=tool_name,
        tool_description=tool_description,
    )
    tool_attributes = get_tool_attributes(
        name=tool_name,
        description=tool_description,
        parameters=tool_parameters,
    )
    with tracer.start_as_current_span(
        tool_name,
        fi_span_kind=FiSpanKindValues.TOOL,
        attributes={
            **input_attributes,
            **tool_attributes,
        },
        end_on_exit=end_on_exit,
    ) as span:
        context = _ToolContext(span=span)
        yield context


_DecoratorContext = Union[_ChainContext, _ToolContext]


@contextmanager
def _generator_context(
    manager: AbstractContextManager[_DecoratorContext],
) -> Iterator[_DecoratorContext]:
    # Start lazily on first consumption, under the consumer's parent. Detach
    # immediately: an OTel context token must never span a yield (or two tasks).
    with manager as context:
        pass
    span = context._span
    try:
        yield context
    except GeneratorExit:
        # Explicit early close is not a failed operation.
        raise
    except BaseException as exc:
        # use_span only handles Exception; cancellation is a BaseException.
        # Record once through FiSpan so error privacy applies on every path.
        span.record_exception(exc, escaped=True)
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        raise
    finally:
        span.end()


def _trace_generator(
    generator: Any,
    manager: AbstractContextManager[_DecoratorContext],
) -> Union[Generator[Any, Any, Any], AsyncGenerator[Any, Any]]:
    if inspect.isasyncgen(generator):
        return _trace_async_generator(generator, manager)
    return _trace_sync_generator(generator, manager)


def _trace_sync_generator(
    generator: Generator[Any, Any, Any],
    manager: AbstractContextManager[_DecoratorContext],
) -> Generator[Any, Any, Any]:
    with _generator_context(manager) as context:
        operation: Callable[[], Any] = generator.__next__
        while True:
            try:
                with use_span(
                    context._span, record_exception=False, set_status_on_exception=False
                ):
                    output = operation()
            except StopIteration as stopped:
                # Do not accumulate chunks: streams can be unbounded. Preserve
                # an explicit return value or output set by the application.
                if stopped.value is not None:
                    context.process_output(stopped.value)
                return stopped.value
            try:
                sent = yield output
            except GeneratorExit:
                with use_span(
                    context._span, record_exception=False, set_status_on_exception=False
                ):
                    generator.close()
                raise
            except BaseException as exc:
                operation = partial(generator.throw, exc)
            else:
                operation = partial(generator.send, sent)


async def _trace_async_generator(
    generator: AsyncGenerator[Any, Any],
    manager: AbstractContextManager[_DecoratorContext],
) -> AsyncGenerator[Any, Any]:
    with _generator_context(manager) as context:
        operation: Callable[[], Awaitable[Any]] = generator.__anext__
        while True:
            try:
                with use_span(
                    context._span, record_exception=False, set_status_on_exception=False
                ):
                    output = await operation()
            except StopAsyncIteration:
                return
            try:
                sent = yield output
            except GeneratorExit:
                with use_span(
                    context._span, record_exception=False, set_status_on_exception=False
                ):
                    await generator.aclose()
                raise
            except BaseException as exc:
                operation = partial(generator.athrow, exc)
            else:
                operation = partial(generator.asend, sent)


def _infer_span_name(*, instance: Any, callable: Callable[..., Any]) -> str:
    """
    Makes a best-effort attempt to infer a span name from the bound instance
    (e.g., self or cls) and the callable (the function or method being wrapped).
    Handles functions, methods, and class methods.
    """

    if inspect.ismethod(callable):
        is_class_method = isinstance(instance, type)
        if is_class_method:
            class_name = instance.__name__
        else:  # regular method
            class_name = instance.__class__.__name__
        method_name = callable.__name__
        return f"{class_name}.{method_name}"
    function_name = callable.__name__
    return function_name


def _infer_tool_description(callable: Callable[..., Any]) -> Optional[str]:
    """
    Infers a tool description from the callable's docstring if one exists.
    """
    docstring = callable.__doc__
    if docstring is not None and (stripped_docstring := docstring.strip()):
        return stripped_docstring
    return None


def _infer_tool_parameters(
    *,
    callable: Callable[..., Any],
    tool_name: str,
    tool_description: Optional[str],
) -> Dict[str, Any]:
    json_schema: Dict[str, Any] = {"type": "object", "title": tool_name}
    if tool_description:
        json_schema["description"] = tool_description
    properties = {}
    required_properties = []
    signature = inspect.signature(callable)
    for param_name, param in signature.parameters.items():
        property_data = {}
        default_value = param.default
        has_default = default_value is not inspect.Parameter.empty
        if has_default:
            property_data["default"] = default_value
        if not has_default:
            required_properties.append(param_name)
        annotation = param.annotation
        property_data.update(_get_jsonschema_type(annotation))
        metadata = getattr(annotation, "__metadata__", None)
        description: Optional[str] = None
        if metadata and isinstance(first_metadata := metadata[0], str):
            description = first_metadata
        if description:
            property_data["description"] = description
        properties[param_name] = property_data

    json_schema["properties"] = properties
    if required_properties:
        json_schema["required"] = required_properties
    return json_schema


def _get_jsonschema_type(annotation_type: type) -> Dict[str, Any]:
    if isinstance(annotation_type, _AnnotatedAlias):
        annotation_type = annotation_type.__args__[0]
        return _get_jsonschema_type(annotation_type)
    if annotation_type is type(None) or annotation_type is None:
        return {"type": "null"}
    if annotation_type is str:
        return {"type": "string"}
    if annotation_type is int:
        return {"type": "integer"}
    if annotation_type is float:
        return {"type": "number"}
    if annotation_type is bool:
        return {"type": "boolean"}
    if annotation_type is datetime:
        return {
            "type": "string",
            "format": "date-time",
        }
    annotation_type_origin = get_origin(annotation_type)
    annotation_type_args = get_args(annotation_type)
    is_union_type = annotation_type_origin is Union
    if is_union_type:
        jsonschema_types = []
        for type_ in annotation_type_args:
            jsonschema_types.append(_get_jsonschema_type(type_))
        return {"anyOf": jsonschema_types}
    is_literal_type = annotation_type_origin is Literal
    if is_literal_type:
        enum_values = list(annotation_type_args)
        unique_enum_types = dict.fromkeys(type(value) for value in enum_values)
        jsonschema_types = [
            _get_jsonschema_type(value_type) for value_type in unique_enum_types
        ]
        result = {}
        if len(jsonschema_types) == 1:
            result.update(jsonschema_types[0])
        elif len(jsonschema_types) > 1:
            result["anyOf"] = jsonschema_types
        result["enum"] = enum_values
        return result
    is_list_type = (
        annotation_type_origin is list
        or annotation_type_origin is collections.abc.Sequence
    )
    if is_list_type:
        result = {"type": "array"}
        if len(annotation_type_args) == 1:
            list_item_type = annotation_type_args[0]
            result["items"] = _get_jsonschema_type(list_item_type)
        return result
    is_tuple_type = annotation_type_origin is tuple
    if is_tuple_type:
        result = {"type": "array"}
        if len(annotation_type_args) == 2 and annotation_type_args[-1] is Ellipsis:
            item_type = annotation_type_args[0]
            result["items"] = _get_jsonschema_type(item_type)
        elif annotation_type_args:
            items = []
            for arg_type in annotation_type_args:
                item_schema = _get_jsonschema_type(arg_type)
                items.append(item_schema)
            result["items"] = items
            result["minItems"] = len(annotation_type_args)
            result["maxItems"] = len(annotation_type_args)
        return result
    is_dict_type = (
        annotation_type_origin is dict
        or annotation_type_origin is collections.abc.Mapping
    )
    if is_dict_type:
        result = {"type": "object"}
        if len(annotation_type_args) == 2:
            # jsonschema requires that the keys in object type are strings, so
            # we ignore the key type
            _, value_type = annotation_type_args
            result["additionalProperties"] = _get_jsonschema_type(value_type)
        return result
    is_typed_dict_type = isinstance(annotation_type, _TypedDictMeta)
    if is_typed_dict_type:
        result = {"type": "object"}
        properties = {}
        for field_name, field_type in annotation_type.__annotations__.items():
            properties[field_name] = _get_jsonschema_type(field_type)
        result["properties"] = properties
        return result
    if (
        pydantic is not None
        and isinstance(annotation_type, type)
        and issubclass(annotation_type, pydantic.BaseModel)
    ):
        return annotation_type.schema()  # type: ignore[no-any-return,attr-defined]
    return {}


# span attributes
OUTPUT_VALUE = SpanAttributes.OUTPUT_VALUE
