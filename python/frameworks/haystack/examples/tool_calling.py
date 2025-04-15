import json

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from haystack import Pipeline
from haystack.components.generators.chat import OpenAIChatGenerator
from haystack.dataclasses import ChatMessage
from traceai_haystack import HaystackInstrumentor

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

# Initialize the Haystack instrumentor
HaystackInstrumentor().instrument(tracer_provider=trace_provider)

tools = [
    {
        "type": "function",
        "function": {
            "name": "get_current_weather",
            "description": "Get the current weather",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": (
                            "The temperature unit to use. Infer this from the users location."
                        ),
                    },
                },
                "required": ["location", "format"],
            },
        },
    }
]
messages = [ChatMessage.from_user("What's the weather like in Berlin?")]
llm = OpenAIChatGenerator(model="gpt-4o")
pipe = Pipeline()
pipe.add_component("llm", llm)
response = pipe.run(
    {"llm": {"messages": messages, "generation_kwargs": {"tools": tools}}}
)
response_msg = response["llm"]["replies"][0]
messages.append(response_msg)
weather_response = [
    {
        "id": "response_uhGNifLfopt5JrCUxXw1L3zo",
        "status": "success",
        "function": {
            "name": "get_current_weather",
            "arguments": {"location": "Berlin", "format": "celsius"},
        },
        "data": {
            "location": "Berlin",
            "temperature": 18,
            "weather_condition": "Partly Cloudy",
            "humidity": "60%",
            "wind_speed": "15 km/h",
            "observation_time": "2024-03-05T14:00:00Z",
        },
    }
]
messages.append(
    ChatMessage.from_function(
        content=json.dumps(weather_response), name="get_current_weather"
    )
)
response = pipe.run(
    {"llm": {"messages": messages, "generation_kwargs": {"tools": tools}}}
)
print(f'{response["llm"]["replies"][0]=}')
