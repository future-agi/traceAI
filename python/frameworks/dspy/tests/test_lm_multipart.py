"""Offline public LM calls through the real model SDK and HTTP response parser."""

import importlib
import importlib.util
import inspect
import json
import socket
from types import SimpleNamespace

import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_dspy import DSPyInstrumentor, _eval_input


TEXT = "synthetic question"
ANSWER = "synthetic answer"
PARTS = [{"type": "text", "text": TEXT}, {"type": "text", "text": "second part"}]


@pytest.fixture
def public_lm(monkeypatch, tmp_path):
    # No application credentials, disk cache, retries, telemetry, or real sockets.
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-not-a-credential")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("DSPY_CACHEDIR", str(tmp_path / "cache"))
    monkeypatch.setenv("DO_NOT_TRACK", "1")

    def no_network(*args, **kwargs):
        raise AssertionError("unexpected network access in offline test")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    import dspy

    requests = []

    def transport(module):
        def respond(self, request):
            assert request.url.host == "offline.invalid"
            assert request.url.path == "/v1/chat/completions"
            requests.append(json.loads(request.content))
            return module.Response(
                200,
                request=request,
                json={
                    "id": "chatcmpl_synthetic",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {
                        "role": "assistant", "content": ANSWER,
                    }}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                },
            )
        return respond

    # SDK releases use either HTTP transport; no model method or parser is mocked.
    for name in ("httpx", "httpx2"):
        if importlib.util.find_spec(name) is not None:
            module = importlib.import_module(name)
            monkeypatch.setattr(module.HTTPTransport, "handle_request", transport(module))

    kwargs = {"engine": "litellm"} if "engine" in inspect.signature(dspy.LM).parameters else {}
    lm = dspy.LM(
        "openai/gpt-4o", cache=False, num_retries=0,
        api_base="https://offline.invalid/v1", **kwargs,
    )
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = DSPyInstrumentor()
    missing = object()
    targets = [
        (dspy.LM, "__call__"), (dspy.Predict, "forward"),
        (dspy.Retrieve, "forward"), (dspy.Module, "__call__"),
        (dspy.ColBERTv2, "__call__"), (dspy.Adapter, "__call__"),
        (dspy.Embedder, "__call__"),
        *((cls, "forward") for cls in dspy.Predict.__subclasses__()),
    ]
    originals = [(cls, name, vars(cls).get(name, missing)) for cls, name in targets]

    def enable(hidden=False):
        instrumentor.instrument(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden,
                               hide_llm_invocation_parameters=True),
        )
        assert instrumentor.is_instrumented_by_opentelemetry

    try:
        yield SimpleNamespace(lm=lm, requests=requests, exporter=exporter, enable=enable)
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
        # Keep cases isolated despite older releases' partial public teardown.
        for cls, name, original in originals:
            if original is missing:
                if name in vars(cls):
                    delattr(cls, name)
            else:
                setattr(cls, name, original)
        provider.shutdown()


def assert_call(public_lm, messages, instrumented):
    public_lm.requests.clear()
    public_lm.exporter.clear()
    assert public_lm.lm(messages=messages) == [ANSWER]
    assert len(public_lm.requests) == 1
    assert public_lm.requests[0]["messages"] == messages
    spans = public_lm.exporter.get_finished_spans()
    assert len(spans) == int(instrumented)
    if instrumented:
        assert spans[0].name == "LM.__call__"
        assert spans[0].status.status_code == StatusCode.OK
        assert spans[0].end_time is not None
        assert "synthetic-not-a-credential" not in json.dumps(dict(spans[0].attributes))
    return spans


@pytest.mark.parametrize("content", [TEXT, PARTS], ids=["string", "multipart"])
def test_public_lm_native_messages_reach_http(public_lm, content):
    assert_call(public_lm, [{"role": "user", "content": content}], False)


def test_public_lm_string_messages_reach_http_when_instrumented(public_lm):
    public_lm.enable()
    assert_call(public_lm, [{"role": "user", "content": TEXT}], True)


@pytest.mark.parametrize("hidden", [False, True], ids=["visible", "hidden"])
def test_public_lm_multipart_messages_reach_http_when_instrumented(public_lm, hidden):
    messages = [{"role": "user", "content": PARTS}]
    assert_call(public_lm, messages, False)
    public_lm.enable(hidden)
    spans = assert_call(public_lm, messages, True)
    attributes = dict(spans[0].attributes)
    if hidden:
        encoded = json.dumps(attributes)
        for value in (TEXT, "second part", ANSWER):
            assert value not in encoded
        assert not any(key.startswith((SpanAttributes.GEN_AI_INPUT_MESSAGES,
                                       SpanAttributes.GEN_AI_OUTPUT_MESSAGES))
                       for key in attributes)
    else:
        assert json.loads(attributes[SpanAttributes.INPUT_VALUE])["messages"] == messages
        assert json.loads(attributes[SpanAttributes.OUTPUT_VALUE]) == [ANSWER]


def test_public_lm_string_messages_respect_hidden_io(public_lm):
    public_lm.enable(hidden=True)
    spans = assert_call(public_lm, [{"role": "user", "content": TEXT}], True)
    encoded = json.dumps(dict(spans[0].attributes))
    assert TEXT not in encoded
    assert ANSWER not in encoded


class UnconvertibleMessage(dict):
    """JSON-valid input whose optional tracing conversion is unavailable."""

    def to_dict(self):
        raise ValueError("synthetic tracing conversion failure")


def test_public_lm_input_attribute_failure_does_not_abort_dispatch(public_lm):
    messages = [UnconvertibleMessage(role="user", content=TEXT)]
    assert_call(public_lm, messages, False)
    public_lm.enable()
    assert_call(public_lm, messages, True)


@pytest.mark.parametrize(
    "messages, expected",
    [
        ([{"content": "first"}, {"content": "second"}], "first\nsecond"),
        ([{"content": PARTS}], json.dumps(PARTS)),
        ([{"content": "first"}, {"content": PARTS}], "first\n" + json.dumps(PARTS)),
        ([{"content": None}], "null"),
        ([{"role": "assistant"}], ""),
    ],
    ids=["strings", "multipart", "mixed", "null", "missing"],
)
def test_eval_input_serializes_message_content(messages, expected):
    assert json.loads(dict(_eval_input(messages))[SpanAttributes.INPUT_VALUE]) == expected


def test_eval_input_without_messages():
    assert dict(_eval_input(None)) == {}
    assert dict(_eval_input([])) == {}
