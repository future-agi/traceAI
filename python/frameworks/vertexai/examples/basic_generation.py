"""Legacy SDK example; use google_genai_vertex.py for new applications."""

import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from traceai_vertexai import VertexAIInstrumentor

trace_provider = register(
    project_type=ProjectType.OBSERVE,
    project_name="vertexai_legacy_app",
)
VertexAIInstrumentor().instrument(tracer_provider=trace_provider)

import vertexai
from vertexai.generative_models import GenerativeModel

vertexai.init(
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
)
try:
    response = GenerativeModel(os.environ["GOOGLE_GENAI_MODEL"]).generate_content(
        "Write a haiku about a mountain trail."
    )
    print(response.text)
finally:
    trace_provider.force_flush()
