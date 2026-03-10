"""Basic chat example with Fireworks AI instrumentation."""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_fireworks import FireworksInstrumentor


def main():
    # Register TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="fireworks-example",
    )

    # Instrument Fireworks
    FireworksInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use OpenAI client with Fireworks
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("FIREWORKS_API_KEY"),
        base_url="https://api.fireworks.ai/inference/v1",
    )

    # Make a chat completion request
    response = client.chat.completions.create(
        model="accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the capital of France?"},
        ],
        max_tokens=100,
        temperature=0.7,
    )

    print("Response:", response.choices[0].message.content)
    print("Model:", response.model)
    print("Usage:", response.usage)


if __name__ == "__main__":
    main()
