"""Replay-safe tracing for Temporal. Call instrument() before creating clients/workers."""

import logging
from typing import Any, Collection

import wrapt
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from temporalio import client as sdk_client
from temporalio.client import Client
from temporalio.worker import Worker

from ._interceptor import TemporalInterceptor

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


class _TracingPlugin(getattr(sdk_client, "Plugin", object)):
    """Finalize tracing after application plugins, without running them twice.

    Only SDKs with plugins use this adapter; older SDKs keep constructor setup.
    The first explicitly configured TemporalInterceptor wins. Other interceptors
    keep their order and identity, and application-owned config is never mutated.
    """

    def __init__(self, interceptor: TemporalInterceptor) -> None:
        self.interceptor = interceptor

    def configure_client(self, config: dict) -> dict:
        try:
            existing = tuple(config.get("interceptors", ()))
            # A clone can retain a default interceptor from an earlier constructor.
            # Explicit application configuration always wins over that default.
            selected = next((i for i in existing if isinstance(i, TemporalInterceptor)
                             and not i._automatic), None)
            if selected is None:
                selected = next((i for i in existing if isinstance(i, TemporalInterceptor)), self.interceptor)
            interceptors = [i for i in existing if not isinstance(i, TemporalInterceptor) or i is selected]
            # Keep only the first occurrence, even if a plugin appends the same
            # object twice. Preserve the order/identity of all other interceptors.
            found = False
            deduped = []
            for item in interceptors:
                if item is selected:
                    if found:
                        continue
                    found = True
                deduped.append(item)
            if not found:
                deduped.append(selected)
            return {**config, "interceptors": tuple(deduped)}
        except Exception:
            logger.debug("traceAI: Temporal plugin instrumentation skipped", exc_info=True)
            return config

    async def connect_service_client(self, config: Any, next: Any) -> Any:
        return await next(config)


class TemporalInstrumentor(BaseInstrumentor):
    """Append Temporal interceptors without replacing user interceptors.

    Existing clients/workers are untouched. For explicit setup, install a
    TemporalInterceptor on the client (inherited by workers) or on each worker.
    No auto-registry modifications or Temporal service RPC patches are needed.
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return ("temporalio>=1.9,<2",)

    def _instrument(self, **kwargs: Any) -> None:
        self._patches = []
        try:
            interceptor = TemporalInterceptor(kwargs.get("tracer_provider"), kwargs.get("config"))
            interceptor._automatic = True

            def client_init(wrapped: Any, instance: Any, args: Any, kw: dict) -> Any:
                updated = kw
                try:
                    plugins = tuple(kw.get("plugins", ()))
                    if plugins and hasattr(sdk_client, "Plugin"):
                        # Client.config() retains our finalizer when cloned. Move
                        # it behind ALL application plugins, including new ones;
                        # an old finalizer must not discard their explicit config.
                        plugins = tuple(p for p in plugins if not isinstance(p, _TracingPlugin))
                        updated = {**kw, "plugins": (*plugins, _TracingPlugin(interceptor))}
                    else:
                        existing = tuple(kw.get("interceptors", ()))
                        finalized = _TracingPlugin(interceptor).configure_client(kw)
                        effective = tuple(finalized.get("interceptors", ()))
                        if len(existing) != len(effective) or any(a is not b for a, b in zip(existing, effective)):
                            updated = finalized
                except Exception:
                    logger.debug("traceAI: Temporal client instrumentation skipped", exc_info=True)
                return wrapped(*args, **updated)

            def worker_init(wrapped: Any, instance: Any, args: Any, kw: dict) -> Any:
                updated = kw
                try:
                    existing = tuple(kw.get("interceptors", ()))
                    client = args[0] if args else kw.get("client")
                    inherited = ()
                    if client is not None:
                        try:
                            # SDK plugins can change interceptors after the
                            # initial config is saved. Match what Worker inherits.
                            config = client.config(active_config=True)
                        except TypeError:
                            # Older SDKs have no plugins or active_config option.
                            config = client.config()
                        inherited = tuple(config.get("interceptors", ()))
                    if not any(isinstance(i, TemporalInterceptor) for i in (*existing, *inherited)):
                        updated = {**kw, "interceptors": (*existing, interceptor)}
                except Exception:
                    logger.debug("traceAI: Temporal worker instrumentation skipped", exc_info=True)
                return wrapped(*args, **updated)

            for cls, callback in ((Client, client_init), (Worker, worker_init)):
                original = cls.__init__
                patched = wrapt.FunctionWrapper(original, callback)
                cls.__init__ = patched
                self._patches.append((cls, original, patched))
        except Exception:
            self._uninstrument()
            logger.debug("traceAI: Temporal instrumentation unavailable", exc_info=True)

    def _uninstrument(self, **kwargs: Any) -> None:
        # Existing clients/workers retain their interceptors, as with the SDK's
        # own interceptor configuration. Do not interfere with another wrapper.
        for cls, original, patched in reversed(getattr(self, "_patches", [])):
            if cls.__dict__.get("__init__") is patched:
                cls.__init__ = original
        self._patches = []
