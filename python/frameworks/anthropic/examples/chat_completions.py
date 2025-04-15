import instructor
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from openai import OpenAI
from pydantic import BaseModel
from traceai_anthropic import AnthropicInstrumentor

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


# Define the output structure
class UserInfo(BaseModel):
    name: str
    age: int


# Patch the OpenAI clientos
client = instructor.patch(client=OpenAI())

AnthropicInstrumentor().instrument(tracer_provider=trace_provider)

# Extract structured data from natural language
user_info = client.chat.completions.create(
    model="gpt-3.5-turbo",
    response_model=UserInfo,
    messages=[
        {
            "role": "system",
            "content": "Extract the name and age from the text and return them in a structured format.",
        },
        {"role": "user", "content": "John Doe is nine years old."},
    ],
)

print(user_info, type(user_info))
