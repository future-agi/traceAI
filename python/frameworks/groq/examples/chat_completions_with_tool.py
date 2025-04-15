import asyncio
import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from groq import AsyncGroq, Groq
from groq.types.chat import ChatCompletionToolMessageParam
from traceai_groq import GroqInstrumentor

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


def test():
    client = Groq(
        api_key=os.environ.get("GROQ_API_KEY"),
    )

    weather_function = {
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
    }

    sys_prompt = "Respond to the user's query using the correct tool."
    user_msg = "What's the weather like in San Francisco?"

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    response = client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=messages,
        temperature=0.0,
        tools=[weather_function],
        tool_choice="required",
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
    final_response = client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=messages,
    )
    return final_response


async def async_test():
    client = AsyncGroq(
        api_key=os.environ.get("GROQ_API_KEY"),
    )

    weather_function = {
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
    }

    sys_prompt = "Respond to the user's query using the correct tool."
    user_msg = "What's the weather like in San Francisco?"

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_msg},
    ]
    response = await client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=messages,
        temperature=0.0,
        tools=[weather_function],
        tool_choice="required",
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
    final_response = await client.chat.completions.create(
        model="mixtral-8x7b-32768",
        messages=messages,
    )
    return final_response


if __name__ == "__main__":

    # Configure trace provider with custom evaluation tags
    trace_provider = register(
        project_type=ProjectType.EXPERIMENT,
        eval_tags=eval_tags,
        project_name="FUTURE_AGI",
        project_version_name="v1",
    )

    # Initialize the Groq instrumentor
    GroqInstrumentor().instrument(tracer_provider=trace_provider)

    response = test()
    print("Response\n--------")
    print(response)

    async_response = asyncio.run(async_test())
    print("\nAsync Response\n--------")
    print(async_response)
