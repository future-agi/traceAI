import asyncio
import os
from openai import AsyncAzureOpenAI

from fi_instrumentation.otel import register
from fi_instrumentation.fi_types import ProjectType
from traceai_azure_openai import AzureOpenAIInstrumentor

# Configure trace provider
trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="azure_openai_async_app",
)

# Initialize the Azure OpenAI instrumentor
AzureOpenAIInstrumentor().instrument(tracer_provider=trace_provider)


async def main():
    client = AsyncAzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-02-01"),
    )

    response = await client.chat.completions.create(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        messages=[{"role": "user", "content": "Write a haiku about Azure cloud."}],
        max_tokens=50,
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    asyncio.run(main())
