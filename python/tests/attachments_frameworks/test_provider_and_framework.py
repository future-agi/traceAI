"""Framework + OpenAI instrumentor on ONE manager: one model call, one owner.

Each test drives the real framework against the real OpenAI SDK (loopback provider). When a
framework adapter captures a model call's media it runs the downstream SDK call under the owner
mark (``media_capture.OWNER_KEY``), so ``traceai_openai`` uploads nothing for that call: each
part is uploaded exactly once, under the framework span. DIFFERENT calls still each own their
occurrence, even with identical bytes (byte deduplication is server-side). With attachments off
for the framework nothing is marked and the provider captures as before.

The mark is set only for a call in which the adapter observes at least one input media part.
Media the adapter never reads (an Agno ``filepath``, a LlamaIndex ``path``) is inlined by the
SDK integration as a data URI, so ``traceai_openai`` must still capture it on the provider span.
"""

import asyncio
import json

import pytest

pytest.importorskip("openai")
pytest.importorskip("traceai_openai")

from conftest import PDF, assert_uris_resolve, b64, flush, png, refs  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402
from traceai_openai import OpenAIInstrumentor  # noqa: E402

IMAGE = png(seed=31)
URI = "data:image/png;base64," + b64(IMAGE)
PATH_IMAGE = png(seed=32)  # only ever read from disk by the framework's SDK integration
OPENAI_SCOPE = "traceai_openai"


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


def sent_image(provider, count=1):
    """The provider actually received the image ``count`` times (the same bytes, not a copy)."""
    seen = sum(1 for path, body, raw in provider.requests
               if path.endswith("/chat/completions") and b64(IMAGE) in json.dumps(body))
    return seen >= count


def owners(exporter):
    return [s for s in exporter.get_finished_spans() if refs(s)]


def is_provider_span(span):
    return getattr(span.instrumentation_scope, "name", "") == OPENAI_SCOPE


def assert_one_owner(f01, manager, provider, exporter):
    """Exactly one upload, parented by and referenced on the framework span; the provider span holds none."""
    flush(manager)
    assert sent_image(provider), "the framework did not send the image to the provider"
    assert f01.payloads == [IMAGE], len(f01.payloads)
    (owner,) = owners(exporter)
    assert not is_provider_span(owner), owner.name
    assert any(is_provider_span(s) for s in exporter.get_finished_spans()), "no provider span: not a 2-layer call"
    (ref,) = refs(owner)
    (upload,) = f01.uploads
    assert upload["occurrence_id"] == ref["occurrence_id"]
    assert upload["association"]["parent"]["span_id"] == format(owner.context.span_id, "016x")
    assert_uris_resolve(exporter)
    return owner


def direct_openai_call(provider):
    import openai

    client = openai.OpenAI(api_key="sk-synthetic", base_url=provider.url + "/v1", max_retries=0)
    client.chat.completions.create(model="m", messages=[{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": URI}}]}])


async def _drain(stream):
    async for _ in stream:
        pass


# ------------------------------------------------------------------ framework drivers
class LangChain:
    name = "langchain"
    modes = ("sync", "stream", "async", "astream")

    def __init__(self, provider, tracer_provider):
        pytest.importorskip("langchain_openai")
        self.provider, self.tracer_provider = provider, tracer_provider

    def instrument(self, manager, config=None):
        from traceai_langchain import LangChainInstrumentor

        kwargs = {"attachments": manager} if manager is not None else {}
        LangChainInstrumentor().instrument(tracer_provider=self.tracer_provider, config=config or TraceConfig(),
                                           **kwargs)

    def uninstrument(self):
        from traceai_langchain import LangChainInstrumentor

        LangChainInstrumentor().uninstrument()

    def _model(self):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model="gpt-4o-mini", api_key="sk-synthetic", base_url=self.provider.url + "/v1",
                          max_retries=0)

    def _messages(self):
        from langchain_core.messages import HumanMessage

        return [HumanMessage(content=[{"type": "text", "text": "what"},
                                      {"type": "image_url", "image_url": {"url": URI}}])]

    def call(self, mode="sync"):
        model, messages = self._model(), self._messages()
        if mode == "sync":
            model.invoke(messages)
        elif mode == "stream":
            for _ in model.stream(messages):
                pass
        elif mode == "async":
            asyncio.run(model.ainvoke(messages))
        else:
            asyncio.run(_drain(model.astream(messages)))

    def open_stream(self):
        stream = self._model().stream(self._messages())
        next(stream)  # the provider request has been made; the stream is suspended mid-call
        return stream


class LlamaIndex:
    name = "llamaindex"
    modes = ("sync", "stream", "async", "astream")

    def __init__(self, provider, tracer_provider):
        pytest.importorskip("llama_index.llms.openai")
        self.provider, self.tracer_provider = provider, tracer_provider

    def instrument(self, manager, config=None):
        from traceai_llamaindex import LlamaIndexInstrumentor

        kwargs = {"attachments": manager} if manager is not None else {}
        LlamaIndexInstrumentor().instrument(tracer_provider=self.tracer_provider, config=config or TraceConfig(),
                                            **kwargs)

    def uninstrument(self):
        from traceai_llamaindex import LlamaIndexInstrumentor

        LlamaIndexInstrumentor().uninstrument()

    def _llm(self):
        from llama_index.llms.openai import OpenAI

        return OpenAI(model="gpt-4o-mini", api_key="sk-synthetic", api_base=self.provider.url + "/v1",
                      max_retries=0)

    def _messages(self):
        from llama_index.core.base.llms.types import ChatMessage, ImageBlock, TextBlock

        return [ChatMessage(role="user", blocks=[TextBlock(text="what"),
                                                 ImageBlock(image=IMAGE, image_mimetype="image/png")])]

    def call(self, mode="sync"):
        llm, messages = self._llm(), self._messages()
        if mode == "sync":
            llm.chat(messages)
        elif mode == "stream":
            for _ in llm.stream_chat(messages):
                pass
        elif mode == "async":
            asyncio.run(llm.achat(messages))
        else:
            async def run():
                await _drain(await llm.astream_chat(messages))
            asyncio.run(run())

    def open_stream(self):
        stream = self._llm().stream_chat(self._messages())
        next(stream)
        return stream

    def path_call(self, path, kind, with_content=False):
        """One chat call with a ``path`` block (never read by the adapter), optionally after a content block."""
        from llama_index.core.base.llms.types import ChatMessage, DocumentBlock, ImageBlock, TextBlock

        blocks = [TextBlock(text="what")]
        if with_content:
            blocks.append(ImageBlock(image=IMAGE, image_mimetype="image/png"))
        blocks.append(ImageBlock(path=path, image_mimetype="image/png") if kind == "image"
                      else DocumentBlock(path=path, document_mimetype="application/pdf"))
        self._llm().chat([ChatMessage(role="user", blocks=blocks)])


class Agno:
    name = "agno"
    modes = ("sync", "stream")

    def __init__(self, provider, tracer_provider):
        pytest.importorskip("agno")
        pytest.importorskip("openinference.instrumentation.agno")
        self.provider, self.tracer_provider = provider, tracer_provider

    def instrument(self, manager, config=None):
        from traceai_agno import configure_agno_tracing

        assert config is None, "Agno takes an OpenInference TraceConfig; not exercised here"
        kwargs = {"attachments": manager} if manager is not None else {}
        configure_agno_tracing(tracer_provider=self.tracer_provider, **kwargs)

    def uninstrument(self):
        from traceai_agno import AgnoInstrumentorWrapper

        AgnoInstrumentorWrapper().uninstrument()

    def _agent(self):
        from agno.agent import Agent
        from agno.models.openai import OpenAIChat

        model = OpenAIChat(id="gpt-4o-mini", api_key="sk-synthetic", base_url=self.provider.url + "/v1",
                           max_retries=0)
        try:
            return Agent(model=model, telemetry=False)
        except TypeError:
            return Agent(model=model)

    def call(self, mode="sync"):
        from agno.media import Image

        images = [Image(content=IMAGE, format="png")]
        if mode == "sync":
            self._agent().run("what is this", images=images)
        else:
            for _ in self._agent().run("what is this", images=images, stream=True):
                pass

    def path_call(self, path, kind, with_content=False):
        """One model call with a ``filepath`` source (never read by the adapter), optionally after content."""
        from agno.media import File, Image

        if kind == "image":
            images = ([Image(content=IMAGE, format="png")] if with_content else []) + [Image(filepath=str(path))]
            self._agent().run("what is this", images=images)
        else:
            images = [Image(content=IMAGE, format="png")] if with_content else None
            self._agent().run("what is this", images=images, files=[File(filepath=str(path))])


class PydanticAI:
    name = "pydantic_ai"
    modes = ("sync", "stream")

    def __init__(self, provider, tracer_provider):
        pytest.importorskip("pydantic_ai")
        self.provider, self.tracer_provider = provider, tracer_provider

    def instrument(self, manager, config=None):
        from traceai_pydantic_ai import PydanticAIInstrumentor

        kwargs = {"attachments": manager} if manager is not None else {}
        # skip_dep_check: CI installs pydantic-ai-slim, which the instrumentor's "pydantic-ai" requirement
        # does not name.
        PydanticAIInstrumentor().instrument(tracer_provider=self.tracer_provider, config=config or TraceConfig(),
                                            skip_dep_check=True, **kwargs)

    def uninstrument(self):
        from traceai_pydantic_ai import PydanticAIInstrumentor

        PydanticAIInstrumentor().uninstrument()

    def _agent(self):
        from pydantic_ai import Agent

        try:
            from pydantic_ai.models.openai import OpenAIChatModel as Model
        except ImportError:
            from pydantic_ai.models.openai import OpenAIModel as Model
        from pydantic_ai.providers.openai import OpenAIProvider

        return Agent(Model("gpt-4o-mini", provider=OpenAIProvider(base_url=self.provider.url + "/v1",
                                                                  api_key="sk-synthetic")))

    def _prompt(self):
        from pydantic_ai import BinaryContent

        return ["what is this", BinaryContent(data=IMAGE, media_type="image/png")]

    def call(self, mode="sync", body=None):
        if mode == "sync":
            self._agent().run_sync(self._prompt())
            return

        async def run():
            async with self._agent().run_stream(self._prompt()):
                if body is not None:
                    body()  # the caller's own code inside the stream's ``async with``
        asyncio.run(run())


FRAMEWORKS = {cls.name: cls for cls in (LangChain, LlamaIndex, Agno, PydanticAI)}
CALLS = [(name, mode) for name, cls in FRAMEWORKS.items() for mode in cls.modes]


@pytest.fixture
def framework(request, provider, spans):
    tracer_provider, _ = spans
    driver = FRAMEWORKS[request.param](provider, tracer_provider)
    state = {}
    original = driver.instrument

    def instrument(*args, **kwargs):
        original(*args, **kwargs)
        state["on"] = True

    driver.instrument = instrument
    yield driver
    if state.get("on"):
        driver.uninstrument()


# ------------------------------------------------------------------ one call, one owner
@pytest.mark.parametrize("framework, mode", CALLS, indirect=["framework"])
def test_framework_and_provider_one_call_uploads_each_part_once(f01, provider, spans, openai_on, framework, mode):
    """Was 2 (framework span + provider span); the provider instrumentor now skips the owned call."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    framework.call(mode)
    assert_one_owner(f01, manager, provider, exporter)


@pytest.mark.parametrize("framework", list(FRAMEWORKS), indirect=True)
def test_framework_alone_still_uploads_inside_its_own_mark(f01, provider, spans, framework):
    """Control: the adapter's own capture must not be suppressed by the mark it sets."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    framework.call()
    flush(manager)
    assert sent_image(provider)
    assert f01.payloads == [IMAGE]
    (owner,) = owners(exporter)
    assert not is_provider_span(owner)


@pytest.mark.parametrize("framework", list(FRAMEWORKS), indirect=True)
def test_framework_attachments_off_marks_nothing(f01, provider, spans, openai_on, framework):
    """Attachments off for the framework: no mark, so the provider captures exactly as before."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(None)
    openai_on(manager)
    framework.call()
    flush(manager)
    assert sent_image(provider)
    assert f01.payloads == [IMAGE]
    (owner,) = owners(exporter)
    assert is_provider_span(owner)


# ------------------------------------------------------------------ different calls
@pytest.mark.parametrize("framework", list(FRAMEWORKS), indirect=True)
def test_same_bytes_in_two_framework_calls_are_two_occurrences(f01, provider, spans, openai_on, framework):
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    framework.call()
    framework.call()
    flush(manager)
    assert sent_image(provider, 2)
    assert f01.payloads == [IMAGE, IMAGE]
    found = owners(exporter)
    assert len(found) == 2 and not any(is_provider_span(s) for s in found)
    assert len({r["occurrence_id"] for s in found for r in refs(s)}) == 2
    assert_uris_resolve(exporter)


@pytest.mark.parametrize("framework", list(FRAMEWORKS), indirect=True)
def test_framework_call_then_direct_provider_call_each_own_one(f01, provider, spans, openai_on, framework):
    """The mark ends with the framework call: a later direct SDK call with the same bytes uploads its own."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    framework.call()
    direct_openai_call(provider)
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
    found = owners(exporter)
    assert sorted(is_provider_span(s) for s in found) == [False, True]
    assert_uris_resolve(exporter)


@pytest.mark.parametrize("framework", ["langchain", "llamaindex"], indirect=True)
def test_an_abandoned_framework_stream_leaves_no_mark(f01, provider, spans, openai_on, framework):
    """A stream suspended mid-call (or dropped) never leaves the mark on the caller's context."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    stream = framework.open_stream()
    direct_openai_call(provider)  # while the framework stream is suspended
    stream.close()
    direct_openai_call(provider)  # after it was abandoned
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE, IMAGE]
    found = owners(exporter)
    assert sorted(is_provider_span(s) for s in found) == [False, True, True]


@pytest.mark.parametrize("framework", ["pydantic_ai"], indirect=True)
def test_the_callers_stream_body_is_not_marked(f01, provider, spans, openai_on, framework):
    """Pydantic AI marks entering and exiting the stream, never the caller's ``async with`` body."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    framework.call("stream", body=lambda: direct_openai_call(provider))
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
    found = owners(exporter)
    assert sorted(is_provider_span(s) for s in found) == [False, True]


# ------------------------------------------------------------------ media only the SDK reads
PATH_SOURCES = [("agno", "image"), ("agno", "file"), ("llamaindex", "image"), ("llamaindex", "document")]


def sent(provider, data):
    return any(path.endswith("/chat/completions") and b64(data) in json.dumps(body)
               for path, body, raw in provider.requests)


def _path(tmp_path, kind):
    data = PATH_IMAGE if kind == "image" else PDF
    path = tmp_path / ("source.png" if kind == "image" else "source.pdf")
    path.write_bytes(data)
    return path, data


@pytest.mark.parametrize("framework, kind", PATH_SOURCES, indirect=["framework"])
def test_a_source_only_the_sdk_reads_is_still_captured_by_the_provider(f01, provider, spans, openai_on,
                                                                        framework, kind, tmp_path):
    """The adapter never opens a path, the SDK integration inlines it: no mark, ONE upload, on the provider span.

    With the mark set on every call (16311a37..1080f58d) this was 0: nobody captured it.
    """
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    path, data = _path(tmp_path, kind)
    framework.path_call(path, kind)
    flush(manager)
    assert sent(provider, data), "the SDK integration did not inline the path source"
    assert f01.payloads == [data], len(f01.payloads)
    (owner,) = owners(exporter)
    assert is_provider_span(owner), owner.name
    (ref,) = refs(owner)
    (upload,) = f01.uploads
    assert upload["occurrence_id"] == ref["occurrence_id"]
    assert upload["association"]["parent"]["span_id"] == format(owner.context.span_id, "016x")
    assert_uris_resolve(exporter)


@pytest.mark.parametrize("framework, kind", PATH_SOURCES, indirect=["framework"])
def test_the_framework_alone_never_reads_a_path_source(f01, provider, spans, framework, kind, tmp_path):
    """Negative control for the row above: without traceai_openai the path source is uploaded by nobody."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    path, data = _path(tmp_path, kind)
    framework.path_call(path, kind)
    flush(manager)
    assert sent(provider, data)
    assert f01.payloads == [] and owners(exporter) == []


@pytest.mark.parametrize("framework", ["agno", "llamaindex"], indirect=True)
def test_residual_a_call_mixing_an_observed_part_and_a_path_part_is_marked(f01, provider, spans, openai_on,
                                                                           framework, tmp_path):
    """Disclosed residual: the adapter observes the content part, so the WHOLE call is marked and the
    provider skips the path part too. One upload (the content part), on the framework span."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    openai_on(manager)
    path, data = _path(tmp_path, "image")
    framework.path_call(path, "image", with_content=True)
    flush(manager)
    assert sent(provider, IMAGE) and sent(provider, data)
    assert f01.payloads == [IMAGE], len(f01.payloads)
    (owner,) = owners(exporter)
    assert not is_provider_span(owner), owner.name


# ------------------------------------------------------------------ the mark carries the framework's deny
@pytest.mark.parametrize("framework", ["langchain", "llamaindex", "pydantic_ai"], indirect=True)
def test_framework_hide_inputs_is_not_bypassed_through_the_provider(f01, provider, spans, openai_on, framework):
    """The framework hides its input: it uploads nothing, and the provider beneath it does not either."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager, config=TraceConfig(hide_inputs=True))
    openai_on(manager)
    framework.call()
    flush(manager)
    assert sent_image(provider)
    assert f01.payloads == []
    assert owners(exporter) == []


def test_provider_alone_two_calls_same_image_each_own_their_occurrence(f01, provider, spans, openai_on):
    """Provider only (no framework): two independent calls with the same image on one manager.

    Each call is its own trace. Each span uploads, references its own occurrence, and its
    telemetry URI points at that occurrence (never at the other trace's).
    """
    _, exporter = spans
    manager = f01.manager()
    openai_on(manager)
    for _ in range(2):
        direct_openai_call(provider)
    flush(manager)
    assert f01.payloads == [IMAGE, IMAGE]
    found = owners(exporter)
    assert len(found) == 2 and len({s.context.trace_id for s in found}) == 2
    assert len({r["occurrence_id"] for s in found for r in refs(s)}) == 2
    # A span may project its URI into several attributes; each must resolve to its OWN occurrence.
    assert assert_uris_resolve(exporter) >= 2
    for span in found:
        (own,) = [r["occurrence_id"] for r in refs(span)]
        assert any("fi-attachment:" + own in value for value in (span.attributes or {}).values()
                   if isinstance(value, str)), span.name
