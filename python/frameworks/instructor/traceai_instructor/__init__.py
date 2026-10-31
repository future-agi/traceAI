from importlib import import_module
from inspect import getattr_static, signature
from typing import Any, Collection

from fi_instrumentation import FITracer, TraceConfig
from fi_instrumentation.instrumentation._protect_wrapper import GuardrailProtectWrapper
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from wrapt import FunctionWrapper

from traceai_instructor._wrappers import _FactoryWrapper, _RequestWrapper, _remove_wrapper
from traceai_instructor.version import __version__

try:
    from fi.evals import Protect
except ImportError:
    Protect = None

_instruments = ("instructor >= 1.0.0",)


class _OwnedProtectWrapper(GuardrailProtectWrapper):
    """A later ordinary decorator can close over our hook permanently."""

    def __init__(self, tracer):
        super().__init__(tracer)
        self.enabled = True

    def __call__(self, wrapped, instance, args, kwargs):
        if not self.enabled:
            return wrapped(*args, **kwargs)
        return super().__call__(wrapped, instance, args, kwargs)


class InstructorInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs: Any) -> None:
        tracer_provider = kwargs.get("tracer_provider") or trace_api.get_tracer_provider()
        config = kwargs.get("config") or TraceConfig()
        assert isinstance(config, TraceConfig)
        self._tracer = FITracer(
            trace_api.get_tracer(__name__, __version__, tracer_provider), config=config,
        )
        self._hooks = []
        self._callbacks = []
        module = import_module("instructor")
        sync_class = module.Instructor
        async_class = getattr(module, "AsyncInstructor", None)
        if async_class is None:
            async_class = import_module("instructor.client").AsyncInstructor
        hooks_class = None
        for module_name in ("instructor.core.hooks", "instructor.hooks"):
            try:
                hooks_class = import_module(module_name).Hooks
                break
            except ImportError:
                pass  # Instructor 1.0 has no public hook API.

        for cls in (sync_class, async_class):
            for name in ("create", "create_with_completion", "create_partial", "create_iterable"):
                if hasattr(cls, name):
                    supports_hooks = "hooks" in signature(getattr(cls, name)).parameters
                    callback = _RequestWrapper(
                        self._tracer, hooks_class if supports_hooks else None,
                        name=f"instructor.{name}",
                    )
                    self._callbacks.append(callback)
                    self._wrap(cls, name, callback)

        generated = _RequestWrapper(self._tracer, hooks_class)
        self._callbacks.append(generated)
        # v2 factories also cover patch imports captured before instrumentation,
        # from_provider(), and the provider adapters that bypass the root alias.
        try:
            patch_module = import_module("instructor.v2.core.patch")
        except ImportError:
            patch_module = None
        if patch_module is not None and all(
            hasattr(patch_module, name) for name in ("_create_sync_wrapper", "_create_async_wrapper")
        ):
            for name in ("_create_sync_wrapper", "_create_async_wrapper"):
                self._wrap(patch_module, name, _FactoryWrapper(generated))
        else:
            # Older Instructor generates callables inside patch(). Keep its
            # public entry point working, without a client-construction span.
            patch = module.patch
            canonical = import_module(patch.__module__)
            for owner in (module, canonical):
                self._wrap(owner, "patch", _FactoryWrapper(generated, legacy=True))

        self._protect_owner = Protect
        self._protect_hook = None
        if Protect is not None:
            self._protect_callback = _OwnedProtectWrapper(tracer=self._tracer)
            self._callbacks.append(self._protect_callback)
            self._protect_hook = self._wrap(Protect, "protect", self._protect_callback)

    def _wrap(self, owner, name, callback):
        hook = FunctionWrapper(getattr_static(owner, name), callback)
        setattr(owner, name, hook)
        self._hooks.append((owner, name, hook))
        return hook

    def _uninstrument(self, **kwargs: Any) -> None:
        # Generated callables and ordinary decorators can outlive this setup.
        # Disable their callback before detaching our owned dispatch links.
        for callback in self._callbacks:
            callback.enabled = False
        for owner, name, hook in reversed(self._hooks):
            _remove_wrapper(owner, name, hook)
        self._hooks.clear()
        self._callbacks.clear()
        self._protect_hook = None
        self._protect_owner = None
