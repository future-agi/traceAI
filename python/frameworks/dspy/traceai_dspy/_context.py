"""Keep callback context changes inside a DSPy operation's lexical scope."""

from collections.abc import Iterator
from inspect import iscoroutinefunction, isfunction
from typing import Any

import dspy
from dspy.teleprompt import teleprompt
from dspy.teleprompt.teleprompt import Teleprompter
from dspy.utils.callback import with_callbacks
from opentelemetry import context


def callback_roots() -> list[type]:
    roots = [dspy.Module, dspy.BaseLM, dspy.Adapter, dspy.Tool, Teleprompter]
    roots.extend(
        cls
        for name in ("Evaluate", "CodeInterpreter")
        if isinstance(cls := getattr(dspy, name, None), type)
    )
    return roots


def callback_classes() -> Iterator[type]:
    pending = callback_roots()
    seen = set()
    while pending:
        cls = pending.pop()
        if cls not in seen:
            seen.add(cls)
            pending.extend(cls.__subclasses__())
            yield cls


def callback_methods(cls: type) -> Iterator[tuple[str, type]]:
    """Find native boundaries, not arbitrary application methods.

    DSPy decorates different methods across versions (e.g. LM.__call__ versus
    BaseLM.__call__, and optimizer compile methods). Match its decorator rather
    than adding tracing to methods that don't participate in that lifecycle.
    """
    decorator_files = {with_callbacks.__code__.co_filename}
    # DSPy 3.x's compile deduplication wrapper holds with_callbacks in a
    # closure, rather than exposing it through the __wrapped__ chain.
    compile_decorator = getattr(teleprompt, "_with_compile_callbacks", None)
    if compile_decorator is not None:
        decorator_files.add(compile_decorator.__code__.co_filename)
    for name, method in list(vars(cls).items()):
        if isinstance(getattr(method, "_self_wrapper", None), _ContextGuard):
            continue
        candidate = method
        while isfunction(candidate) and hasattr(candidate, "__wrapped__"):
            if candidate.__code__.co_filename in decorator_files:
                wrapper = (
                    _AsyncContextGuard if iscoroutinefunction(method) else _ContextGuard
                )
                yield name, wrapper
                break
            candidate = candidate.__wrapped__


class _SubclassGuard:
    def __init__(self, original: Any, patch_class: Any) -> None:
        self._original = original
        self._patch_class = patch_class
        self._enabled = True

    def __call__(self, cls: type, **kwargs: Any) -> None:
        # Preserve DSPy's own hooks, including automatic callback decoration of
        # custom adapters and optimizers created after instrument().
        self._original.__get__(None, cls)(**kwargs)
        if self._enabled:
            self._patch_class(cls)


class _ContextGuard:
    def __init__(self, tracer: Any) -> None:
        self._enabled = True

    def __call__(self, wrapped, instance, args, kwargs):
        if not self._enabled:
            return wrapped(*args, **kwargs)
        token = context.attach(context.get_current())
        try:
            return wrapped(*args, **kwargs)
        finally:
            # DSPy calls start AND end callbacks in list order, not LIFO. An
            # application callback can therefore restore our already-ended
            # span (or vice versa). Restore only after all callbacks finish.
            context.detach(token)


class _AsyncContextGuard(_ContextGuard):
    async def __call__(self, wrapped, instance, args, kwargs):
        if not self._enabled:
            return await wrapped(*args, **kwargs)
        # Attach in the executing task, never when the coroutine is created.
        token = context.attach(context.get_current())
        try:
            return await wrapped(*args, **kwargs)
        finally:
            context.detach(token)
