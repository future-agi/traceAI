"""Basic chat example with xAI (Grok) instrumentation."""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_xai import XAIInstrumentor


def main():
    # Register TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="xai-example",
    )

    # Instrument xAI
    XAIInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use OpenAI client with xAI
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("XAI_API_KEY"),
        base_url="https://api.x.ai/v1",
    )

    # Make a chat completion request
    response = client.chat.completions.create(
        model="grok-beta",
        messages=[
            {"role": "system", "content": "You are Grok, a helpful AI assistant."},
            {"role": "user", "content": "What is the meaning of life?"},
        ],
        max_tokens=150,
        temperature=0.7,
    )

    print("Response:", response.choices[0].message.content)
    print("Model:", response.model)
    print("Usage:", response.usage)


if __name__ == "__main__":
    main()
