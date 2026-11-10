import os

from fi_instrumentation import register
from fi_instrumentation.fi_types import ProjectType
from google import genai
from traceai_google_genai import GoogleGenAIInstrumentor


def main():
    trace_provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="vertexai_app",
    )
    GoogleGenAIInstrumentor().instrument(tracer_provider=trace_provider)
    try:
        client = genai.Client(
            vertexai=True,
            project=os.environ["GOOGLE_CLOUD_PROJECT"],
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        )
        response = client.models.generate_content(
            model=os.environ["GOOGLE_GENAI_MODEL"],
            contents="Write a haiku about a mountain trail.",
        )
        print(response.text)
    finally:
        trace_provider.force_flush()


if __name__ == "__main__":
    main()
