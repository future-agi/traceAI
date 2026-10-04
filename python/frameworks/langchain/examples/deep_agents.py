"""Trace a LangChain Deep Agents run with traceAI-langchain.

Deep Agents (``deepagents``) builds a LangGraph graph with ``create_deep_agent``.
There is no Deep Agents instrumentor and no ``traceai-deepagents`` package: the
existing ``LangChainInstrumentor`` traces the graph, its model calls, its
built-in tools and its ``task`` subagents.

Tested with ``deepagents==0.7.21`` (see the "Deep Agents" section of the
package README for the exact versions the compatibility test printed).

This script runs offline. It uses a scripted fake chat model that returns
``usage_metadata``, so no vendor key is needed and no vendor is called. To trace
a real agent, replace ``build_model()`` with your chat model (for example
``init_chat_model("openai:gpt-5.5")``). Always pass a model: ``model=None``
selects Deep Agents' deprecated ``claude-sonnet-4-6`` default.

Run::

    pip install traceAI-langchain "deepagents==0.7.21" langgraph
    export FI_API_KEY="YOUR_API_KEY"
    export FI_SECRET_KEY="YOUR_SECRET_KEY"
    export FI_PROJECT_NAME="deep-agents-cookbook"
    python deep_agents.py

``tests/test_deepagents_compat.py`` runs this same script against a loopback
receiver.
"""

import os
from typing import Any, Optional, Sequence

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from traceai_langchain import LangChainInstrumentor

THREAD_ID = "t-1"
"""LangGraph ``configurable.thread_id``. traceai-langchain copies it to ``session.id``."""

NOTE_PATH = "/notes.txt"
NOTE_CONTENT = "trip notes: confidential-7f3a"
"""File content the agent writes. Hidden from spans by the TraceConfig below."""


class ScriptedToolChatModel(GenericFakeChatModel):
    """Offline stand-in for a tool-calling chat model.

    Replays ``messages`` in order, ignores the prompt, and accepts
    ``bind_tools`` so ``create_deep_agent`` can attach its tools. Each scripted
    ``AIMessage`` carries ``usage_metadata`` so the LLM spans have token counts.
    """

    model_name: str = "scripted-deep-agent-model"
    # Always answer through _generate: GenericFakeChatModel's streaming path
    # drops tool calls and usage_metadata.
    disable_streaming: bool = True
    # Tool names create_deep_agent bound, one list per model call (read by the test).
    bound_tool_names: list[list[str]] = []

    @property
    def _llm_type(self) -> str:
        return "scripted-tool-chat"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model_name": self.model_name}

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedToolChatModel":
        self.bound_tool_names.append([getattr(t, "name", None) or t.get("name") for t in tools])
        return self


def _usage(input_tokens: int, output_tokens: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _call(name: str, args: dict[str, Any], call_id: str, usage: dict[str, int]) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
        usage_metadata=usage,
    )


def scripted_turns() -> list[AIMessage]:
    """The model turns of one run, in call order.

    Main agent: write_file, read_file, the custom tool, then ``task``. The
    ``general-purpose`` subagent (same model) calls ``ls`` and answers. The main
    agent then answers. One tool call per turn keeps the order deterministic.
    """
    return [
        _call("write_file", {"file_path": NOTE_PATH, "content": NOTE_CONTENT}, "call_1", _usage(11, 3)),
        _call("read_file", {"file_path": NOTE_PATH}, "call_2", _usage(13, 4)),
        _call("lookup_weather", {"city": "Paris"}, "call_3", _usage(17, 5)),
        _call(
            "task",
            {"description": "List the workspace files.", "subagent_type": "general-purpose"},
            "call_4",
            _usage(19, 6),
        ),
        # general-purpose subagent
        _call("ls", {"path": "/"}, "call_5", _usage(23, 7)),
        AIMessage(content="The workspace has /notes.txt.", usage_metadata=_usage(29, 8)),
        # main agent, final answer
        AIMessage(content="Paris is sunny and your notes are saved.", usage_metadata=_usage(31, 9)),
    ]


EXPECTED_INPUT_TOKENS = sum(turn.usage_metadata["input_tokens"] for turn in scripted_turns())


@tool
def lookup_weather(city: str) -> str:
    """Return the weather for a city."""
    return f"sunny in {city}"


def build_model(turns: Optional[list[AIMessage]] = None) -> ScriptedToolChatModel:
    """Replace with your real chat model. Do not pass ``model=None``."""
    return ScriptedToolChatModel(messages=iter(turns if turns is not None else scripted_turns()))


def build_agent(model: Optional[Any] = None, tools: Optional[list[Any]] = None) -> Any:
    """Build the deep agent.

    ``StateBackend`` keeps files in graph state (a stub, nothing touches disk).
    Rubric middleware is not enabled: it would add a second grading model call.
    """
    return create_deep_agent(
        model=model if model is not None else build_model(),
        tools=tools if tools is not None else [lookup_weather],
        system_prompt="You are a travel assistant.",
        backend=StateBackend(),
    )


def instrument(trace_provider: Any) -> LangChainInstrumentor:
    """Instrument LangChain (and so LangGraph and Deep Agents) once per process.

    Filesystem tool arguments and results can contain file contents, so inputs
    and outputs are hidden. Turn hiding off only on a stub backend.
    """
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(
        tracer_provider=trace_provider,
        config=TraceConfig(hide_inputs=True, hide_outputs=True),
    )
    return instrumentor


def run(agent: Any) -> dict[str, Any]:
    return agent.invoke(
        {"messages": [{"role": "user", "content": "Save my trip notes, check Paris weather, then list the files."}]},
        config={"configurable": {"thread_id": THREAD_ID}},
    )


def main() -> None:
    # 1. register() first, 2. instrument(), 3. build the agent.
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name=os.environ.get("FI_PROJECT_NAME", "deep-agents-cookbook"),
    )
    instrument(trace_provider)

    agent = build_agent()
    result = run(agent)
    print("FINAL_ANSWER", result["messages"][-1].content)

    # Export everything before the process exits.
    print("FLUSHED", trace_provider.force_flush())


if __name__ == "__main__":
    main()
