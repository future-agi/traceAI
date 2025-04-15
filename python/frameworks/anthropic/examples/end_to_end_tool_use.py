from typing import Optional

import anthropic
from anthropic.types import (
    Message,
    MessageParam,
    TextBlock,
    ToolResultBlockParam,
    ToolUseBlock,
)
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_anthropic import AnthropicInstrumentor
from typing_extensions import assert_never

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

# Initialize the Anthropic instrumentation
AnthropicInstrumentor().instrument(tracer_provider=trace_provider)


def _to_assistant_message_param(
    message: Message,
) -> MessageParam:
    content = []
    for block in message.content:
        if isinstance(block, TextBlock):
            content.append(block)
        elif isinstance(block, ToolUseBlock):
            content.append(block)
        else:
            assert_never(block)
    return MessageParam(content=content, role="assistant")


def _get_tool_use_id(message: Message) -> Optional[str]:
    for block in message.content:
        if isinstance(block, ToolUseBlock):
            return block.id
    return None


# Initialize Anthropic client
client = anthropic.Anthropic()
messages = [
    {
        "role": "user",
        "content": "What is the weather like in San Francisco in Fahrenheit?",
    }
]
response = client.messages.create(
    model="claude-3-5-sonnet-20240620",
    max_tokens=1024,
    tools=[
        {
            "name": "get_weather",
            "description": "Get the current weather in a given location",
            "input_schema": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": 'The unit of temperature, either "celsius" or "fahrenheit"',
                    },
                },
                "required": ["location"],
            },
        }
    ],
    messages=messages,
)
messages.append(_to_assistant_message_param(response))

assert (tool_use_id := _get_tool_use_id(response)) is not None, "tool was not called"
messages.append(
    MessageParam(
        content=[
            ToolResultBlockParam(
                tool_use_id=tool_use_id,
                content='{"weather": "sunny", "temperature": "75"}',
                type="tool_result",
                is_error=False,
            )
        ],
        role="user",
    )
)
response = client.messages.create(
    model="claude-3-5-sonnet-20240620",
    max_tokens=1024,
    tools=[
        {
            "name": "get_weather",
            "description": "Get the current weather in a given location",
            "input_schema": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "The city and state, e.g. San Francisco, CA",
                    },
                    "unit": {
                        "type": "string",
                        "enum": ["celsius", "fahrenheit"],
                        "description": 'The unit of temperature, either "celsius" or "fahrenheit"',
                    },
                },
                "required": ["location"],
            },
        }
    ],
    messages=messages,
)
# print(f"{response=}")
