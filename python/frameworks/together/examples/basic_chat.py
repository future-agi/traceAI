"""
Basic example of using Together AI instrumentation with traceai-together.

This example demonstrates:
1. Setting up OpenTelemetry tracing
2. Instrumenting the Together AI client
3. Making chat completion requests (both sync and streaming)
4. Using context attributes for session tracking
"""

import together

from fi_instrumentation.otel import register
from fi_instrumentation.instrumentation.context_attributes import using_attributes
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_together import TogetherInstrumentor

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.LLM,
        type=EvalTagType.OBSERVATION_SPAN,
        custom_eval_name="together_ai_eval",
        mapping={},
    )
]

# Configure trace provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    eval_tags=eval_tags,
    project_name="TOGETHER_AI_EXAMPLE",
    project_version_name="v1",
)

# Initialize the Together AI instrumentor
TogetherInstrumentor().instrument(tracer_provider=trace_provider)


def basic_chat_example():
    """Basic chat completion example."""
    client = together.Together()

    response = client.chat.completions.create(
        model="meta-llama/Llama-3-8b-chat-hf",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Write a haiku about programming."},
        ],
        max_tokens=100,
        temperature=0.7,
    )

    print("Basic Chat Response:")
    print(response.choices[0].message.content)
    print()
    return response


def streaming_chat_example():
    """Streaming chat completion example."""
    client = together.Together()

    print("Streaming Chat Response:")
    stream = client.chat.completions.create(
        model="meta-llama/Llama-3-8b-chat-hf",
        messages=[
            {"role": "user", "content": "Count from 1 to 5, explaining each number."},
        ],
        max_tokens=200,
        stream=True,
    )

    for chunk in stream:
        if chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)

    print("\n")


def embeddings_example():
    """Embeddings example."""
    client = together.Together()

    response = client.embeddings.create(
        model="togethercomputer/m2-bert-80M-8k-retrieval",
        input=["Hello, world!", "Machine learning is fascinating."],
    )

    print("Embeddings Response:")
    print(f"Number of embeddings: {len(response.data)}")
    print(f"Embedding dimensions: {len(response.data[0].embedding)}")
    print()
    return response


def completions_example():
    """Text completions example."""
    client = together.Together()

    response = client.completions.create(
        model="meta-llama/Llama-3-8b-hf",
        prompt="The future of artificial intelligence is",
        max_tokens=50,
        temperature=0.7,
    )

    print("Completions Response:")
    print(response.choices[0].text)
    print()
    return response


if __name__ == "__main__":
    # Run examples with context attributes for better tracing
    with using_attributes(
        session_id="together-demo-session",
        user_id="demo-user",
        metadata={
            "example_type": "basic_demo",
            "sdk_version": "1.0",
        },
        tags=["demo", "together-ai"],
    ):
        print("=" * 60)
        print("Together AI Instrumentation Demo")
        print("=" * 60)
        print()

        # Basic chat
        basic_chat_example()

        # Streaming chat
        streaming_chat_example()

        # Embeddings
        embeddings_example()

        # Completions
        completions_example()

        print("=" * 60)
        print("Demo completed! Check your tracing dashboard for spans.")
        print("=" * 60)
