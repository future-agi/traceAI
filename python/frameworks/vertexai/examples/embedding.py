
from fi_instrumentation import register
from fi_instrumentation.fi_types import (
    ProjectType,
)
from traceai_vertexai import VertexAIInstrumentor
from vertexai.language_models import TextEmbeddingInput
from vertexai.preview.language_models import TextEmbeddingModel

trace_provider = register(
    project_type=ProjectType.EXPERIMENT,
    project_name="FUTURE_AGI",
    project_version_name="v1",
)


VertexAIInstrumentor().instrument(tracer_provider=trace_provider)


def get_text_embedding(text, title=None, task_type=None, output_dimensionality=None):
    model = TextEmbeddingModel.from_pretrained("text-embedding-005")

    text_embedding_input = TextEmbeddingInput(
        text=text, title=title, task_type=task_type
    )

    kwargs = {}
    if output_dimensionality:
        kwargs["output_dimensionality"] = output_dimensionality

    embeddings = model.get_embeddings([text_embedding_input], **kwargs)
    return [embedding.values for embedding in embeddings]


# Example usage:
if __name__ == "__main__":
    sample_text = "Your text here"
    vectors = get_text_embedding(sample_text)
    for vector in vectors:
        print(vector)
