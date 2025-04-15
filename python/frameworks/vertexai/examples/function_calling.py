import os

import vertexai
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_vertexai import VertexAIInstrumentor
from vertexai.generative_models import FunctionDeclaration, GenerativeModel, Part, Tool

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

# Initialize the VertexAI instrumentor
VertexAIInstrumentor().instrument(tracer_provider=trace_provider)

vertexai.init(
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
)

# First, create tools that the model is can use to answer your questions.
# Describe a function by specifying its schema (JsonSchema format)
get_current_weather_func = FunctionDeclaration(
    name="get_current_weather",
    description="Get the current weather in a given location",
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "The city and state, e.g. San Francisco, CA",
            },
            "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
        },
        "required": ["location"],
    },
)
# Tool is a collection of related functions
weather_tool = Tool(function_declarations=[get_current_weather_func])
# Use tools in chat
chat = GenerativeModel("gemini-1.5-flash", tools=[weather_tool]).start_chat()

if __name__ == "__main__":
    # Send a message to the model. The model will respond with a function call.
    for response in chat.send_message(
        "What is the weather like in Boston?", stream=True
    ):
        print(response)
    # Then send a function response to the model. The model will use it to answer.
    for response in chat.send_message(
        Part.from_function_response(
            name="get_current_weather",
            response={"content": {"weather": "super nice"}},
        ),
        stream=True,
    ):
        print(response)
