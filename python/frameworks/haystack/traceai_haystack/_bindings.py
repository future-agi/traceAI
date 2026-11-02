"""Bind component metadata at public pipeline entry points, without creating spans.

Haystack's public Tracer receives component names and socket types, but not the
component instance (and therefore not embedder models or prompt templates).
Only these public entry points are wrapped: execution, timing and parenting all
belong to Haystack's native tracing. Component methods and private execution
hooks are never patched.
"""

from contextvars import ContextVar
from inspect import isasyncgenfunction, iscoroutinefunction
from typing import Any

from wrapt import FunctionWrapper

pipeline_context: ContextVar[Any] = ContextVar(
    "traceai_haystack_pipeline", default=None
)


def bind_pipeline(method: Any) -> Any:
    if isasyncgenfunction(method):

        async def generator(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
            token = pipeline_context.set(instance)
            stream = wrapped(*args, **kwargs)
            try:
                async for output in stream:
                    yield output
            finally:
                try:
                    await stream.aclose()
                finally:
                    pipeline_context.reset(token)

        return FunctionWrapper(method, generator)

    if iscoroutinefunction(method):

        async def coroutine(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
            token = pipeline_context.set(instance)
            try:
                return await wrapped(*args, **kwargs)
            finally:
                pipeline_context.reset(token)

        return FunctionWrapper(method, coroutine)

    def synchronous(wrapped: Any, instance: Any, args: Any, kwargs: Any) -> Any:
        token = pipeline_context.set(instance)
        try:
            return wrapped(*args, **kwargs)
        finally:
            pipeline_context.reset(token)

    return FunctionWrapper(method, synchronous)
