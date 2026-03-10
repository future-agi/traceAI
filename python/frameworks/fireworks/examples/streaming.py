"""Streaming example with Fireworks AI instrumentation."""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_fireworks import FireworksInstrumentor


def main():
    # Register TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="fireworks-streaming-example",
    )

    # Instrument Fireworks
    FireworksInstrumentor().instrument(tracer_provider=trace_provider)

    # Now use OpenAI client with Fireworks
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("FIREWORKS_API_KEY"),
        base_url="https://api.fireworks.ai/inference/v1",
    )

    # Make a streaming chat completion request
    print("Streaming response: ", end="", flush=True)

    stream = client.chat.completions.create(
        model="accounts/fireworks/models/llama-v3p1-8b-instruct",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Write a short poem about AI."},
        ],
        max_tokens=200,
        temperature=0.8,
        stream=True,
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)

    print()  # New line at end


if __name__ == "__main__":
    main()
