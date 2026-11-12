"""Framework + Anthropic instrumentor on ONE manager: one model call, one owner.

The owner mark is provider-neutral: ``traceai_anthropic`` makes the same core check as
``traceai_openai``, so a framework call that captured its media is not uploaded a second time
by the Anthropic provider span. Driven through LangChain's ``ChatAnthropic`` against the real
Anthropic SDK (loopback provider). Latest-only: skipped where ``langchain-anthropic`` is not
installed.

Not covered here:
- Pydantic AI's Anthropic model calls ``client.beta.messages``, which ``traceai_anthropic`` does
  not instrument, so that pair has no provider span to double-count;
- llama-index-llms-anthropic 0.12.3 fails before any request against anthropic 2.8.0
  (``Cannot instantiate typing.Union`` building ``MessageParam``), with or without traceAI.
"""

import json

import pytest

pytest.importorskip("anthropic")
pytest.importorskip("traceai_anthropic")

from conftest import assert_uris_resolve, b64, flush, png, refs  # noqa: E402
from fi_instrumentation import TraceConfig  # noqa: E402

IMAGE = png(seed=41)
URI = "data:image/png;base64," + b64(IMAGE)
ANTHROPIC_SCOPE = "traceai_anthropic"
MODEL = "claude-haiku-4-5"  # the loopback answers any model id


@pytest.fixture
def anthropic_on(spans):
    from traceai_anthropic import AnthropicInstrumentor

    tracer_provider, _ = spans
    state = {}

    def install(manager):
        AnthropicInstrumentor().instrument(tracer_provider=tracer_provider, attachments=manager, config=TraceConfig())
        state["on"] = True

    yield install
    if state.get("on"):
        AnthropicInstrumentor().uninstrument()


class LangChain:
    def __init__(self, provider, tracer_provider):
        pytest.importorskip("traceai_langchain")
        pytest.importorskip("langchain_anthropic")
        self.provider, self.tracer_provider = provider, tracer_provider

    def instrument(self, manager):
        from traceai_langchain import LangChainInstrumentor

        kwargs = {"attachments": manager} if manager is not None else {}
        LangChainInstrumentor().instrument(tracer_provider=self.tracer_provider, config=TraceConfig(), **kwargs)

    def uninstrument(self):
        from traceai_langchain import LangChainInstrumentor

        LangChainInstrumentor().uninstrument()

    def call(self, mode):
        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage

        model = ChatAnthropic(model=MODEL, api_key="sk-ant-synthetic", base_url=self.provider.url, max_retries=0,
                              max_tokens=16)
        messages = [HumanMessage(content=[{"type": "text", "text": "what"},
                                          {"type": "image_url", "image_url": {"url": URI}}])]
        if mode == "sync":
            model.invoke(messages)
        else:
            for _ in model.stream(messages):
                pass


FRAMEWORKS = {"langchain": LangChain}


@pytest.fixture
def framework(request, provider, spans):
    tracer_provider, _ = spans
    driver = FRAMEWORKS[request.param](provider, tracer_provider)
    state = {}
    original = driver.instrument

    def instrument(manager):
        original(manager)
        state["on"] = True

    driver.instrument = instrument
    yield driver
    if state.get("on"):
        driver.uninstrument()


def sent_image(provider):
    return any(path.endswith("/messages") and b64(IMAGE) in json.dumps(body) for path, body, raw in provider.requests)


def owners(exporter):
    return [s for s in exporter.get_finished_spans() if refs(s)]


def is_provider_span(span):
    return getattr(span.instrumentation_scope, "name", "") == ANTHROPIC_SCOPE


@pytest.mark.parametrize("framework", list(FRAMEWORKS), indirect=True)
@pytest.mark.parametrize("mode", ["sync", "stream"])
def test_framework_and_anthropic_one_call_uploads_each_part_once(f01, provider, spans, anthropic_on, framework, mode):
    """Without the mark this is 2 (framework span + Anthropic span)."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(manager)
    anthropic_on(manager)
    framework.call(mode)
    flush(manager)
    assert sent_image(provider), "the framework did not send the image to the provider"
    assert any(is_provider_span(s) for s in exporter.get_finished_spans()), "no Anthropic span: not a 2-layer call"
    assert f01.payloads == [IMAGE], len(f01.payloads)
    (owner,) = owners(exporter)
    assert not is_provider_span(owner), owner.name
    (ref,) = refs(owner)
    (upload,) = f01.uploads
    assert upload["occurrence_id"] == ref["occurrence_id"]
    assert upload["association"]["parent"]["span_id"] == format(owner.context.span_id, "016x")
    assert_uris_resolve(exporter)


@pytest.mark.parametrize("framework", list(FRAMEWORKS), indirect=True)
def test_framework_attachments_off_leaves_the_anthropic_span_the_owner(f01, provider, spans, anthropic_on, framework):
    """Control: no framework manager, no mark, so the Anthropic provider span captures the part."""
    _, exporter = spans
    manager = f01.manager()
    framework.instrument(None)
    anthropic_on(manager)
    framework.call("sync")
    flush(manager)
    assert sent_image(provider)
    assert f01.payloads == [IMAGE], len(f01.payloads)
    (owner,) = owners(exporter)
    assert is_provider_span(owner), owner.name
    assert_uris_resolve(exporter)
