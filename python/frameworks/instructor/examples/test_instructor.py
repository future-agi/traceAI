import os
from typing import List

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
from traceai_instructor import InstructorInstrumentor

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
    # eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

InstructorInstrumentor().instrument(tracer_provider=trace_provider)


class Address(BaseModel):
    street: str
    city: str
    country: str


class User(BaseModel):
    name: str
    age: int
    addresses: List[Address]


# Initialize with API key
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Enable instructor patches for OpenAI client
client = instructor.from_openai(client)
# Create structured output with nested objects
user = client.chat.completions.create(
    model="gpt-4o",
    messages=[
        {
            "role": "user",
            "content": """
            Extract: Jason is 25 years old.
            He lives at 123 Main St, New York, USA
            and has a summer house at 456 Beach Rd, Miami, USA
        """,
        },
    ],
    response_model=User,
)

# print(user)
