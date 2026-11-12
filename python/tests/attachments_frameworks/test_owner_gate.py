"""The owner mark is set only for a call in which the adapter observes at least one input media part.

A call with no observed part (text only, an Agno ``filepath``, a LlamaIndex ``path``, a provider
file ID) runs unmarked, so a provider instrumentor beneath the framework still captures what only
the SDK sees. Every gate fails open: an error means no mark, never a suppressed provider.
"""

import pytest

from conftest import b64, png
from fi_instrumentation import TraceConfig
from fi_instrumentation.attachments import media_capture as mc

IMAGE = png(seed=41)
URI = "data:image/png;base64," + b64(IMAGE)


# ------------------------------------------------------------------ LangChain
@pytest.fixture
def langchain_probe(spans):
    pytest.importorskip("langchain_core")
    pytest.importorskip("traceai_langchain")
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from traceai_langchain import LangChainInstrumentor

    seen = []

    class Probe(BaseChatModel):
        """Records whether the provider call beneath LangChain runs under the owner mark."""

        @property
        def _llm_type(self) -> str:
            return "owner-probe"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            seen.append(mc.owned_elsewhere())
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    tracer_provider, _ = spans
    state = {}

    def install(manager):
        LangChainInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                           config=TraceConfig())
        state["on"] = True
        return Probe(), seen

    yield install
    if state.get("on"):
        LangChainInstrumentor().uninstrument()


def test_langchain_marks_only_a_call_with_an_observed_part(f01, langchain_probe):
    from langchain_core.messages import HumanMessage

    model, seen = langchain_probe(f01.manager())
    image = HumanMessage(content=[{"type": "text", "text": "what"}, {"type": "image_url", "image_url": {"url": URI}}])
    provider_file = HumanMessage(content=[{"type": "file", "file": {"file_id": "file-synthetic"}}])
    model.invoke("text only")
    model.invoke([HumanMessage(content="text only")])
    model.invoke([provider_file])
    model.invoke([image])
    for _ in model.stream("text only"):
        pass
    for _ in model.stream([image]):
        pass
    assert seen == [False, False, False, True, False, True]


def test_langchain_gate_fails_open():
    pytest.importorskip("traceai_langchain")
    from traceai_langchain._attachments import observes_langchain_input

    class Broken:
        def _convert_input(self, value):
            raise RuntimeError("cannot convert")

    assert observes_langchain_input("stream", Broken(), ([{"role": "user"}],), {}) is False
    assert observes_langchain_input("generate", None, (object(),), {}) is False
    assert observes_langchain_input("generate", None, (), {}) is False


# ------------------------------------------------------------------ LlamaIndex
@pytest.fixture
def llamaindex_probe(spans):
    pytest.importorskip("llama_index.core")
    pytest.importorskip("traceai_llamaindex")
    from llama_index.core.base.llms.types import ChatMessage, ChatResponse, CompletionResponse, LLMMetadata
    from llama_index.core.llms.callbacks import llm_chat_callback, llm_completion_callback
    from llama_index.core.llms.custom import CustomLLM
    from traceai_llamaindex import LlamaIndexInstrumentor

    seen = []

    class Probe(CustomLLM):
        @property
        def metadata(self) -> LLMMetadata:
            return LLMMetadata(model_name="owner-probe", is_chat_model=True)

        @llm_chat_callback()
        def chat(self, messages, **kwargs):
            seen.append(mc.owned_elsewhere())
            return ChatResponse(message=ChatMessage(role="assistant", content="ok"))

        @llm_completion_callback()
        def complete(self, prompt, formatted=False, **kwargs):
            return CompletionResponse(text="ok")

        @llm_completion_callback()
        def stream_complete(self, prompt, formatted=False, **kwargs):
            yield CompletionResponse(text="ok", delta="ok")

    tracer_provider, _ = spans
    state = {}

    def install(manager):
        LlamaIndexInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager,
                                            config=TraceConfig())
        state["on"] = True
        return Probe(), seen

    yield install
    if state.get("on"):
        LlamaIndexInstrumentor().uninstrument()


def test_llamaindex_marks_only_a_call_with_an_observed_part(f01, llamaindex_probe, tmp_path):
    from llama_index.core.base.llms.types import ChatMessage, ImageBlock, TextBlock

    llm, seen = llamaindex_probe(f01.manager())
    path = tmp_path / "source.png"
    path.write_bytes(IMAGE)
    llm.chat([ChatMessage(role="user", content="text only")])
    llm.chat([ChatMessage(role="user", blocks=[TextBlock(text="what"),
                                                ImageBlock(path=path, image_mimetype="image/png")])])
    llm.chat([ChatMessage(role="user", blocks=[ImageBlock(image=IMAGE, image_mimetype="image/png")])])
    llm.chat(messages=[ChatMessage(role="user", blocks=[ImageBlock(image=IMAGE, image_mimetype="image/png")])])
    assert seen == [False, False, True, True]


def test_llamaindex_gate_fails_open():
    pytest.importorskip("traceai_llamaindex")
    from traceai_llamaindex._attachments import observes_llamaindex_messages

    assert observes_llamaindex_messages(None) is False
    assert observes_llamaindex_messages("text") is False
    assert observes_llamaindex_messages([object()]) is False


# ------------------------------------------------------------------ Agno
def test_agno_gate_skips_a_filepath_only_call(tmp_path):
    pytest.importorskip("agno")
    pytest.importorskip("traceai_agno")
    from agno.media import Image
    from agno.models.message import Message
    from traceai_agno._attachments import observes_agno_input

    path = tmp_path / "source.png"
    path.write_bytes(IMAGE)
    assert observes_agno_input([Message(role="user", content="text only")]) is False
    assert observes_agno_input([Message(role="user", content="x", images=[Image(filepath=str(path))])]) is False
    assert observes_agno_input([Message(role="user", content="x", images=[Image(content=IMAGE, format="png")])])
    assert observes_agno_input(None) is False and observes_agno_input([object()]) is False


# ------------------------------------------------------------------ Pydantic AI
def test_pydantic_ai_gate_marks_only_a_request_with_an_observed_part():
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("traceai_pydantic_ai")
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from traceai_pydantic_ai._attachments import PydanticAIMedia

    media = PydanticAIMedia(None, None)
    text = ModelRequest(parts=[UserPromptPart(content="text only")])
    image = ModelRequest(parts=[UserPromptPart(content=["what", BinaryContent(data=IMAGE, media_type="image/png")])])
    assert media.observes_request([text]) is False
    assert media.observes_request([text, image]) is True
    assert media.observes_request(None) is False and media.observes_request([object()]) is False


def test_pydantic_ai_call_owner_follows_the_gate(f01):
    """The wrapper marks only a request the adapter observes; a text-only request runs unmarked."""
    pytest.importorskip("pydantic_ai")
    pytest.importorskip("traceai_pydantic_ai")
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from traceai_pydantic_ai._attachments import PydanticAIMedia
    from traceai_pydantic_ai._execution_wrappers import _call_owner

    media = PydanticAIMedia(f01.manager(), None)
    text = ModelRequest(parts=[UserPromptPart(content="text only")])
    image = ModelRequest(parts=[UserPromptPart(content=["what", BinaryContent(data=IMAGE, media_type="image/png")])])
    assert _call_owner(media, [text]) is None
    assert isinstance(_call_owner(media, [text, image]), mc.CallOwner)
    assert _call_owner(None, [image]) is None
