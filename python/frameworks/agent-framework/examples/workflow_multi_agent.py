"""Multi-agent workflow example traced into Future AGI.

Two agents handing off through a WorkflowBuilder graph:
  researcher → summarizer → output

Run with:
    export FI_API_KEY=...
    export FI_SECRET_KEY=...
    export OPENAI_API_KEY=...
    python examples/workflow_multi_agent.py

In the FI dashboard you should see a ``workflow.run`` (CHAIN) span containing
nested ``executor.process`` (CHAIN) spans and ``invoke_agent`` (AGENT) spans
for each agent invocation.
"""

import asyncio
import os

from agent_framework import Agent, WorkflowBuilder, WorkflowContext
from agent_framework._workflows._function_executor import executor
from agent_framework.observability import enable_instrumentation
from agent_framework.openai import OpenAIChatClient
from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType

from traceai_agent_framework import enable_fi_attribute_mapping


# --- Workflow nodes ---------------------------------------------------------


@executor(id="researcher")
async def research(topic: str, ctx: WorkflowContext[str]) -> None:
    """Step 1: ask the research agent for a few bullet points."""
    agent = Agent(
        OpenAIChatClient(),
        name="researcher",
        instructions="You are a research assistant. Return 3 short bullet points.",
    )
    response = await agent.run(f"Research this topic: {topic}")
    await ctx.send_message(str(response))


@executor(id="summarizer")
async def summarize(notes: str, ctx: WorkflowContext[None, str]) -> None:
    """Step 2: ask the summarizer agent to compress to one sentence."""
    agent = Agent(
        OpenAIChatClient(),
        name="summarizer",
        instructions="Compress the input into a single concise sentence.",
    )
    response = await agent.run(notes)
    await ctx.yield_output(str(response))


# --- Main -------------------------------------------------------------------


def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY before running this example.")

    register(
        project_type=ProjectType.OBSERVE,
        project_name="agent-framework-workflow-example",
        set_global_tracer_provider=True,
    )
    enable_instrumentation(enable_sensitive_data=True)
    enable_fi_attribute_mapping()

    workflow = (
        WorkflowBuilder(start_executor=research)
        .add_edge(research, summarize)
        .build()
    )

    async def run() -> None:
        result = await workflow.run("the discovery of penicillin")
        print("Workflow result:\n", result)

    asyncio.run(run())


if __name__ == "__main__":
    main()
