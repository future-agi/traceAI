"""Real boto3 (Bedrock Runtime) and LiteLLM -> traceAI instrumentors -> loopback F01."""

import asyncio
import base64
import json

import pytest

from conftest import MP4, PDF, b64, data_uri, flush, png, refs, span_named, uploads_by_purpose  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402

IMAGE = png(seed=51)
SHOT_1 = png(seed=52)
SHOT_2 = png(seed=53)
TOOL_IMAGE = png(seed=54)
CLAUDE = "us.anthropic.claude-sonnet-4-20250514-v1:0"
NOVA = "amazon.nova-lite-v1:0"


# ------------------------------------------------------------------ Bedrock
@pytest.fixture
def bedrock(spans, media_provider):
    pytest.importorskip("boto3")
    from traceai_bedrock import BedrockInstrumentor

    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        import boto3

        BedrockInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                         config=config or TraceConfig())
        state["on"] = True
        # Created AFTER instrument(): the instrumentor wraps client creation.
        client = boto3.client("bedrock-runtime", region_name="us-east-1", endpoint_url=media_provider.url,
                              aws_access_key_id="AKIDSYNTHETIC", aws_secret_access_key="synthetic")
        return client, exporter

    yield install
    if state.get("on"):
        BedrockInstrumentor().uninstrument()


def converse_messages(computer=True):
    return [
        {"role": "user", "content": [
            {"text": "open the page"},
            {"image": {"format": "png", "source": {"bytes": IMAGE}}},
            {"document": {"format": "pdf", "name": "brief", "source": {"bytes": PDF}}},
            {"image": {"format": "png", "source": {"s3Location": {"uri": "s3://bucket/never-read.png"}}}},
        ]},
        {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "tooluse_1", "name": "computer", "input": {"action": "screenshot"}}},
            {"toolUse": {"toolUseId": "tooluse_2", "name": "chart", "input": {}}},
        ]},
        {"role": "user", "content": [
            {"toolResult": {"toolUseId": "tooluse_1", "content": [
                {"image": {"format": "png", "source": {"bytes": SHOT_1}}}]}},
            {"toolResult": {"toolUseId": "tooluse_2", "content": [
                {"text": "chart"}, {"image": {"format": "png", "source": {"bytes": TOOL_IMAGE}}}]}},
        ]},
    ]


def computer_fields():
    return {"tools": [{"type": "computer_20250124", "name": "computer", "display_width_px": 1024,
                       "display_height_px": 768}], "anthropic_beta": ["computer-use-2025-01-24"]}


def test_converse_vision_document_and_computer_screenshots(f01, bedrock):
    manager = f01.manager()
    client, exporter = bedrock(manager)
    client.converse(modelId=CLAUDE, messages=converse_messages(),
                    toolConfig={"tools": [{"toolSpec": {"name": "chart", "inputSchema": {"json": {"type": "object"}}}}]},
                    additionalModelRequestFields=computer_fields())
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF, SHOT_1, TOOL_IMAGE])
    by_purpose = uploads_by_purpose(f01)
    assert [u["tool_call_id"] for u in by_purpose["computer_screenshot"]] == ["tooluse_1"]
    assert [u["tool_call_id"] for u in by_purpose["tool_result"]] == ["tooluse_2"]
    assert len(by_purpose["document"]) == 1 and len(by_purpose["model_input"]) == 1
    span = span_named(exporter, "bedrock.converse")
    shots = [r for r in refs(span) if r["purpose"] == "computer_screenshot"]
    assert [(r["message_index"], r["part_index"], r["role"]) for r in shots] == [(2, 0, "tool")]
    manager.shutdown(5)


def test_converse_screenshot_needs_declared_computer_tool(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager)
    client.converse(modelId=CLAUDE, messages=converse_messages())
    flush(manager)
    purposes = sorted(u["purpose"] for u in f01.uploads)
    assert "computer_screenshot" not in purposes and purposes.count("tool_result") == 2
    manager.shutdown(5)


def test_converse_unknown_model_family_is_never_read(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager)
    client.converse(modelId="meta.llama3-2-90b-instruct-v1:0", messages=converse_messages())
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


def test_nova_converse_video_block(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager)
    client.converse(modelId=NOVA, messages=[{"role": "user", "content": [
        {"video": {"format": "mp4", "source": {"bytes": MP4}}}, {"text": "summarise"}]}])
    client.converse(modelId=CLAUDE, messages=[{"role": "user", "content": [
        {"video": {"format": "mp4", "source": {"bytes": MP4}}}, {"text": "summarise"}]}])
    flush(manager)
    assert f01.payloads == [MP4]  # video is qualified for Nova only
    manager.shutdown(5)


def test_converse_hide_input_images_keeps_documents(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager, TraceConfig(hide_input_images=True))
    client.converse(modelId=CLAUDE, messages=converse_messages(), additionalModelRequestFields=computer_fields())
    flush(manager)
    assert f01.payloads == [PDF]
    manager.shutdown(5)


def test_invoke_model_anthropic_body(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager)
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": 8,
            "tools": [{"type": "computer_20250124", "name": "computer", "display_width_px": 800,
                       "display_height_px": 600}],
            "messages": [
                {"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(IMAGE)}}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "computer",
                                                   "input": {"action": "screenshot"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": b64(SHOT_2)}}]}]},
            ]}
    response = client.invoke_model(modelId="anthropic.claude-3-5-sonnet-20240620-v1:0", body=json.dumps(body))
    assert json.loads(response["body"].read())["content"][0]["text"] == "ok"
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, SHOT_2])
    assert sorted(u["purpose"] for u in f01.uploads) == ["computer_screenshot", "model_input"]
    manager.shutdown(5)


def test_invoke_model_nova_body(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager)
    body = {"schemaVersion": "messages-v1", "messages": [{"role": "user", "content": [
        {"image": {"format": "png", "source": {"bytes": b64(IMAGE)}}},
        {"document": {"format": "pdf", "name": "brief", "source": {"bytes": b64(PDF)}}},
        {"text": "describe"}]}]}
    client.invoke_model(modelId=NOVA, body=json.dumps(body))
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, PDF])
    manager.shutdown(5)


@pytest.mark.parametrize("model", ["amazon.titan-image-generator-v2:0", "amazon.nova-canvas-v1:0"])
def test_invoke_model_image_generation_both_directions(f01, bedrock, media_provider, model):
    manager = f01.manager()
    client, exporter = bedrock(manager)
    body = {"taskType": "INPAINTING", "inPaintingParams": {"image": b64(IMAGE), "text": "add a hat",
                                                            "maskPrompt": "head"},
            "imageGenerationConfig": {"numberOfImages": 1}}
    response = client.invoke_model(modelId=model, body=json.dumps(body))
    # The output came from the body the instrumentor already buffered; the caller still reads it whole.
    assert json.loads(response["body"].read())["images"] == [b64(media_provider.generated)]
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, media_provider.generated])
    directions = sorted((u["direction"], u["purpose"]) for u in f01.uploads)
    assert directions == [("input", "model_input"), ("output", "model_output")]
    span = span_named(exporter, "bedrock.invoke_model")
    assert len(refs(span)) == 2
    manager.shutdown(5)


def test_invoke_model_unknown_body_is_never_scraped(f01, bedrock):
    manager = f01.manager()
    client, _ = bedrock(manager)
    body = {"prompt": "x", "images": [b64(IMAGE)], "messages": [{"role": "user", "content": [
        {"image": {"format": "png", "source": {"bytes": b64(IMAGE)}}}]}]}
    client.invoke_model(modelId="meta.llama3-2-90b-instruct-v1:0", body=json.dumps(body))
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


# ------------------------------------------------------------------ LiteLLM
@pytest.fixture
def litellm_instrument(spans):
    pytest.importorskip("litellm")
    from traceai_litellm import LiteLLMInstrumentor

    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None, with_openai=False):
        LiteLLMInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                         config=config or TraceConfig())
        state["litellm"] = True
        if with_openai:
            from traceai_openai import OpenAIInstrumentor

            OpenAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager)
            state["openai"] = True
        return exporter

    yield install
    if state.get("openai"):
        from traceai_openai import OpenAIInstrumentor

        OpenAIInstrumentor().uninstrument()
    if state.get("litellm"):
        LiteLLMInstrumentor().uninstrument()


def chat_messages():
    return [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": {"url": data_uri(IMAGE)}},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}},
    ]}]


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
def test_litellm_completion_input_media(f01, media_provider, litellm_instrument, mode):
    import litellm

    manager = f01.manager()
    exporter = litellm_instrument(manager)
    options = dict(model="openai/gpt-synthetic", messages=chat_messages(), api_base=media_provider.url,
                   api_key="sk-synthetic")
    if mode == "async":
        asyncio.run(litellm.acompletion(**options))
    elif mode == "stream":
        for _ in litellm.completion(stream=True, **options):
            pass
    else:
        litellm.completion(**options)
    flush(manager)
    assert f01.payloads == [IMAGE]
    span = span_named(exporter, "completion", "acompletion")
    assert [r["purpose"] for r in refs(span)] == ["model_input"]
    manager.shutdown(5)


def test_litellm_owns_media_under_nested_openai_instrumentor(f01, media_provider, litellm_instrument):
    """LiteLLM marks the call as owned, so the nested OpenAI instrumentor uploads nothing more."""
    pytest.importorskip("traceai_openai")
    import litellm

    manager = f01.manager()
    exporter = litellm_instrument(manager, with_openai=True)
    litellm.completion(model="openai/gpt-synthetic", messages=chat_messages(), api_base=media_provider.url,
                       api_key="sk-synthetic")
    flush(manager)
    assert f01.payloads == [IMAGE]
    owners = [name for name, _ in ((s.name, r) for s in exporter.get_finished_spans() for r in refs(s))]
    assert owners == ["completion"], owners
    manager.shutdown(5)


@pytest.mark.parametrize("async_", [False, True])
def test_litellm_image_generation_output(f01, media_provider, litellm_instrument, async_):
    import litellm

    manager = f01.manager()
    exporter = litellm_instrument(manager)
    options = dict(model="openai/dall-e-3", prompt="a cat", api_base=media_provider.url, api_key="sk-synthetic",
                   response_format="b64_json")
    if async_:
        result = asyncio.run(litellm.aimage_generation(**options))
    else:
        result = litellm.image_generation(**options)
    assert base64.b64decode(result.data[0].b64_json) == media_provider.generated
    flush(manager)
    assert f01.payloads == [media_provider.generated]  # the URL result is never fetched
    assert f01.uploads[0]["direction"] == "output"
    span = span_named(exporter, "image_generation", "aimage_generation")
    assert len(refs(span)) == 1
    manager.shutdown(5)


def test_litellm_hide_input_images(f01, media_provider, litellm_instrument):
    import litellm

    manager = f01.manager()
    litellm_instrument(manager, TraceConfig(hide_input_images=True))
    litellm.completion(model="openai/gpt-synthetic", messages=chat_messages(), api_base=media_provider.url,
                       api_key="sk-synthetic")
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)
