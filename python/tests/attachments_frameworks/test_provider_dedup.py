"""Framework + OpenAI instrumentor on ONE manager: the same bytes upload exactly once.

Each test drives the real framework against the real OpenAI SDK (loopback provider),
with both the framework instrumentor and ``traceai_openai`` given the same manager.
"""

import json

import pytest

pytest.importorskip("openai")
pytest.importorskip("traceai_openai")

from conftest import b64, flush, png, refs  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from traceai_openai import OpenAIInstrumentor  # noqa: E402

IMAGE = png(seed=31)
URI = "data:image/png;base64," + b64(IMAGE)


@pytest.fixture
def openai_on(spans):
    tracer_provider, exporter = spans
    state = {}

    def install(manager, config=None):
        OpenAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                        config=config or TraceConfig())
        state["on"] = True

    yield install
    if state.get("on"):
        OpenAIInstrumentor().uninstrument()


def sent_image(provider):
    """The image the provider actually received, so the test proves the bytes are the same ones."""
    for path, body, raw in provider.requests:
        if path.endswith("/chat/completions"):
            text = json.dumps(body)
            if b64(IMAGE) in text:
                return True
    return False


def assert_once(f01, manager, provider):
    flush(manager)
    assert sent_image(provider), "the framework did not send the image to the provider"
    assert f01.payloads == [IMAGE], len(f01.payloads)
    assert len(f01.uploads) == 1


# ------------------------------------------------------------------ LangChain + langchain-openai
def test_langchain_openai(f01, provider, spans, openai_on):
    pytest.importorskip("langchain_openai")
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI
    from traceai_langchain import LangChainInstrumentor

    tracer_provider, exporter = spans
    manager = f01.manager()
    LangChainInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager)
    openai_on(manager)
    try:
        model = ChatOpenAI(model="gpt-4o-mini", api_key="sk-synthetic", base_url=provider.url + "/v1",
                           max_retries=0)
        model.invoke([HumanMessage(content=[{"type": "text", "text": "what"},
                                            {"type": "image_url", "image_url": {"url": URI}}])])
    finally:
        LangChainInstrumentor().uninstrument()
    assert_once(f01, manager, provider)
    owners = [s.name for s in exporter.get_finished_spans() if refs(s)]
    assert owners == ["ChatOpenAI"], owners  # the framework span (captured first) owns the occurrence


# ------------------------------------------------------------------ LlamaIndex + llama-index-llms-openai
def test_llamaindex_openai(f01, provider, spans, openai_on):
    pytest.importorskip("llama_index.llms.openai")
    from llama_index.core.base.llms.types import ChatMessage, ImageBlock, TextBlock
    from llama_index.llms.openai import OpenAI
    from traceai_llamaindex import LlamaIndexInstrumentor

    tracer_provider, exporter = spans
    manager = f01.manager()
    LlamaIndexInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager)
    openai_on(manager)
    try:
        llm = OpenAI(model="gpt-4o-mini", api_key="sk-synthetic", api_base=provider.url + "/v1", max_retries=0)
        llm.chat([ChatMessage(role="user", blocks=[TextBlock(text="what"), ImageBlock(image=IMAGE,
                                                                                      image_mimetype="image/png")])])
    finally:
        LlamaIndexInstrumentor().uninstrument()
    assert_once(f01, manager, provider)


# ------------------------------------------------------------------ Agno + OpenAIChat
def test_agno_openai(f01, provider, spans, openai_on, monkeypatch):
    pytest.importorskip("agno")
    pytest.importorskip("openinference.instrumentation.agno")
    from agno.agent import Agent
    from agno.media import Image
    from agno.models.openai import OpenAIChat
    from traceai_agno import AgnoInstrumentorWrapper, configure_agno_tracing

    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    tracer_provider, exporter = spans
    manager = f01.manager()
    configure_agno_tracing(tracer_provider=tracer_provider, attachments=manager)
    openai_on(manager)
    try:
        agent = Agent(model=OpenAIChat(id="gpt-4o-mini", api_key="sk-synthetic", base_url=provider.url + "/v1",
                                       max_retries=0), telemetry=False)
        agent.run("what is this", images=[Image(content=IMAGE, format="png")])
    finally:
        AgnoInstrumentorWrapper().uninstrument()
    assert_once(f01, manager, provider)


# ------------------------------------------------------------------ Pydantic AI + OpenAI model
def test_pydantic_ai_openai(f01, provider, spans, openai_on):
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import Agent, BinaryContent
    from traceai_pydantic_ai import PydanticAIInstrumentor

    try:
        from pydantic_ai.models.openai import OpenAIChatModel as Model
    except ImportError:
        from pydantic_ai.models.openai import OpenAIModel as Model
    from pydantic_ai.providers.openai import OpenAIProvider

    tracer_provider, exporter = spans
    manager = f01.manager()
    PydanticAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager)
    openai_on(manager)
    try:
        model = Model("gpt-4o-mini", provider=OpenAIProvider(base_url=provider.url + "/v1", api_key="sk-synthetic"))
        Agent(model).run_sync(["what is this", BinaryContent(data=IMAGE, media_type="image/png")])
    finally:
        PydanticAIInstrumentor().uninstrument()
    assert_once(f01, manager, provider)


def test_without_the_claim_both_spans_would_upload(f01, provider, spans, openai_on, monkeypatch):
    """Control for the dedup tests: the provider alone uploads the image it sees (claims are per trace)."""
    import openai

    manager = f01.manager()
    openai_on(manager)
    client = openai.OpenAI(api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0)
    for _ in range(2):  # two separate traces
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": URI}}]}])
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
