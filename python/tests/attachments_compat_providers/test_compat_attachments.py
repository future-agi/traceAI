"""Real compatible-chat SDKs -> thin traceAI bindings -> loopback F01.

One typed fixture per wrapper plus a capability-negative row; DeepSeek and
Cerebras have no qualified media surface and must upload nothing.
"""

import asyncio

import pytest

from conftest import b64, data_uri, flush, png, refs, wav  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402

IMAGE = png(seed=61)


def image_messages(url_form="object"):
    image = data_uri(IMAGE)
    return [{"role": "user", "content": [
        {"type": "text", "text": "what is this"},
        {"type": "image_url", "image_url": image if url_form == "string" else {"url": image}},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}},
    ]}]


def audio_messages(sound):
    return [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": b64(sound), "format": "wav"}}]}]


@pytest.fixture
def instrumented(spans):
    tracer_provider, exporter = spans
    installed = []

    def install(instrumentor, manager, config=None):
        instrumentor.instrument(tracer_provider=tracer_provider, attachments=manager, config=config or TraceConfig())
        installed.append(instrumentor)
        return exporter

    yield install
    for instrumentor in installed:
        instrumentor.uninstrument()


def media_refs(exporter):
    return [r for s in exporter.get_finished_spans() for r in refs(s)]


# ------------------------------------------------------------------ groq (image-only chat)
def test_groq_chat_images(f01, media_provider, instrumented):
    groq = pytest.importorskip("groq")
    pytest.importorskip("traceai_groq")
    from traceai_groq import GroqInstrumentor

    manager = f01.manager()
    exporter = instrumented(GroqInstrumentor(), manager)
    client = groq.Groq(api_key="gsk-synthetic", base_url=media_provider.url, max_retries=0)
    client.chat.completions.create(model="m", messages=image_messages())
    client.chat.completions.create(model="m", messages=audio_messages(wav()))  # Groq chat: no audio parts
    asyncio.run(groq.AsyncGroq(api_key="gsk-synthetic", base_url=media_provider.url, max_retries=0)
                .chat.completions.create(model="m", messages=image_messages()))
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
    assert [r["purpose"] for r in media_refs(exporter)] == ["model_input", "model_input"]
    manager.shutdown(5)


# ------------------------------------------------------------------ together
def test_together_chat_is_not_claimed(f01, media_provider, instrumented):
    """traceai_together does not trace chat today, so it must not claim (or upload) chat media."""
    together = pytest.importorskip("together")
    pytest.importorskip("traceai_together")
    import traceai_together
    from traceai_together import TogetherInstrumentor

    manager = f01.manager()
    exporter = instrumented(TogetherInstrumentor(), manager)
    client = together.Together(api_key="synthetic", base_url=media_provider.url + "/v1")
    client.chat.completions.create(model="m", messages=image_messages())
    flush(manager)
    assert f01.payloads == [] and media_refs(exporter) == []
    descriptor = traceai_together.together_attachment_capabilities
    assert descriptor["operations"] == () and "chat.completions.create" in descriptor["unsupported"]
    manager.shutdown(5)


# ------------------------------------------------------------------ OpenAI-SDK hosted routes
@pytest.mark.parametrize("package", ["fireworks", "xai"])
def test_openai_sdk_routes_capture_images(f01, media_provider, instrumented, monkeypatch, package):
    openai = pytest.importorskip("openai")
    pytest.importorskip("traceai_" + package)
    if package == "fireworks":
        from traceai_fireworks import FireworksInstrumentor as Instrumentor
        from traceai_fireworks import _wrappers
        monkeypatch.setattr(_wrappers, "_is_fireworks_client", lambda instance: True)
        if hasattr(_wrappers, "_shared_instrumentor_enabled"):
            monkeypatch.setattr(_wrappers, "_shared_instrumentor_enabled", lambda: False)
    else:
        from traceai_xai import XAIInstrumentor as Instrumentor
        from traceai_xai import _wrappers
        monkeypatch.setattr(_wrappers, "_is_xai_client", lambda instance: True)
        if hasattr(_wrappers, "_shared_instrumentor_enabled"):
            monkeypatch.setattr(_wrappers, "_shared_instrumentor_enabled", lambda: False)

    manager = f01.manager()
    exporter = instrumented(Instrumentor(), manager)
    client = openai.OpenAI(api_key="sk-synthetic", base_url=media_provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="m", messages=image_messages())
    client.chat.completions.create(model="m", messages=audio_messages(wav()))  # image-only routes
    flush(manager)
    assert f01.payloads == [IMAGE]
    assert len(media_refs(exporter)) == 1
    manager.shutdown(5)


def media_owners(exporter):
    return [(s.name, r["purpose"]) for s in exporter.get_finished_spans() for r in refs(s)]


@pytest.mark.parametrize("order", ["openai_first", "binding_first"])
@pytest.mark.parametrize("package", ["xai", "minimax"])
def test_binding_with_openai_instrumentor_uploads_once(f01, media_provider, instrumented, monkeypatch, package,
                                                       order):
    """The OpenAI instrumentor wraps the same SDK call; one image is uploaded once, whatever the order."""
    openai = pytest.importorskip("openai")
    pytest.importorskip("traceai_openai")
    pytest.importorskip("traceai_" + package)
    from traceai_openai import OpenAIInstrumentor

    if package == "xai":
        from traceai_xai import XAIInstrumentor as Instrumentor
        from traceai_xai import _wrappers
        monkeypatch.setattr(_wrappers, "_is_xai_client", lambda instance: True)
    else:
        from traceai_minimax import MiniMaxInstrumentor as Instrumentor
        from traceai_minimax import _wrappers
        monkeypatch.setattr(_wrappers, "is_minimax_client", lambda instance: True)

    manager = f01.manager()
    pair = [OpenAIInstrumentor(), Instrumentor()]
    if order == "binding_first":
        pair.reverse()
    for instrumentor in pair:
        exporter = instrumented(instrumentor, manager)
    client = openai.OpenAI(api_key="sk-synthetic", base_url=media_provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="m", messages=image_messages())
    flush(manager)
    assert f01.payloads == [IMAGE]
    owners = media_owners(exporter)
    assert len(owners) == 1 and owners[0][1] == "model_input", owners
    manager.shutdown(5)


def test_minimax_chat_images(f01, media_provider, instrumented, monkeypatch):
    openai = pytest.importorskip("openai")
    pytest.importorskip("traceai_minimax")
    from traceai_minimax import MiniMaxInstrumentor
    from traceai_minimax import _wrappers

    monkeypatch.setattr(_wrappers, "is_minimax_client", lambda instance: True)
    manager = f01.manager()
    exporter = instrumented(MiniMaxInstrumentor(), manager)
    client = openai.OpenAI(api_key="sk-synthetic", base_url=media_provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="MiniMax-M3", messages=image_messages())
    client.chat.completions.create(model="MiniMax-M3", messages=audio_messages(wav()))  # no audio input
    asyncio.run(openai.AsyncOpenAI(api_key="sk-synthetic", base_url=media_provider.url + "/v1", max_retries=0)
                .chat.completions.create(model="MiniMax-M3", messages=image_messages()))
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
    assert media_owners(exporter) == [("minimax.chat.completions", "model_input")] * 2
    manager.shutdown(5)


def test_minimax_hide_input_images(f01, media_provider, instrumented, monkeypatch):
    openai = pytest.importorskip("openai")
    pytest.importorskip("traceai_minimax")
    from traceai_minimax import MiniMaxInstrumentor
    from traceai_minimax import _wrappers

    monkeypatch.setattr(_wrappers, "is_minimax_client", lambda instance: True)
    manager = f01.manager()
    instrumented(MiniMaxInstrumentor(), manager, TraceConfig(hide_input_images=True))
    client = openai.OpenAI(api_key="sk-synthetic", base_url=media_provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="MiniMax-M3", messages=image_messages())
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


# ------------------------------------------------------------------ vllm (legacy OpenAI-dialect chat binding)
def test_vllm_chat_images_and_audio(f01, media_provider, instrumented):
    openai = pytest.importorskip("openai")
    pytest.importorskip("traceai_vllm")
    from traceai_vllm import VLLMInstrumentor

    manager = f01.manager()
    sound = wav(seed=9)
    exporter = instrumented(VLLMInstrumentor(vllm_base_urls=[media_provider.url]), manager)
    client = openai.OpenAI(api_key="EMPTY", base_url=media_provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="m", messages=image_messages() + audio_messages(sound))
    asyncio.run(openai.AsyncOpenAI(api_key="EMPTY", base_url=media_provider.url + "/v1", max_retries=0)
                .chat.completions.create(model="m", messages=image_messages()))
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, sound, IMAGE])
    assert sorted(media_owners(exporter)) == [("vLLM Chat Completions", "model_input")] * 3
    manager.shutdown(5)


def test_vllm_defers_to_openai_instrumentor(f01, media_provider, instrumented):
    """VLLMInstrumentor already defers the whole call to traceai_openai while it is enabled: one upload."""
    openai = pytest.importorskip("openai")
    pytest.importorskip("traceai_openai")
    pytest.importorskip("traceai_vllm")
    from traceai_openai import OpenAIInstrumentor
    from traceai_vllm import VLLMInstrumentor

    manager = f01.manager()
    instrumented(VLLMInstrumentor(vllm_base_urls=[media_provider.url]), manager)
    exporter = instrumented(OpenAIInstrumentor(), manager)
    client = openai.OpenAI(api_key="EMPTY", base_url=media_provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="m", messages=image_messages())
    flush(manager)
    assert f01.payloads == [IMAGE]
    owners = media_owners(exporter)
    assert len(owners) == 1 and owners[0][0] != "vLLM Chat Completions", owners
    manager.shutdown(5)


@pytest.mark.parametrize("package", ["deepseek", "cerebras"])
def test_routes_without_media_surface_capture_nothing(f01, media_provider, instrumented, monkeypatch, package):
    pytest.importorskip("traceai_" + package)
    if package == "deepseek":
        openai = pytest.importorskip("openai")
        import traceai_deepseek
        from traceai_deepseek import DeepSeekInstrumentor as Instrumentor

        for name in ("_is_deepseek_client", "_is_deepseek"):
            if hasattr(traceai_deepseek._wrappers, name):
                monkeypatch.setattr(traceai_deepseek._wrappers, name, lambda *a, **k: True)
        manager = f01.manager()
        exporter = instrumented(Instrumentor(), manager)
        client = openai.OpenAI(api_key="sk-synthetic", base_url=media_provider.url + "/v1", max_retries=0)
        client.chat.completions.create(model="deepseek-chat", messages=image_messages())
        descriptor = traceai_deepseek.deepseek_attachment_capabilities
    else:
        sdk = pytest.importorskip("cerebras.cloud.sdk")
        import traceai_cerebras
        from traceai_cerebras import CerebrasInstrumentor as Instrumentor

        manager = f01.manager()
        exporter = instrumented(Instrumentor(), manager)
        try:
            client = sdk.Cerebras(api_key="synthetic", base_url=media_provider.url, max_retries=0,
                                  warm_tcp_connection=False)
        except TypeError:  # cerebras-cloud-sdk 1.0.0 has no warm_tcp_connection option
            client = sdk.Cerebras(api_key="synthetic", base_url=media_provider.url, max_retries=0)
        client.chat.completions.create(model="llama", messages=image_messages())
        descriptor = traceai_cerebras.cerebras_attachment_capabilities
    flush(manager)
    assert f01.payloads == [] and media_refs(exporter) == []
    assert descriptor["operations"] == () and descriptor["note"]
    manager.shutdown(5)


# ------------------------------------------------------------------ mistralai (legacy 1.x and native 3.6+)
@pytest.mark.parametrize("async_", [False, True])
def test_mistral_chat_images(f01, media_provider, instrumented, async_):
    mistralai = pytest.importorskip("mistralai")
    pytest.importorskip("traceai_mistralai")
    from traceai_mistralai import MistralAIInstrumentor

    manager = f01.manager()
    exporter = instrumented(MistralAIInstrumentor(), manager)
    Mistral = getattr(mistralai, "Mistral", None)
    if Mistral is None:
        from mistralai.client import Mistral
    client = Mistral(api_key="synthetic", server_url=media_provider.url)
    messages = image_messages("string") + [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": data_uri(png(seed=62))}}]}]
    if async_:
        asyncio.run(client.chat.complete_async(model="pixtral-synthetic", messages=messages))
    else:
        client.chat.complete(model="pixtral-synthetic", messages=messages)
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, png(seed=62)])
    assert len(media_refs(exporter)) == 2
    manager.shutdown(5)


def test_mistral_stream_and_hide_input_images(f01, media_provider, instrumented):
    mistralai = pytest.importorskip("mistralai")
    pytest.importorskip("traceai_mistralai")
    from traceai_mistralai import MistralAIInstrumentor

    manager = f01.manager()
    instrumented(MistralAIInstrumentor(), manager, TraceConfig(hide_input_images=True))
    Mistral = getattr(mistralai, "Mistral", None)
    if Mistral is None:
        from mistralai.client import Mistral
    client = Mistral(api_key="synthetic", server_url=media_provider.url)
    client.chat.complete(model="pixtral-synthetic", messages=image_messages("string"))
    flush(manager)
    assert f01.payloads == []
    manager.shutdown(5)


# ------------------------------------------------------------------ openrouter
def test_openrouter_chat_images_and_audio(f01, media_provider, instrumented):
    openrouter = pytest.importorskip("openrouter")
    pytest.importorskip("traceai_openrouter")
    from traceai_openrouter import OpenRouterInstrumentor

    manager = f01.manager()
    exporter = instrumented(OpenRouterInstrumentor(), manager)
    sound = wav(seed=8)
    with openrouter.OpenRouter(api_key="sk-or-synthetic", server_url=media_provider.url) as client:
        client.chat.send(model="m", messages=image_messages() + audio_messages(sound))
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, sound])
    assert len(media_refs(exporter)) == 2
    manager.shutdown(5)


# ------------------------------------------------------------------ portkey
def test_portkey_chat_images(f01, media_provider, instrumented):
    portkey_ai = pytest.importorskip("portkey_ai")
    pytest.importorskip("traceai_portkey")
    from traceai_portkey import PortkeyInstrumentor

    manager = f01.manager()
    exporter = instrumented(PortkeyInstrumentor(), manager)
    client = portkey_ai.Portkey(api_key="synthetic", base_url=media_provider.url + "/v1", provider="openai",
                                Authorization="Bearer sk-synthetic")
    client.chat.completions.create(model="m", messages=image_messages())
    flush(manager)
    assert f01.payloads == [IMAGE]
    assert len(media_refs(exporter)) == 1
    manager.shutdown(5)


# ------------------------------------------------------------------ ollama
def test_ollama_chat_and_generate_images(f01, media_provider, instrumented, tmp_path):
    ollama = pytest.importorskip("ollama")
    pytest.importorskip("traceai_ollama")
    from traceai_ollama import OllamaInstrumentor

    manager = f01.manager()
    exporter = instrumented(OllamaInstrumentor(), manager)
    second = png(seed=63)
    path = tmp_path / "local.png"
    path.write_bytes(png(seed=64))
    client = ollama.Client(host=media_provider.url)
    client.chat(model="llava", messages=[{"role": "user", "content": "describe", "images": [IMAGE]}], stream=False)
    client.generate(model="llava", prompt="describe", images=[b64(second)], stream=False)
    # A local path: the SDK reads it for its own request; the adapter never opens it or uploads it.
    client.generate(model="llava", prompt="describe", images=[str(path)], stream=False)
    flush(manager)
    assert sorted(f01.payloads) == sorted([IMAGE, second])
    assert len(media_refs(exporter)) == 2
    manager.shutdown(5)


def test_ollama_async_chat(f01, media_provider, instrumented):
    ollama = pytest.importorskip("ollama")
    pytest.importorskip("traceai_ollama")
    from traceai_ollama import OllamaInstrumentor

    manager = f01.manager()
    instrumented(OllamaInstrumentor(), manager)
    asyncio.run(ollama.AsyncClient(host=media_provider.url).chat(
        model="llava", messages=[{"role": "user", "content": "describe", "images": [IMAGE]}], stream=False))
    flush(manager)
    assert f01.payloads == [IMAGE]
    manager.shutdown(5)


# ------------------------------------------------------------------ descriptors exported per package
@pytest.mark.parametrize("module,name", [
    ("traceai_google_genai", "google_genai_attachment_capabilities"),
    ("traceai_vertexai", "vertexai_attachment_capabilities"),
    ("traceai_bedrock", "bedrock_attachment_capabilities"),
    ("traceai_litellm", "litellm_attachment_capabilities"),
    ("traceai_together", "together_attachment_capabilities"),
    ("traceai_fireworks", "fireworks_attachment_capabilities"),
    ("traceai_xai", "xai_attachment_capabilities"),
    ("traceai_mistralai", "mistralai_attachment_capabilities"),
    ("traceai_openrouter", "openrouter_attachment_capabilities"),
    ("traceai_ollama", "ollama_attachment_capabilities"),
    ("traceai_portkey", "portkey_attachment_capabilities"),
    ("traceai_deepseek", "deepseek_attachment_capabilities"),
    ("traceai_cerebras", "cerebras_attachment_capabilities"),
    ("traceai_vllm", "vllm_attachment_capabilities"),
    ("traceai_minimax", "minimax_attachment_capabilities"),
])
def test_package_descriptor(module, name):
    package = pytest.importorskip(module)
    descriptor = getattr(package, name)
    assert descriptor["schema_version"] == 1 and isinstance(descriptor["operations"], tuple)
    with pytest.raises(AttributeError):
        getattr(package, "no_such_attachment_capabilities")
