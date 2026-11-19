"""A chain whose output echoes its input must not export that output while an input hide setting is on,
whatever the type or size of the input (short strings, numbers, booleans, documents, models, deep nesting)."""
import json

import pytest
from fi_instrumentation import TraceConfig
from langchain_core.documents import Document
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from traceai_langchain import LangChainInstrumentor

from ._core_version import requires_current_core

pytestmark = requires_current_core

REPLY = "synthetic chain answer"
SETTINGS = ["hide_inputs", "hide_input_messages", "hide_input_text", "hide_input_images"]


class Request(BaseModel):
    secret: str


def _deep():
    value = "SENTINEL_DEEP_LEAF_88"
    for _ in range(12):
        value = {"level": value}
    return value


# name -> (input value, token that must not reach the wire)
CASES = {
    "short_string": ("Qz", "Qz"),
    "number": (12345678, "12345678"),
    "boolean": (True, "ZZ_BOOLEAN_MARKER"),
    "document": (Document(page_content="SENTINEL_DOC_77"), "SENTINEL_DOC_77"),
    "model": (Request(secret="SENTINEL_MODEL_66"), "SENTINEL_MODEL_66"),
    "deeply_nested": (_deep(), "SENTINEL_DEEP_LEAF_88"),
}


def _export(config, value, extra=None):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=config)
    try:
        chain = RunnablePassthrough.assign(answer=RunnableLambda(lambda _: REPLY))
        payload = {"input": value}
        if extra:
            payload.update(extra)
        chain.invoke(payload)
        return exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


def _dump(spans):
    return json.dumps([dict(s.attributes) for s in spans], default=str) + json.dumps(
        [[(e.name, dict(e.attributes or {})) for e in s.events] for s in spans], default=str)


def _chain_outputs(spans):
    return [dict(s.attributes).get("output.value") for s in spans
            if dict(s.attributes).get("gen_ai.span.kind") == "CHAIN"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_echoing_chain_output_is_recorded_with_no_setting(name):
    value, _ = CASES[name]
    assert any('"input"' in str(out) and REPLY in str(out) for out in _chain_outputs(_export(TraceConfig(), value)))


@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("name", sorted(CASES))
def test_the_echoing_chain_output_is_not_recorded_under_input_settings(name, setting):
    value, token = CASES[name]
    spans = _export(TraceConfig(**{setting: True}), value, extra={"ZZ_BOOLEAN_MARKER": True} if name == "boolean" else None)
    for out in _chain_outputs(spans):
        # the echoing output is {"input": <request>, "answer": ...}; the inner answer-only chain may record its own output
        assert out is None or '"input"' not in str(out), (name, setting, out)
    assert token not in _dump(spans), (name, setting)


@pytest.mark.parametrize("setting", SETTINGS)
def test_a_chain_whose_output_does_not_echo_its_input_still_records_it(setting):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**{setting: True}))
    try:
        RunnableLambda(lambda _: REPLY).invoke({"input": "SENTINEL_PRIVATE_REQUEST_55"})
        spans = exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    assert REPLY in _dump(spans)
    assert "SENTINEL_PRIVATE_REQUEST_55" not in _dump(spans)


SETTINGS_ALL = ["hide_inputs", "hide_input_messages", "hide_input_text", "hide_input_images"]


@pytest.mark.parametrize("setting", SETTINGS_ALL)
def test_a_non_echo_output_sharing_a_boolean_and_a_number_with_its_input_is_kept(setting):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**{setting: True}))
    try:
        # The output shares False and 3 with the input but under other keys: a legitimate result.
        RunnableLambda(lambda x: {"verdict": False, "total": 3, "note": REPLY}).invoke({"flag": False, "count": 3, "q": "SENTINEL_Q_91"})
        spans = exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    assert REPLY in _dump(spans)
    assert "SENTINEL_Q_91" not in _dump(spans)


@pytest.mark.parametrize("setting", SETTINGS_ALL)
def test_a_passthrough_that_echoes_a_boolean_or_number_by_key_is_still_dropped(setting):
    spans = _export(TraceConfig(**{setting: True}), False, extra={"count": 3})
    for out in _chain_outputs(spans):
        assert out is None or '"input"' not in str(out)


@pytest.mark.parametrize("setting", SETTINGS_ALL)
def test_a_prompt_template_run_does_not_export_its_formatted_input(setting):
    from langchain_core.prompts import ChatPromptTemplate

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**{setting: True}))
    try:
        ChatPromptTemplate.from_template("Question: {q}").invoke({"q": "SENTINEL_PROMPT_VAR_92"})
        spans = exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    assert spans
    assert "SENTINEL_PROMPT_VAR_92" not in _dump(spans)


def test_a_prompt_template_run_records_its_formatted_prompt_with_no_setting():
    from langchain_core.prompts import ChatPromptTemplate

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = LangChainInstrumentor()
    instrumentor.instrument(tracer_provider=provider, config=TraceConfig())
    try:
        ChatPromptTemplate.from_template("Question: {q}").invoke({"q": "SENTINEL_PROMPT_VAR_92"})
        spans = exporter.get_finished_spans()
    finally:
        instrumentor.uninstrument()
        provider.shutdown()
    assert "SENTINEL_PROMPT_VAR_92" in _dump(spans)
