import openai
import sys
import os

# Now import from local paths
from fi_instrumentation.otel import register
from fi_instrumentation.instrumentation.context_attributes import using_attributes
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
    ModelChoices,
    )
from traceai_openai import OpenAIInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.AGENT_AS_JUDGE,
        value=EvalSpanKind.LLM,
        type=EvalTagType.OBSERVATION_SPAN,
        custom_eval_name="custom_eval_name_custom_5_tox2",
        # mapping={
        #     "output": "response"
        # },
        mapping={},
        model="turing_large"
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI_CUSTOM_EVAL",
    project_version_name="v5",
)

# Initialize the OpenAI instrumentor
OpenAIInstrumentor().instrument(tracer_provider=trace_provider)


if __name__ == "__main__":
    client = openai.OpenAI()
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
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "Write a haiku."}],
            max_tokens=20,
        )
        print(response.choices[0].message.content)
