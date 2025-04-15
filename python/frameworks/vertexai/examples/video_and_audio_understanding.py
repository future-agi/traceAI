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
prompt = """Provide a description of the video.
The description should also contain anything important which people say in the video.
"""
video_file_uri = "gs://cloud-samples-data/generative-ai/video/pixel8.mp4"
video_file = Part.from_uri(video_file_uri, mime_type="video/mp4")
contents = [video_file, prompt]

if __name__ == "__main__":
    response = model.generate_content(contents)
    print(response)
