import dspy
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType,
)
from traceai_dspy import DSPyInstrumentor

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Instrument DSPy with the trace provider
DSPyInstrumentor().instrument(tracer_provider=trace_provider)


embedder = dspy.Embedder("openai/text-embedding-3-small", batch_size=100)
embeddings = embedder(
    ["hello", "world", "asre"],
)

# print("Embedding for 'hello':", embeddings[0].tolist())
