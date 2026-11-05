"""Context handoff for CrewAI's legacy Task thread target."""

from contextvars import copy_context
from functools import wraps
from typing import Any


class _ContextPropagatingMethod:
    """Capture at method binding, before execute_async starts its worker thread.

    Wrapping the worker's invocation is too late: it already runs in an empty
    context. A descriptor captures when CrewAI reads ``self._execute_task_async``
    for Thread's target, without replacing execute_async, mutating the Task, or
    patching threading. Each binding owns its snapshot (including suppression).
    """

    def __init__(self, original: Any) -> None:
        self.__wrapped__ = original

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        bound = self.__wrapped__.__get__(instance, owner)
        if instance is None:
            return bound
        context = copy_context()

        @wraps(bound)
        def run(*args: Any, **kwargs: Any) -> Any:
            # A bound target can be reused concurrently; never enter the same
            # Context twice or let one invocation contaminate the next.
            return context.copy().run(bound, *args, **kwargs)

        return run
