from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType,
)
from litellm import rerank
from traceai_litellm import LiteLLMInstrumentor

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the Lite LLM instrumentor
LiteLLMInstrumentor().instrument(tracer_provider=trace_provider)


query = "What is the capital of the United States?"
documents = [
    "Carson City is the capital city of the American state of Nevada.",
    "The Commonwealth of the Northern Mariana Islands is a group of islands in the Pacific Ocean. Its capital is Saipan.",
    "Washington, D.C. is the capital of the United States.",
    "Capital punishment has existed in the United States since before it was a country.",
]

response = rerank(
    model="cohere/rerank-english-v3.0",
    query=query,
    documents=documents,
    top_n=3,
)
print(response)
