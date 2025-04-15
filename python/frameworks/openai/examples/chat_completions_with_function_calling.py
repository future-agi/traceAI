import openai
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from openai.types.chat import (
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
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


if __name__ == "__main__":
    client = openai.OpenAI()
    messages = [
        ChatCompletionUserMessageParam(
            role="user",
            content="What's the weather like in San Francisco?",
        )
    ]
    response = client.chat.completions.create(
        model="gpt-4",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "finds the weather for a given city",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {
                                "type": "string",
                                "description": "The city to find the weather for, e.g. 'London'",
                            }
                        },
                        "required": ["city"],
                    },
                },
            },
        ],
        messages=messages,
    )
    message = response.choices[0].message
    assert (tool_calls := message.tool_calls)
    tool_call_id = tool_calls[0].id
    messages.append(message)
    messages.append(
        ChatCompletionToolMessageParam(
            content="sunny", role="tool", tool_call_id=tool_call_id
        ),
    )
    client.chat.completions.create(
        model="gpt-4",
        messages=messages,
    )
