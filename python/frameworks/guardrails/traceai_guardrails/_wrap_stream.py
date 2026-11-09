"""Privacy boundaries for the pre-native SDK's overriding streaming steps."""

from contextlib import contextmanager

from opentelemetry import context
from wrapt import ObjectProxy

from traceai_guardrails._wrap_guard_call import _ParseCallableWrapper


class _StreamStep(ObjectProxy):
    def __init__(self, stream, scope):
        super().__init__(stream)
        self._self_scope = scope
        self._self_started = False
        self._self_finished = False

    @contextmanager
    def _resume(self):
        # Do not hold an attached context or unfinished span across a yield.
        # In 0.5, AsyncStreamRunner.async_run does not forward aclose to its
        # inner step. A span lasting until that inner generator is finalized
        # can therefore leak (or detach in another task). Scope each resume,
        # including cleanup, instead; SDK events stay owned by the adapter,
        # while consumer code between chunks keeps its own parent and policy.
        if self._self_finished or context.get_value(context._SUPPRESS_INSTRUMENTATION_KEY):
            yield
            return
        self._self_started = True
        with self._self_scope():
            yield


class _StreamIterator(_StreamStep):
    def __iter__(self):
        return self

    def _advance(self, method, *args):
        stopped = None
        with self._resume():
            try:
                result = method(*args)
            except StopIteration as exc:
                # Normal exhaustion must not become an ERROR span. Preserve
                # the generator's return value and raise outside the span.
                stopped = exc
                self._self_finished = True
        if stopped is not None:
            raise stopped
        return result

    def __next__(self):
        return self._advance(next, self.__wrapped__)

    def send(self, value):
        return self._advance(self.__wrapped__.send, value)

    def throw(self, *args):
        return self._advance(self.__wrapped__.throw, *args)

    def close(self):
        if not self._self_started or self._self_finished:
            return self.__wrapped__.close()
        with self._resume():
            result = self.__wrapped__.close()
        self._self_finished = True
        return result


class _AsyncStreamIterator(_StreamStep):
    def __aiter__(self):
        return self

    async def _advance(self, method, *args):
        stopped = None
        with self._resume():
            try:
                result = await method(*args)
            except StopAsyncIteration as exc:
                stopped = exc
                self._self_finished = True
        if stopped is not None:
            raise stopped
        return result

    async def __anext__(self):
        return await self._advance(self.__wrapped__.__anext__)

    async def asend(self, value):
        return await self._advance(self.__wrapped__.asend, value)

    async def athrow(self, *args):
        return await self._advance(self.__wrapped__.athrow, *args)

    async def aclose(self):
        if not self._self_started or self._self_finished:
            return await self.__wrapped__.aclose()
        with self._resume():
            result = await self.__wrapped__.aclose()
        self._self_finished = True
        return result


class _StreamParseCallableWrapper(_ParseCallableWrapper):
    iterator_type = _StreamIterator

    def __call__(self, wrapped, instance, args, kwargs):
        stream = wrapped(*args, **kwargs)
        return self.iterator_type(
            stream, lambda: self._trace_step(wrapped, instance, args, kwargs)
        )


class _AsyncStreamParseCallableWrapper(_StreamParseCallableWrapper):
    # The legacy async_step is an async generator function, not a coroutine.
    iterator_type = _AsyncStreamIterator
