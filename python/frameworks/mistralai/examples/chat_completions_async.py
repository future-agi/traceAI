import asyncio
import os

from fi_instrumentation import register, using_attributes
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from mistralai import Mistral
from traceai_mistralai import MistralAIInstrumentor

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

# Initialize the Mistral AI instrumentor
MistralAIInstrumentor().instrument(tracer_provider=trace_provider)


async def chat_completions_async():
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    with using_attributes(
        session_id="my-test-session",
        user_id="my-test-user",
        metadata={
            "test-int": 1,
            "test-str": "string",
            "test-list": [1, 2, 3],
            "test-dict": {
                "key-1": "val-1",
                "key-2": "val-2",
            },
        },
        tags=["tag-1", "tag-2"],
        prompt_template="Who won the soccer match in {city} on {date}",
        prompt_template_version="v1.0",
        prompt_template_variables={
            "city": "Johannesburg",
            "date": "July 11th",
        },
    ):
        res = await client.chat.complete_async(
            model="mistral-small-latest",
            messages=[
                {
                    "content": "Who won the World Cup in 2018?",
                    "role": "user",
                },
            ],
        )
        if res is not None:
            print(res.choices[0].message.content)


if __name__ == "__main__":
    asyncio.run(chat_completions_async())
