"""T2-ANTHROPIC-001: tracing must not exhaust messages before the SDK call."""

import asyncio

import opentelemetry.context as context_api
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_anthropic import _wrappers


class _OneShotMessages:
    """An iterable sharing one iterator, without itself implementing __next__."""

    def __init__(self, messages):
        self._iterator = iter(messages)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return self._iterator


class _FakeResponse:
    model = "local-test"
    role = "assistant"
    content = ()
    usage = None
    input_tokens = 3

    def model_dump_json(self):
        return "{}"

    def to_dict(self):
        return {"input_tokens": self.input_tokens}


@pytest.fixture
def tracing():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield provider.get_tracer(__name__), exporter
    finally:
        provider.shutdown()


@pytest.mark.parametrize("operation", ["sync-create", "async-create", "count-tokens"])
@pytest.mark.parametrize(
    "message_factory",
    [
        pytest.param(lambda messages: (message for message in messages), id="generator"),
        pytest.param(iter, id="iterator"),
        pytest.param(_OneShotMessages, id="one-shot-iterable"),
        pytest.param(list, id="list"),
        pytest.param(tuple, id="tuple"),
    ],
)
@pytest.mark.parametrize("suppressed", [False, True], ids=["instrumented", "suppressed"])
def test_messages_iterables_preserve_request_and_telemetry(
    tracing, operation, message_factory, suppressed
):
    tracer, exporter = tracing
    expected = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": [{"type": "text", "text": "follow-up"}]},
    ]
    messages = message_factory(expected)
    kwargs = {"model": "local-test", "messages": messages}
    if operation != "count-tokens":
        kwargs["max_tokens"] = 8
    response = _FakeResponse()
    received = []

    def original(**request):
        # This is the SDK boundary: consume messages only after tracing has run.
        received.append((request, list(request["messages"])))
        return response

    async def async_original(**request):
        return original(**request)

    wrapper_type = {
        "sync-create": _wrappers._MessagesWrapper,
        "async-create": _wrappers._AsyncMessagesWrapper,
        "count-tokens": _wrappers._MessagesCountTokensWrapper,
    }[operation]
    wrapper = wrapper_type(tracer=tracer)
    token = context_api.attach(
        context_api.set_value(context_api._SUPPRESS_INSTRUMENTATION_KEY, suppressed)
    )
    try:
        if operation == "async-create":
            result = asyncio.run(wrapper(async_original, None, (), kwargs))
        else:
            result = wrapper(original, None, (), kwargs)
    finally:
        context_api.detach(token)

    assert result is response
    assert len(received) == 1
    request, sent_messages = received[0]
    assert sent_messages == expected
    assert all(sent is source for sent, source in zip(sent_messages, expected))
    assert kwargs["messages"] is messages  # Do not mutate the caller's mapping.
    assert {key: value for key, value in request.items() if key != "messages"} == {
        key: value for key, value in kwargs.items() if key != "messages"
    }
    if isinstance(messages, _OneShotMessages):
        assert messages.iterations == 1
    if suppressed or isinstance(messages, (list, tuple)):
        assert request["messages"] is messages

    spans = exporter.get_finished_spans()
    if suppressed:
        assert not spans
    else:
        assert len(spans) == 1
        attributes = spans[0].attributes
        for index, message in enumerate(expected):
            prefix = f"{_wrappers.GEN_AI_INPUT_MESSAGES}.{index}"
            assert attributes[f"{prefix}.{_wrappers.MESSAGE_ROLE}"] == message["role"]
        assert attributes[
            f"{_wrappers.GEN_AI_INPUT_MESSAGES}.0.{_wrappers.MESSAGE_CONTENT}"
        ] == "hello"
        assert attributes[
            f"{_wrappers.GEN_AI_INPUT_MESSAGES}.2.{_wrappers.MESSAGE_CONTENT}.0."
            f"{_wrappers.MESSAGE_CONTENT_TEXT}"
        ] == "follow-up"
