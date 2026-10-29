"""Shared builders and span utilities for the LangGraph tracing regression suite.

These helpers build *real* LangGraph graphs (no mocks) so the tests exercise the
same code paths the customer hits. Graphs must be built AFTER instrumentation is
enabled, so builders are plain functions the tests call inside an instrumented
fixture — not module-level singletons.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import START, END, StateGraph
from langgraph.graph.message import add_messages
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import var_child_runnable_config


def _interrupt(value):
    """Use the installed SDK's genuine control-flow API in each supported era."""
    try:
        from langgraph.types import interrupt
    except ImportError:
        # 0.2.0 exposes GraphInterrupt but predates dynamic interrupt()/Command.
        from langgraph.errors import GraphInterrupt
        raise GraphInterrupt(value)
    return interrupt(value)


class State(TypedDict):
    messages: Annotated[list, add_messages]
    value: int


# A minimal, deterministic input for every graph below.
INPUT: dict = {"messages": [], "value": 1}


# --------------------------------------------------------------------------- #
# Graph builders
# --------------------------------------------------------------------------- #
def build_sync_graph():
    """Single sync node named 'agent'."""
    def agent(state: State):
        return {"value": state["value"] + 1}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g.compile()


def build_async_graph():
    """Single async node named 'agent' (the customer's pattern)."""
    async def agent(state: State):
        await asyncio.sleep(0)
        return {"value": state["value"] + 1}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g.compile()


def build_barrier_async_graph(enter: asyncio.Event, release: asyncio.Event):
    """Async node that signals `enter` then parks on `release` — lets a test
    force a deterministic interleave of two concurrent invocations."""
    async def agent(state: State):
        enter.set()
        await release.wait()
        return {"value": state["value"] + 1}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g.compile()


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def build_tool_graph():
    """seed -> ToolNode('tools'): the tool node runs `add` from a tool_call."""
    from langgraph.prebuilt import ToolNode

    def seed(state: State):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "add", "args": {"a": 1, "b": 2}, "id": "c1", "type": "tool_call"}
                    ],
                )
            ]
        }

    g = StateGraph(State)
    g.add_node("seed", seed)
    g.add_node("tools", ToolNode([add]))
    g.add_edge(START, "seed")
    g.add_edge("seed", "tools")
    g.add_edge("tools", END)
    return g.compile()


def build_interrupt_graph():
    """HITL node that calls interrupt() — requires a checkpointer + thread_id."""
    from langgraph.checkpoint.memory import MemorySaver

    def ask_human(state: State):
        _interrupt({"q": "approve?"})
        return {"value": 0}

    g = StateGraph(State)
    g.add_node("ask_human", ask_human)
    g.add_edge(START, "ask_human")
    g.add_edge("ask_human", END)
    return g.compile(checkpointer=MemorySaver())


def build_error_graph():
    """A node that raises a genuine exception (not a control-flow interrupt)."""
    def boom(state: State):
        raise ValueError("boom")

    g = StateGraph(State)
    g.add_node("boom", boom)
    g.add_edge(START, "boom")
    g.add_edge("boom", END)
    return g.compile()


def build_async_interrupt_graph():
    """Async HITL node calling interrupt() — the customer's actual pattern."""
    from langgraph.checkpoint.memory import MemorySaver

    async def ask_human(state: State, config: RunnableConfig):
        # Python 3.10 tasks cannot accept a context= argument. Propagate the
        # explicitly injected runnable config while executing this node, rather
        # than relying on Python 3.11-only implicit task-context propagation.
        token = var_child_runnable_config.set(config)
        try:
            await asyncio.sleep(0)
            _interrupt({"q": "approve?"})
            return {"value": 0}
        finally:
            var_child_runnable_config.reset(token)

    g = StateGraph(State)
    g.add_node("ask_human", ask_human)
    g.add_edge(START, "ask_human")
    g.add_edge("ask_human", END)
    return g.compile(checkpointer=MemorySaver())


def build_llm_node_graph():
    """A node 'agent' that calls an LLM, producing a nested LLM child run."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    def agent(state: State):
        model = GenericFakeChatModel(messages=iter([AIMessage(content="hi")]))
        model.invoke("hello")
        return {"value": state["value"] + 1}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g.compile()


def build_tool_interrupt_graph():
    """seed -> ToolNode whose tool calls interrupt() (LangGraph HITL-approval pattern)."""
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.prebuilt import ToolNode

    @tool
    def ask_approval(item: str) -> str:
        """Human-in-the-loop approval tool."""
        _interrupt({"q": item})
        return "approved"

    def seed(state: State):
        return {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "ask_approval", "args": {"item": "x"}, "id": "c1", "type": "tool_call"}
                    ],
                )
            ]
        }

    g = StateGraph(State)
    g.add_node("seed", seed)
    g.add_node("tools", ToolNode([ask_approval], handle_tool_errors=False))
    g.add_edge(START, "seed")
    g.add_edge("seed", "tools")
    g.add_edge("tools", END)
    return g.compile(checkpointer=MemorySaver())


def build_command_handoff_graph():
    """route -> Command(goto='done'): a ParentCommand control-flow bubble-up (not HITL)."""
    try:
        from langgraph.types import Command
    except ImportError:
        Command = None

    def route(state: State):
        return Command(goto="done") if Command is not None else {"value": state["value"]}

    def done(state: State):
        return {"value": 9}

    g = StateGraph(State)
    g.add_node("route", route)
    g.add_node("done", done)
    g.add_edge(START, "route")
    if Command is None:
        # Historical routing used conditional edges, before Command existed.
        g.add_conditional_edges("route", lambda _: "done")
    g.add_edge("done", END)
    return g.compile()


def build_store_graph():
    """Node whose signature requests LangGraph's injected `store`."""
    from langgraph.store.memory import InMemoryStore

    def agent(state: State, *, store):
        return {"value": state["value"] + 1}

    g = StateGraph(State)
    g.add_node("agent", agent)
    g.add_edge(START, "agent")
    g.add_edge("agent", END)
    return g.compile(store=InMemoryStore())


# --------------------------------------------------------------------------- #
# Span utilities
# --------------------------------------------------------------------------- #
GRAPH_NODE_NAME_ATTR = "gen_ai.agent.graph.node_name"
SPAN_KIND_ATTR = "gen_ai.span.kind"


def span_names(exporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def spans_named(exporter, name: str) -> list:
    return [s for s in exporter.get_finished_spans() if s.name == name]


def span_kinds(exporter) -> list[str]:
    return [(s.attributes or {}).get(SPAN_KIND_ATTR) for s in exporter.get_finished_spans()]


def attr(span, key, default=None):
    return (span.attributes or {}).get(key, default)
