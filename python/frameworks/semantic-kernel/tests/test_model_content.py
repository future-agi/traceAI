"""Prompts and completions on model-call (chat) spans with ``sensitive=True``.

Semantic Kernel writes them as INFO log records inside the chat span
(``model_diagnostics/decorators.py`` ``_set_completion_input`` /
``_set_completion_response``), not as span attributes. ``instrument(sensitive=True)``
puts a proxy in front of that module's logger that also records them on the
span; the real logger still gets every call. Runs the real OpenAI connector
against the loopback fake server.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from _fake_openai import FakeOpenAI
from traceai_semantic_kernel import SemanticKernelInstrumentor

DECORATORS = "semantic_kernel.utils.telemetry.model_diagnostics.decorators"
PROMPT = "IN-MARKER what is the weather"
REPLY = "OUT-MARKER sunny"
HIDE_ENV = ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS")


@pytest.fixture()
def instrumentor(monkeypatch):
    for key in HIDE_ENV:
        monkeypatch.delenv(key, raising=False)
    inst = SemanticKernelInstrumentor()
    yield inst
    inst.uninstrument()


@pytest.fixture()
def provider():
    tp = TracerProvider()
    exporter = InMemorySpanExporter()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    tp.exporter = exporter  # type: ignore[attr-defined]
    return tp


def _chat(fake, stream: bool = False):
    from openai import AsyncOpenAI
    from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion, OpenAIChatPromptExecutionSettings
    from semantic_kernel.contents import ChatHistory

    client = AsyncOpenAI(api_key="placeholder", base_url=fake.base_url, max_retries=0)
    service = OpenAIChatCompletion(ai_model_id="gpt-4o-mini", async_client=client)
    history = ChatHistory(system_message="Be brief.")
    history.add_user_message(PROMPT)
    settings = OpenAIChatPromptExecutionSettings()

    async def run():
        if not stream:
            return await service.get_chat_message_contents(history, settings)
        return [chunk async for chunk in service.get_streaming_chat_message_contents(history, settings)]

    return asyncio.run(run())


def _chat_span(provider):
    spans = [s for s in provider.exporter.get_finished_spans() if s.name.startswith("chat ")]
    assert len(spans) == 1, [s.name for s in provider.exporter.get_finished_spans()]
    return dict(spans[0].attributes)


@pytest.mark.parametrize("stream", [False, True], ids=["unary", "streaming"])
def test_sensitive_chat_span_carries_prompt_and_completion(instrumentor, provider, stream):
    instrumentor.instrument(tracer_provider=provider, sensitive=True)
    with FakeOpenAI(text_reply=REPLY) as fake:
        _chat(fake, stream=stream)
    attrs = _chat_span(provider)

    inputs = json.loads(attrs["gen_ai.input.messages"])
    assert [m["role"] for m in inputs] == ["system", "user"]
    assert inputs[1]["content"] == PROMPT
    outputs = json.loads(attrs["gen_ai.output.messages"])
    assert outputs[0]["role"] == "assistant"
    assert REPLY in json.dumps(outputs)
    assert attrs["input.value"] == attrs["gen_ai.input.messages"]
    # One completion: output.value is that message object, not a one-item list.
    # Observe's chat view renders an object {role, content} as the assistant
    # reply and shows nothing for a list (ChatMessageView outputMessage).
    assert json.loads(attrs["output.value"]) == outputs[0]
    assert attrs["input.mime_type"] == attrs["output.mime_type"] == "application/json"


@pytest.mark.parametrize(
    "env, hidden, kept",
    [
        ({"FI_HIDE_INPUTS": "true"}, ("gen_ai.input.messages", "input.value"), ("gen_ai.output.messages", "output.value")),
        ({"FI_HIDE_OUTPUTS": "true"}, ("gen_ai.output.messages", "output.value"), ("gen_ai.input.messages", "input.value")),
    ],
    ids=["hide_inputs", "hide_outputs"],
)
def test_hide_flags_apply_to_chat_content(instrumentor, provider, monkeypatch, env, hidden, kept):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    instrumentor.instrument(tracer_provider=provider, sensitive=True)
    with FakeOpenAI(text_reply=REPLY) as fake:
        _chat(fake)
    attrs = _chat_span(provider)
    for key in hidden:
        assert key not in attrs, key
    for key in kept:
        assert key in attrs, key
    marker = "IN-MARKER" if "FI_HIDE_INPUTS" in env else "OUT-MARKER"
    assert marker not in str(attrs)


def test_not_sensitive_leaves_the_logger_and_spans_alone(instrumentor, provider):
    module = importlib.import_module(DECORATORS)
    original = module.logger
    instrumentor.instrument(tracer_provider=provider, sensitive=False)
    assert module.logger is original
    with FakeOpenAI(text_reply=REPLY) as fake:
        _chat(fake)
    attrs = _chat_span(provider)
    assert not {"gen_ai.input.messages", "gen_ai.output.messages", "input.value", "output.value"} & set(attrs)


def test_records_still_reach_the_real_logger(instrumentor, provider, caplog):
    instrumentor.instrument(tracer_provider=provider, sensitive=True)
    with caplog.at_level(logging.INFO, logger=DECORATORS):
        with FakeOpenAI(text_reply=REPLY) as fake:
            _chat(fake)
    events = [getattr(r, "event.name", None) for r in caplog.records if r.name == DECORATORS]
    assert events.count("gen_ai.user.message") == 1
    assert events.count("gen_ai.choice") == 1


def test_uninstrument_restores_the_logger(provider):
    module = importlib.import_module(DECORATORS)
    original = module.logger
    inst = SemanticKernelInstrumentor()
    inst.instrument(tracer_provider=provider, sensitive=True)
    assert module.logger is not original
    inst.uninstrument()
    assert module.logger is original
