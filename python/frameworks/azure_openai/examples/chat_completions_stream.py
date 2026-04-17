import os
from openai import AzureOpenAI

from fi_instrumentation.otel import register
from fi_instrumentation.fi_types import ProjectType
from traceai_azure_openai import AzureOpenAIInstrumentor

# Configure trace provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="azure_openai_stream_app",
)

# Initialize the Azure OpenAI instrumentor
AzureOpenAIInstrumentor().instrument(tracer_provider=trace_provider)


if __name__ == "__main__":
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    )

    stream = client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[{"role": "user", "content": "Tell me a short story."}],
        max_tokens=100,
        stream=True,
    )

    for chunk in stream:
        if chunk.choices[0].delta.content is not None:
            print(chunk.choices[0].delta.content, end="")
    print()
