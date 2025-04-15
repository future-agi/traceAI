import asyncio

import openai
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_openai import OpenAIInstrumentor

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

# Initialize the OpenAI instrumentor
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)


async def chat_completions(**kwargs):
    client = openai.AsyncOpenAI()
    response = await client.chat.completions.with_raw_response.create(**kwargs)
    async for chunk in response.parse():
        if chunk.choices and (content := chunk.choices[0].delta.content):
            print(content, end="")


if __name__ == "__main__":
    asyncio.run(
        chat_completions(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Write a haiku."}],
            max_tokens=20,
            stream=True,
            stream_options={"include_usage": True},
        ),
    )
