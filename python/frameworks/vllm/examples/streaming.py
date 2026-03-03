"""Streaming example with vLLM instrumentation."""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_vllm import VLLMInstrumentor


def main():
    # Register TraceAI
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="vllm-streaming-example",
    )

    # Instrument vLLM (specify your vLLM server URL)
    VLLMInstrumentor(
        vllm_base_urls=["localhost:8000"]
    ).instrument(tracer_provider=trace_provider)

    # Now use OpenAI client with vLLM
    from openai import OpenAI

    client = OpenAI(
        api_key="token",  # vLLM doesn't require a real API key
        base_url="http://localhost:8000/v1",
    )

    # Make a streaming chat completion request
    print("Streaming response: ", end="", flush=True)

    stream = client.chat.completions.create(
        model="meta-llama/Llama-2-7b-chat-hf",  # Model loaded in vLLM
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Write a haiku about machine learning."},
        ],
        max_tokens=100,
        temperature=0.8,
        stream=True,
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)

    print()  # New line at end


if __name__ == "__main__":
    main()
