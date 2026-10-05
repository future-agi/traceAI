"""TH-8327 measurement: the LangGraph path with traceAI-langchain on.

Run by tests/test_measurement.py through ``harness.run``. This is the shape of
``python/frameworks/langchain/examples/langgraph_agent_supervisor.py``: a
LangGraph ``StateGraph`` whose agent asks for ``TavilySearchResults``
(``langchain_community.tools.tavily_search``, the import that example uses)
and a ``ToolNode`` that runs it. The model is replaced by a scripted agent
node, so no LLM is called. ``TavilySearchResults`` posts to the loopback fake
in ``TAVILY_BASE_URL``; spans leave through ``fi_instrumentation.register()``
to ``FI_BASE_URL``.

Options:
  --with-tavily-instrumentor  also instrument traceAI-tavily (PRD J5: the
                              LangChain tool plus the bare-client wrapper).
  --custom-tool               use a LangChain ``@tool`` that calls
                              ``TavilyClient.search`` instead of
                              ``TavilySearchResults``.
  --use-langchain-span        in the custom tool, make traceAI-langchain's
                              tool span current around the client call
                              (``traceai_langchain.get_current_span()``).

Prints one JSON line with the tool output length and the message count.
"""

# No ``from __future__ import annotations``: LangGraph resolves the node and
# router annotations at runtime, and these are local names.

import json
import os
import sys
import warnings

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

QUERY = "th-8327 langgraph tool measurement"


def _custom_tool():
    """A LangChain tool written over the bare client, as an app might."""
    from langchain_core.tools import tool
    from tavily import TavilyClient

    @tool
    def tavily_web_search(query: str) -> str:
        """Search the web with Tavily and return the result URLs."""
        client = TavilyClient(
            api_key=os.environ["TAVILY_API_KEY"],
            api_base_url=os.environ["TAVILY_BASE_URL"],
        )
        if "--use-langchain-span" in sys.argv:
            from opentelemetry import trace
            from traceai_langchain import get_current_span

            with trace.use_span(get_current_span(), end_on_exit=False):
                response = client.search(query, max_results=2)
        else:
            response = client.search(query, max_results=2)
        return json.dumps([result["url"] for result in response["results"]])

    return tavily_web_search


def main() -> None:
    provider = register(
        project_name=os.environ.get("MEASURE_PROJECT", "tavily-langgraph-measurement"),
        project_type=ProjectType.OBSERVE,
        batch=False,
        verbose=False,
    )
    from traceai_langchain import LangChainInstrumentor

    LangChainInstrumentor().instrument(tracer_provider=provider)
    instrumented = ["traceai_langchain"]
    if "--with-tavily-instrumentor" in sys.argv:
        from traceai_tavily import TavilyInstrumentor

        TavilyInstrumentor().instrument(tracer_provider=provider)
        instrumented.append("traceai_tavily")

    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    if "--custom-tool" in sys.argv:
        tool = _custom_tool()
    else:
        with warnings.catch_warnings():
            # TavilySearchResults is deprecated upstream in favour of
            # langchain-tavily; the example on the base branch still imports it.
            warnings.simplefilter("ignore")
            import langchain_community.utilities.tavily_search as tavily_utilities
            from langchain_community.tools.tavily_search import TavilySearchResults

            # The API wrapper reads this module constant on every call.
            tavily_utilities.TAVILY_API_URL = os.environ["TAVILY_BASE_URL"]
            tool = TavilySearchResults(max_results=2)

    def agent(state: MessagesState) -> dict:
        if any(isinstance(message, ToolMessage) for message in state["messages"]):
            return {"messages": [AIMessage(content="done")]}
        call = {"name": tool.name, "args": {"query": QUERY}, "id": "call-tavily-1"}
        return {"messages": [AIMessage(content="", tool_calls=[call])]}

    def route(state: MessagesState) -> str:
        return "tools" if state["messages"][-1].tool_calls else END

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = graph.compile().invoke({"messages": [HumanMessage(content=QUERY)]})
    provider.force_flush()
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    print(
        json.dumps(
            {
                "instrumented": instrumented,
                "tool_name": tool.name,
                "messages": len(result["messages"]),
                "tool_messages": len(tool_messages),
                "tool_status": [getattr(m, "status", None) for m in tool_messages],
            }
        )
    )


if __name__ == "__main__":
    main()
