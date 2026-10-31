"""AgentScope 1.x/2.x lifecycle integration without configuring global OTel."""

import inspect
import logging
import weakref
from collections.abc import Collection
from importlib import import_module
from typing import Any

import wrapt
from fi_instrumentation import FITracer, TraceConfig
from opentelemetry import trace as trace_api
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor

from ._attributes import Attributes
from ._wrappers import AsyncStream, Session, _debug, current_session, skip, wrapper
from .version import __version__

logger = logging.getLogger(__name__)
_MISSING = object()


class AgentScopeInstrumentor(BaseInstrumentor):
    def instrumentation_dependencies(self) -> Collection[str]:
        return ("agentscope>=1.0.21,<3",)

    def _instrument(self, **kwargs: Any) -> None:
        self._enabled = False
        self._patches = []
        self._agents = weakref.WeakSet()
        self._middleware = None
        try:
            config = kwargs.get("config")
            if config is None:
                config = TraceConfig()
            if not isinstance(config, TraceConfig):
                logger.warning(
                    "traceAI AgentScope: invalid TraceConfig; instrumentation disabled"
                )
                return
            self._tracer = FITracer(
                trace_api.get_tracer(
                    __name__, __version__, kwargs.get("tracer_provider")
                ),
                config=config,
            )
            self._attributes = Attributes(config)
            agents = import_module("agentscope.agent")
            models = import_module("agentscope.model")
            tools = import_module("agentscope.tool")
            pipelines = import_module("agentscope.pipeline")
            self._enabled = True
            if hasattr(agents, "Agent"):
                self._setup_middleware(agents.Agent)
                self._patch(tools.Toolkit, "call_tool", "TOOL")
                for name in ("GoalPipeline", "TeamPipeline"):
                    self._patch(getattr(pipelines, name), "reply_stream", "CHAIN")
            else:
                # 1.x pre/post hooks do not run post hooks on exceptions or
                # cancellation; wrapping reply is necessary for leak-free spans.
                self._patch_tree(agents.AgentBase, "reply", "AGENT")
                self._watch_subclasses(agents.AgentBase, "reply", "AGENT")
                self._patch(tools.Toolkit, "call_tool_function", "TOOL")
                # Include already-imported aliases used by pipeline classes.
                for module in (
                    pipelines,
                    import_module("agentscope.pipeline._functional"),
                    import_module("agentscope.pipeline._class"),
                ):
                    for name in ("sequential_pipeline", "fanout_pipeline"):
                        self._patch(module, name, "CHAIN")
            self._patch_tree(models.ChatModelBase, "__call__", "LLM")
            self._watch_subclasses(models.ChatModelBase, "__call__", "LLM")
        except Exception:
            self._uninstrument()
            logger.warning(
                "traceAI AgentScope: unsupported API; instrumentation disabled"
            )

    def _patch(self, owner: Any, name: str, kind: str) -> None:
        original = vars(owner).get(name, _MISSING)
        if original is _MISSING:
            return
        if any(o is owner and n == name for o, n, _, _ in self._patches):
            return
        span_name = f"agentscope.{owner.__name__.split('.')[-1]}.{name}"
        patched = wrapt.FunctionWrapper(
            original, wrapper(self, kind, span_name, original)
        )
        self._replace(owner, name, patched)

    def _replace(self, owner: Any, name: str, patched: Any) -> None:
        original = vars(owner).get(name, _MISSING)
        setattr(owner, name, patched)
        self._patches.append((owner, name, original, patched))

    def _patch_tree(self, base: type, name: str, kind: str) -> None:
        self._patch(base, name, kind)
        for subclass in base.__subclasses__():
            self._patch_tree(subclass, name, kind)

    def _watch_subclasses(self, base: type, name: str, kind: str) -> None:
        original = vars(base).get("__init_subclass__", _MISSING)
        instrumentor = self

        def init_subclass(subclass: type, **kwargs: Any) -> None:
            if original is _MISSING:
                super(base, subclass).__init_subclass__(**kwargs)
            else:
                original.__get__(None, subclass)(**kwargs)
            if instrumentor._enabled:
                try:
                    instrumentor._patch(subclass, name, kind)
                except Exception:
                    _debug()

        self._replace(base, "__init_subclass__", classmethod(init_subclass))

    def _setup_middleware(self, agent_class: type) -> None:
        from agentscope.middleware import MiddlewareBase

        instrumentor = self

        class FIMiddleware(MiddlewareBase):
            def on_reply(
                self, agent: Any, input_kwargs: dict, next_handler: Any
            ) -> Any:
                stream = next_handler(**input_kwargs)
                if skip(instrumentor, "AGENT", agent):
                    session = current_session("AGENT", agent)
                    if session is None:
                        return stream

                    async def observe():
                        # Public reply_stream may filter the final Msg. Observe
                        # it here, without owning/ending the public span twice.
                        async for item in stream:
                            session.observe(item)
                            yield item

                    return observe()
                return AsyncStream(
                    stream,
                    lambda: (
                        None
                        if skip(instrumentor, "AGENT", agent)
                        else Session(
                            instrumentor,
                            "AGENT",
                            "agentscope.Agent.reply",
                            agent,
                            (),
                            input_kwargs,
                        )
                    ),
                )

        self._middleware = FIMiddleware()

        def ensure_middleware(agent: Any) -> None:
            if not instrumentor._enabled:
                return
            try:
                # AgentScope stores the public middleware protocol by hook. Add
                # only our own hook, including on agents created before instrument().
                hooks = agent._reply_middlewares
                if not any(item is instrumentor._middleware for item in hooks):
                    hooks.insert(0, instrumentor._middleware)
                    instrumentor._agents.add(agent)
            except Exception:
                _debug()

        for name in ("reply", "reply_stream"):
            original = vars(agent_class)[name]
            if inspect.iscoroutinefunction(original):

                async def reply(
                    wrapped: Any, instance: Any, args: tuple, kwargs: dict
                ) -> Any:
                    ensure_middleware(instance)
                    return await wrapped(*args, **kwargs)

                patched = wrapt.FunctionWrapper(original, reply)
            else:
                # A native async-for middleware chain does not guarantee closing
                # its delegated iterator on public aclose(). Own this public
                # stream's lifetime; the native hook observes the active marker
                # and transparently delegates without creating another span.
                stream_wrapper = wrapper(
                    self, "AGENT", "agentscope.Agent.reply_stream", original
                )

                def reply_stream(
                    wrapped: Any, instance: Any, args: tuple, kwargs: dict
                ) -> Any:
                    ensure_middleware(instance)
                    return stream_wrapper(wrapped, instance, args, kwargs)

                patched = wrapt.FunctionWrapper(original, reply_stream)
            self._replace(agent_class, name, patched)

    def _uninstrument(self, **kwargs: Any) -> None:
        self._enabled = False
        for agent in list(getattr(self, "_agents", ())):
            try:
                agent._reply_middlewares[:] = [
                    item
                    for item in agent._reply_middlewares
                    if item is not self._middleware
                ]
            except Exception:
                _debug()
        for owner, name, original, patched in reversed(getattr(self, "_patches", ())):
            # Do not clobber another instrumentor's patch installed after ours.
            if vars(owner).get(name) is not patched:
                continue
            if original is _MISSING:
                delattr(owner, name)
            else:
                setattr(owner, name, original)
        self._patches = []
        self._agents = weakref.WeakSet()
