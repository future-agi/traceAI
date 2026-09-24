import asyncio

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from llama_index.core.agent.workflow import FunctionAgent
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai import OpenAI
from traceai_llamaindex import LlamaIndexInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)
# Initialize the Llama Index instrumentor
LlamaIndexInstrumentor().instrument(tracer_provider=trace_provider)


def multiply(a: int, b: int) -> int:
    """Multiple two integers and returns the result integer"""
    return a * b


def add(a: int, b: int) -> int:
    """Add two integers and returns the result integer"""
    return a + b


agent = FunctionAgent(
    tools=[FunctionTool.from_defaults(fn=multiply), FunctionTool.from_defaults(fn=add)],
    llm=OpenAI(model="gpt-4o-mini"),
)


async def main():
    response = await agent.run("What is (121 * 3) + 42?")
    print(response)


if __name__ == "__main__":
    asyncio.run(main())
