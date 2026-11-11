"""Framework + OpenAI instrumentor on ONE manager: each observing span owns its occurrence.

Each test drives the real framework against the real OpenAI SDK (loopback provider),
with both the framework instrumentor and ``traceai_openai`` given the same manager.
LangChain and LlamaIndex do not make their span current, so the provider span may be
a separate trace: an occurrence shared across spans could not resolve there (F01
resolve checks the parent). So both spans upload, each references only its own
occurrence, and byte deduplication is left to the server.
"""

import json

import pytest

pytest.importorskip("openai")
pytest.importorskip("traceai_openai")

from conftest import assert_uris_resolve, b64, flush, png, refs  # noqa: E402
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


def assert_each_span_owns_its_occurrence(f01, manager, provider, exporter):
    """Framework span + provider span: two uploads, each parented by and referenced on its own span."""
    flush(manager)
    assert sent_image(provider), "the framework did not send the image to the provider"
    assert f01.payloads == [IMAGE, IMAGE], len(f01.payloads)
    owners = [s for s in exporter.get_finished_spans() if refs(s)]
    assert len(owners) == 2, [s.name for s in owners]
    owned = {r["occurrence_id"]: format(s.context.span_id, "016x") for s in owners for r in refs(s)}
    parents = {b["occurrence_id"]: b["association"]["parent"]["span_id"] for b in f01.uploads}
    assert owned == parents  # every occurrence is referenced on exactly the span it was uploaded under
    assert assert_uris_resolve(exporter) >= 1  # the provider span's fi-attachment URI is its own
    return owners


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
    owners = assert_each_span_owns_its_occurrence(f01, manager, provider, exporter)
    assert "ChatOpenAI" in [s.name for s in owners]


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
    assert_each_span_owns_its_occurrence(f01, manager, provider, exporter)


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
        model = OpenAIChat(id="gpt-4o-mini", api_key="sk-synthetic", base_url=provider.url + "/v1",
                           max_retries=0)
        try:
            agent = Agent(model=model, telemetry=False)
        except TypeError:
            agent = Agent(model=model)
        agent.run("what is this", images=[Image(content=IMAGE, format="png")])
    finally:
        AgnoInstrumentorWrapper().uninstrument()
    assert_each_span_owns_its_occurrence(f01, manager, provider, exporter)


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
    # skip_dep_check: CI installs pydantic-ai-slim, which the instrumentor's "pydantic-ai" requirement does not name.
    PydanticAIInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager, skip_dep_check=True)
    openai_on(manager)
    try:
        model = Model("gpt-4o-mini", provider=OpenAIProvider(base_url=provider.url + "/v1", api_key="sk-synthetic"))
        Agent(model).run_sync(["what is this", BinaryContent(data=IMAGE, media_type="image/png")])
    finally:
        PydanticAIInstrumentor().uninstrument()
    assert_each_span_owns_its_occurrence(f01, manager, provider, exporter)


def test_provider_alone_two_calls_same_image_each_own_their_occurrence(f01, provider, spans, openai_on):
    """Provider only (no framework): two independent calls with the same image on one manager.

    Each call is its own trace. Each span uploads, references its own occurrence, and its
    telemetry URI points at that occurrence (never at the other trace's).
    """
    import openai

    tracer_provider, exporter = spans
    manager = f01.manager()
    openai_on(manager)
    client = openai.OpenAI(api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0)
    for _ in range(2):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": URI}}]}])
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
    owners = [s for s in exporter.get_finished_spans() if refs(s)]
    assert len(owners) == 2 and len({s.context.trace_id for s in owners}) == 2
    assert len({r["occurrence_id"] for s in owners for r in refs(s)}) == 2
    # A span may project its URI into several attributes; each must resolve to its OWN occurrence.
    assert assert_uris_resolve(exporter) >= 2
    for span in owners:
        (own,) = [r["occurrence_id"] for r in refs(span)]
        assert any("fi-attachment:" + own in value for value in (span.attributes or {}).values()
                   if isinstance(value, str)), span.name
