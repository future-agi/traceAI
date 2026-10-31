"""Fail-open wrappers. Context is attached only while advancing the application."""

import inspect
import logging
import time
from typing import Any

import wrapt
from opentelemetry import context as context_api
from opentelemetry import trace as trace_api
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import SpanKind, Status, StatusCode

from ._attributes import OMITTED_TEXT, bounded_text

logger = logging.getLogger(__name__)
_ACTIVE = context_api.create_key("traceai-agentscope-active")
_SESSIONS = context_api.create_key("traceai-agentscope-sessions")


def current_session(kind: str, instance: Any) -> Any:
    for session in reversed(context_api.get_value(_SESSIONS) or ()):
        if session.key == (kind, id(instance)):
            return session
    return None


def _debug() -> None:
    # No exception repr/traceback: instrumentation errors may contain credentials.
    logger.debug(
        "traceAI AgentScope: telemetry operation failed; application continues"
    )


class Session:
    def __init__(
        self,
        instrumentor: Any,
        kind: str,
        name: str,
        instance: Any,
        args: tuple,
        kwargs: dict,
    ) -> None:
        self.attributes = instrumentor._attributes
        self.kind = kind
        self.span = None
        self.context = context_api.get_current()
        self.output = {}
        self.partial_text = ""
        self.failed = False
        self.done = False
        self.parent = None
        self.children = set()
        self.key = (kind, id(instance))
        try:
            self.span = instrumentor._tracer.start_span(
                name,
                kind=SpanKind.CLIENT if kind == "LLM" else SpanKind.INTERNAL,
                fi_span_kind=kind.lower(),
            )
            self.context = trace_api.set_span_in_context(self.span, self.context)
            active = context_api.get_value(_ACTIVE) or frozenset()
            self.context = context_api.set_value(
                _ACTIVE, active | {self.key}, self.context
            )
            if kind == "LLM":
                # Same marker used by the wire layer. Unlike checking just the current
                # span kind, this survives an intermediate SDK-native CLIENT span.
                from fi_instrumentation.auto.wire._capture import _WIRE_ACTIVE_KEY

                self.context = context_api.set_value(
                    _WIRE_ACTIVE_KEY, True, self.context
                )
                # The framework owns this model operation. Provider instrumentors
                # (including traceai_openai) honour standard OTel suppression,
                # unlike the wire-only marker. Scope it to model advancement so
                # sibling tool/agent calls and unrelated SDK calls remain traced.
                self.context = context_api.set_value(
                    _SUPPRESS_INSTRUMENTATION_KEY, True, self.context
                )
            parents = context_api.get_value(_SESSIONS) or ()
            if parents and not parents[-1].done:
                self.parent = parents[-1]
                self.parent.children.add(self)
            self.context = context_api.set_value(
                _SESSIONS, (*parents, self), self.context
            )
            # S5 transport hooks learn credentials even while this model's wire
            # span is suppressed. The core export boundary scrubs every span;
            # sessions keep lifecycle/parentage only, never credential metadata.
            attrs = self.attributes.request(kind, instance, args, kwargs)
            self.span.set_attributes(self.attributes.clean(attrs))
        except Exception:
            _debug()

    def observe(self, value: Any) -> None:
        if self.span is None or self.done:
            return
        try:
            attrs = self.attributes.response(value, self.kind)
            # AgentScope 2 streams deltas, then a complete is_last=True snapshot.
            # AgentScope 1 streams cumulative snapshots. Never concatenate those.
            if self.kind == "LLM" and self.attributes.field(value, "is_last") is False:
                if self.partial_text != OMITTED_TEXT:
                    self.partial_text = bounded_text(
                        self.partial_text + attrs.get("output.value", "")
                    )
                if self.partial_text:
                    attrs["output.value"] = self.partial_text
                    attrs["gen_ai.output.messages.0.message.content"] = (
                        self.partial_text
                    )
            self.output.update(attrs)
            state = self.attributes.label(self.attributes.field(value, "state"))
            if (
                state == "error"
                or self.attributes.field(value, "is_error") is True
                or self.attributes.label(
                    self.attributes.field(value, "finished_reason")
                )
                == "error"
            ):
                self.failed = True
        except Exception:
            _debug()

    def finish(
        self, error: Any = None, incomplete: bool = False, end_time: Any = None
    ) -> None:
        if self.done:
            return
        self.done = True
        if incomplete:
            # Native async-for delegation need not close its suspended iterator
            # when a public stream is closed. End our descendant telemetry at the
            # same observed boundary without driving/closing application streams.
            for child in tuple(self.children):
                child.finish(error, incomplete=True, end_time=end_time)
        if self.parent is not None:
            self.parent.children.discard(self)
            self.parent = None
        if self.span is None:
            return
        try:
            self.span.set_attributes(self.attributes.clean(self.output))
            if incomplete:
                self.span.set_attribute("traceai.stream.incomplete", True)
            if error is not None:
                error_type = (
                    type.__getattribute__(type(error), "__module__")
                    + "."
                    + type.__getattribute__(type(error), "__qualname__")
                )
                self.span.set_attribute("error.type", error_type)
                self.span.add_event(
                    "exception",
                    {"exception.type": error_type, "exception.escaped": "True"},
                )
            if error is not None or self.failed:
                if error is None:
                    self.span.set_attribute(
                        "error.type",
                        (
                            "agentscope.tool.ToolError"
                            if self.kind == "TOOL"
                            else "agentscope.agent.AgentError"
                        ),
                    )
                self.span.set_status(Status(StatusCode.ERROR))
            else:
                self.span.set_status(Status(StatusCode.OK))
        except Exception:
            _debug()
        finally:
            try:
                self.span.end(end_time=end_time)
            except Exception:
                _debug()

    def attach(self) -> Any:
        try:
            return context_api.attach(self.context)
        except Exception:
            _debug()
            return None

    @staticmethod
    def detach(token: Any) -> None:
        if token is not None:
            try:
                context_api.detach(token)
            except Exception:
                _debug()


class AsyncStream(wrapt.ObjectProxy):
    """Forward the entire async iterator protocol, including send/throw/close.

    Never read ahead or hold an OTel token across a yield/task boundary. The
    wrapped iterator and its yielded objects remain untouched.
    """

    def __init__(self, stream: Any, session: Any) -> None:
        super().__init__(stream)
        self._self_session = session

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> Any:
        return await self._advance(self.__wrapped__.__anext__)

    async def asend(self, value: Any) -> Any:
        return await self._advance(self.__wrapped__.asend, value)

    async def athrow(self, *args: Any) -> Any:
        return await self._advance(self.__wrapped__.athrow, *args)

    async def aclose(self) -> Any:
        session = self._self_session
        if callable(session) or session is None:
            # A never-started async-generator function has not executed any code.
            return await self.__wrapped__.aclose()
        token = session.attach()
        try:
            result = await self.__wrapped__.aclose()
        except BaseException as error:
            session.finish(error, incomplete=True, end_time=time.time_ns())
            raise
        else:
            session.finish(incomplete=True, end_time=time.time_ns())
            return result
        finally:
            session.detach(token)

    async def _advance(self, method: Any, *args: Any) -> Any:
        session = self._self_session
        if callable(session):
            session = self._self_session = session()
        if session is None:
            return await method(*args)
        token = session.attach()
        try:
            value = await method(*args)
        except StopAsyncIteration:
            session.finish(end_time=time.time_ns())
            raise
        except BaseException as error:
            session.finish(error, end_time=time.time_ns())
            raise
        else:
            session.observe(value)
            return value
        finally:
            session.detach(token)


def skip(instrumentor: Any, kind: str, instance: Any) -> bool:
    return (
        not instrumentor._enabled
        or bool(context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY))
        or (
            instance is not None
            and (kind, id(instance)) in (context_api.get_value(_ACTIVE) or ())
        )
    )


def wrapper(instrumentor: Any, kind: str, name: str, function: Any) -> Any:
    def new_session(instance: Any, args: tuple, kwargs: dict) -> Session:
        return Session(instrumentor, kind, name, instance, args, kwargs)

    if inspect.iscoroutinefunction(function):

        async def async_wrapper(
            wrapped: Any, instance: Any, args: tuple, kwargs: dict
        ) -> Any:
            if skip(instrumentor, kind, instance):
                return await wrapped(*args, **kwargs)
            session = new_session(instance, args, kwargs)
            token = session.attach()
            try:
                result = await wrapped(*args, **kwargs)
            except BaseException as error:
                session.finish(error, end_time=time.time_ns())
                raise
            else:
                end_time = time.time_ns()
                if inspect.isasyncgen(result):
                    return AsyncStream(result, session)
                session.observe(result)
                session.finish(end_time=end_time)
                return result
            finally:
                session.detach(token)

        return async_wrapper

    def generator_wrapper(
        wrapped: Any, instance: Any, args: tuple, kwargs: dict
    ) -> Any:
        result = wrapped(*args, **kwargs)
        if skip(instrumentor, kind, instance) or not inspect.isasyncgen(result):
            return result
        return AsyncStream(
            result,
            lambda: (
                None
                if skip(instrumentor, kind, instance)
                else new_session(instance, args, kwargs)
            ),
        )

    return generator_wrapper
