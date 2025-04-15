import os

import vertexai
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    EvalName,
    EvalSpanKind,
    EvalTag,
    EvalTagType,
    ProjectType,
)
from traceai_vertexai import VertexAIInstrumentor
from vertexai.generative_models import GenerativeModel, Part

# Configure trace provider with custom evaluation tags
eval_tags = [
    EvalTag(
        eval_name=EvalName.DETERMINISTIC_EVALS,
        value=EvalSpanKind.TOOL,
        type=EvalTagType.OBSERVATION_SPAN,
        config={
            "multi_choice": False,
            "choices": ["Yes", "No"],
            "rule_prompt": "Evaluate if the response is correct",
        },
        custom_eval_name="<custom_eval_name>",
    )
]

# Configure trace provider with custom evaluation tags
trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    eval_tags=eval_tags,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)

# Initialize the VertexAI instrumentor
VertexAIInstrumentor().instrument(tracer_provider=trace_provider)

vertexai.init(
    project=os.getenv("GOOGLE_CLOUD_PROJECT"),
)
model = GenerativeModel("gemini-1.5-flash")
prompt = """You are a very professional document summarization specialist.
Please summarize the given document.
"""
pdf_file_uri = "gs://cloud-samples-data/generative-ai/pdf/2403.05530.pdf"
pdf_file = Part.from_uri(pdf_file_uri, mime_type="application/pdf")
contents = [pdf_file, prompt]

if __name__ == "__main__":
    response = model.generate_content(contents)
    print(response)
