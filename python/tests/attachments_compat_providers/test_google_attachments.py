"""Real google-genai and Vertex (gapic REST) SDKs -> traceAI instrumentors -> loopback F01."""

import asyncio
import json

import pytest

from conftest import MP4, PDF, b64, flush, png, refs, span_named, uploads_by_purpose, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402

IMAGE = png(seed=41)


# ------------------------------------------------------------------ google-genai
@pytest.fixture
def genai_instrument(spans):
    pytest.importorskip("google.genai")
    from traceai_google_genai import GoogleGenAIInstrumentor

    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        GoogleGenAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                             config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        GoogleGenAIInstrumentor().uninstrument()


def genai_client(media_provider):
    from google import genai
    from google.genai import types

    return genai.Client(api_key="synthetic-key", http_options=types.HttpOptions(base_url=media_provider.url + "/"))


def gemini_contents():
    from google.genai import types

    return [types.Content(role="user", parts=[
        types.Part(text="describe"),
        types.Part(inline_data=types.Blob(mime_type="image/png", data=IMAGE)),
        types.Part(inline_data=types.Blob(mime_type="application/pdf", data=PDF)),
        types.Part(file_data=types.FileData(file_uri="gs://bucket/never-read.png", mime_type="image/png")),
        types.Part(inline_data=types.Blob(mime_type="audio/L16;codec=pcm;rate=24000", data=b"\x00\x01" * 64)),
    ])]


@pytest.mark.parametrize("async_", [False, True])
def test_genai_generate_content_input_and_generated_output(f01, media_provider, genai_instrument, async_):
    manager = f01.manager()
    exporter = genai_instrument(manager)
    client = genai_client(media_provider)
    if async_:
        response = asyncio.run(client.aio.models.generate_content(model="gemini-synthetic", contents=gemini_contents()))
    else:
        response = client.models.generate_content(model="gemini-synthetic", contents=gemini_contents())
    assert response.candidates[0].content.parts[1].inline_data.data == media_provider.generated
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF, media_provider.generated])
    by_purpose = uploads_by_purpose(f01)
    assert sorted(by_purpose) == ["document", "model_input", "model_output"]
    assert by_purpose["model_output"][0]["direction"] == "output"
    span = span_named(exporter, "GenerateContent", "AsyncGenerateContent")
    positions = sorted((r["direction"], r["message_index"], r["part_index"]) for r in refs(span))
    assert positions == [("input", 0, 1), ("input", 0, 2), ("output", 0, 1)]
    # The application's request reached the provider unchanged (base64 on the wire, as the SDK sends it).
    sent = json.dumps(media_provider.requests[-1][1])
    assert b64(IMAGE) in sent
    manager.shutdown(5)


def test_genai_stream_captures_input_only(f01, media_provider, genai_instrument):
    manager = f01.manager()
    genai_instrument(manager)
    client = genai_client(media_provider)
    for _ in client.models.generate_content_stream(model="gemini-synthetic", contents=gemini_contents()):
        pass
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF])
    manager.shutdown(5)


def test_genai_bare_parts_and_audio(f01, media_provider, genai_instrument):
    from google.genai import types

    manager = f01.manager()
    genai_instrument(manager)
    sound = wav(seed=5)
    client = genai_client(media_provider)
    client.models.generate_content(model="gemini-synthetic", contents=[
        types.Part.from_bytes(data=sound, mime_type="audio/wav"),
        types.Part.from_bytes(data=MP4, mime_type="video/mp4"),
        "transcribe",
    ])
    flush(manager)
    assert sorted(f01.payloads) == sorted([sound, MP4, media_provider.generated])
    manager.shutdown(5)


@pytest.mark.parametrize("config", [TraceConfig(hide_input_images=True), TraceConfig(hide_outputs=True)])
def test_genai_local_restrictions(f01, media_provider, genai_instrument, config):
    manager = f01.manager()
    genai_instrument(manager, config)
    genai_client(media_provider).models.generate_content(model="gemini-synthetic", contents=gemini_contents())
    flush(manager)
    if config.hide_input_images:
        assert sorted(f01.payloads) == sorted([PDF, media_provider.generated])
    else:
        assert sorted(f01.payloads) == sorted([IMAGE, PDF])
    manager.shutdown(5)


def test_genai_without_manager_is_unchanged(media_provider, genai_instrument):
    exporter = genai_instrument(None)
    genai_client(media_provider).models.generate_content(model="gemini-synthetic", contents=gemini_contents())
    span = span_named(exporter, "GenerateContent")
    assert "fi.attachment.refs" not in span.attributes


# ------------------------------------------------------------------ Vertex (gapic, REST transport)
@pytest.fixture
def vertex_instrument(spans):
    pytest.importorskip("google.cloud.aiplatform_v1")
    from traceai_vertexai import VertexAIInstrumentor

    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        VertexAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                          config=config or TraceConfig())
        state["on"] = True
        return exporter

    yield install
    if state.get("on"):
        VertexAIInstrumentor().uninstrument()


def vertex_client(media_provider):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import aiplatform_v1

    # Created AFTER instrument(): the instrumentor wraps gapic's wrap_method.
    return aiplatform_v1.PredictionServiceClient(
        credentials=AnonymousCredentials(), transport="rest",
        client_options={"api_endpoint": media_provider.url})


MODEL = "projects/synthetic/locations/us-central1/publishers/google/models/"


def test_vertex_generate_content_input_and_output(f01, media_provider, vertex_instrument):
    from google.cloud import aiplatform_v1 as v1

    manager = f01.manager()
    exporter = vertex_instrument(manager)
    request = v1.GenerateContentRequest(model=MODEL + "gemini-synthetic", contents=[v1.Content(role="user", parts=[
        v1.Part(text="describe"),
        v1.Part(inline_data=v1.Blob(mime_type="image/png", data=IMAGE)),
        v1.Part(inline_data=v1.Blob(mime_type="application/pdf", data=PDF)),
        v1.Part(file_data=v1.FileData(file_uri="gs://bucket/never-read.png", mime_type="image/png")),
    ])])
    response = vertex_client(media_provider).generate_content(request=request)
    assert response.candidates[0].content.parts[1].inline_data.data == media_provider.generated
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF, media_provider.generated])
    assert sorted(uploads_by_purpose(f01)) == ["document", "model_input", "model_output"]
    assert len(refs(exporter.get_finished_spans()[-1])) == 3
    manager.shutdown(5)


def test_vertex_imagen_predictions_are_generated_images(f01, media_provider, vertex_instrument):
    from google.protobuf import json_format, struct_pb2

    manager = f01.manager()
    vertex_instrument(manager)
    instance = json_format.ParseDict({"prompt": "a cat"}, struct_pb2.Value())
    vertex_client(media_provider).predict(endpoint=MODEL + "imagen-3.0-generate-002", instances=[instance])
    flush(manager)
    assert f01.payloads == [media_provider.generated]
    assert f01.uploads[0]["purpose"] == "model_output" and f01.uploads[0]["direction"] == "output"
    manager.shutdown(5)


def test_vertex_other_prediction_endpoints_are_never_read(f01, media_provider, vertex_instrument):
    from google.protobuf import json_format, struct_pb2

    manager = f01.manager()
    vertex_instrument(manager)
    instance = json_format.ParseDict({"content": "x"}, struct_pb2.Value())
    vertex_client(media_provider).predict(endpoint="projects/synthetic/locations/us-central1/endpoints/123",
                                          instances=[instance])
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)
