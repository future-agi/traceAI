"""Privacy and lifecycle contracts using real SDK classes and synthetic HTTP."""

import asyncio
import json
from importlib.metadata import requires

import cohere
import httpx
import pytest
from fi_instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from packaging.requirements import Requirement

from traceai_cohere import CohereInstrumentor

INPUT = "SYNTHETIC_PRIVATE_INPUT_346"
OUTPUT = "SYNTHETIC_PRIVATE_OUTPUT_346"
ERROR = "SYNTHETIC_PRIVATE_ERROR_346"


class Started(SpanProcessor):
    def __init__(self):
        self.spans = []

    def on_start(self, span, parent_context=None):
        self.spans.append(span)


@pytest.fixture
def tracing():
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    started = Started()
    provider.add_span_processor(started)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = CohereInstrumentor()

    def setup(**options):
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**options))
        return provider, exporter, started

    yield setup
    instrumentor.uninstrument()
    provider.shutdown()


def client_class(version, asynchronous=False):
    name = ("AsyncClient" if asynchronous else "Client") + (
        "V2" if version == 2 else ""
    )
    if not hasattr(cohere, name):
        pytest.skip("v2 not available at SDK floor")
    return getattr(cohere, name)


def request_args(version, operation="chat"):
    if operation == "embed":
        return dict(
            model="embed-v4.0",
            texts=[INPUT],
            input_type="search_document",
            embedding_types=["float"],
        )
    if operation == "rerank":
        return dict(model="rerank-v3.5", query=INPUT, documents=["synthetic"])
    return dict(
        model="command-r",
        **(
            {"message": INPUT}
            if version == 1
            else {"messages": [{"role": "user", "content": INPUT}]}
        ),
    )


def payload(version):
    if version == 1:
        return {
            "text": OUTPUT,
            "generation_id": "00000000-0000-0000-0000-000000000001",
            "finish_reason": "COMPLETE",
            # v1 returns an echoed copy of the input, not just output text.
            "chat_history": [
                {"role": "USER", "message": INPUT},
                {"role": "CHATBOT", "message": OUTPUT},
            ],
        }
    return {
        "id": "synthetic",
        "finish_reason": "COMPLETE",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": OUTPUT}],
        },
    }


def event_response(version, finish="COMPLETE", error=None):
    if version == 1:
        response = payload(version)
        response["finish_reason"] = finish
        chunks = [
            {"event_type": "text-generation", "text": OUTPUT},
            {
                "event_type": "stream-end",
                "finish_reason": finish,
                "response": response,
            },
        ]
    else:
        delta = {"finish_reason": finish}
        if error is not None:
            delta["error"] = error
        chunks = [
            {
                "type": "message-start",
                "id": "synthetic",
                "delta": {"message": {"role": "assistant"}},
            },
            {
                "type": "content-delta",
                "index": 0,
                "delta": {"message": {"content": {"text": OUTPUT}}},
            },
            {"type": "message-end", "delta": delta},
        ]
    return httpx.Response(
        200,
        text="".join(
            ("data: " if version == 2 else "")
            + json.dumps(event)
            + ("\n\n" if version == 2 else "\n")
            for event in chunks
        ),
        headers={"content-type": "text/event-stream"},
    )


async def call_chat(version, asynchronous, streaming, transport):
    cls = client_class(version, asynchronous)
    if asynchronous:
        async with httpx.AsyncClient(transport=transport) as http:
            client = cls(api_key="synthetic-only", httpx_client=http)
            if streaming:
                return [
                    event async for event in client.chat_stream(**request_args(version))
                ]
            return await client.chat(**request_args(version))
    with httpx.Client(transport=transport) as http:
        client = cls(api_key="synthetic-only", httpx_client=http)
        if streaming:
            return list(client.chat_stream(**request_args(version)))
        return client.chat(**request_args(version))


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("configuration", ["explicit", "environment"])
@pytest.mark.parametrize(
    "flag",
    [
        "hide_input_messages",
        "hide_input_text",
        "hide_output_messages",
        "hide_output_text",
    ],
)
async def test_selective_content_flags_do_not_leak_raw_copies(
    tracing, monkeypatch, version, asynchronous, streaming, hidden, configuration, flag
):
    client_class(version, asynchronous)
    # Exercise both explicit overrides, not just a true environment setting.
    monkeypatch.setenv(
        "FI_" + flag.upper(),
        str(not hidden if configuration == "explicit" else hidden).lower(),
    )
    _, exporter, _ = tracing(**({flag: hidden} if configuration == "explicit" else {}))
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return (
            event_response(version)
            if streaming
            else httpx.Response(200, json=payload(version))
        )

    response = await call_chat(
        version, asynchronous, streaming, httpx.MockTransport(respond)
    )
    assert len(requests) == 1
    assert INPUT in json.dumps(requests[0])
    final = response[-1] if streaming else response
    if streaming:
        # Caller-visible events must retain the same native text and history.
        assert OUTPUT in json.dumps([event.dict() for event in response])
        if version == 1:
            assert final.response.chat_history[0].message == INPUT
    elif version == 1:
        assert final.text == OUTPUT
        assert final.chat_history[0].message == INPUT
    else:
        assert final.message.content[0].text == OUTPUT
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    wire = encode_spans(spans).SerializeToString()
    sentinel = INPUT if "input" in flag else OUTPUT
    other = OUTPUT if "input" in flag else INPUT
    assert other.encode() in wire  # Preserve capture for the other direction.
    assert (sentinel.encode() not in wire) == hidden
    assert spans[0].attributes["cohere.finish_reason"] == "COMPLETE"
    assert spans[0].status.status_code == trace.StatusCode.OK


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("operation", ["chat", "embed", "rerank"])
@pytest.mark.parametrize("hidden", [False, True])
async def test_cancelled_unary_request_ends_span(tracing, version, operation, hidden):
    cls = client_class(version, True)
    provider, exporter, started = tracing(hide_error_details=hidden)
    entered = asyncio.Event()
    cancellations = []

    async def respond(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            cancellations.append(error)
            raise

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = cls(api_key="synthetic-only", httpx_client=http)

        async def call():
            try:
                return await getattr(client, operation)(
                    **request_args(version, operation)
                )
            except asyncio.CancelledError as error:
                # Check inside the task: Python 3.10 can drop the message at
                # the awaiting-task boundary, independently of instrumentation.
                cancellations.append(error)
                raise

        task = asyncio.create_task(call())
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel(ERROR)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(cancellations) == 2
        assert cancellations[0] is cancellations[1]
        assert cancellations[1].args == (ERROR,)
    assert len(started.spans) == 1
    provider.force_flush()
    ended = exporter.get_finished_spans()
    assert len(ended) == 1
    assert not started.spans[0].is_recording()
    assert ended[0].status.status_code == trace.StatusCode.ERROR
    assert "CancelledError" in ended[0].attributes["error.type"]
    assert (ERROR.encode() not in encode_spans(ended).SerializeToString()) == hidden
    assert not trace.get_current_span().is_recording()


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("close_at_terminal", [False, True])
@pytest.mark.parametrize(
    "version,finish,error,failed",
    [
        (1, "ERROR", None, True),
        (1, "ERROR_TOXIC", None, True),
        (2, "ERROR", None, True),
        (2, "TIMEOUT", None, True),
        (2, None, ERROR, True),
        (1, "COMPLETE", None, False),
        (1, "MAX_TOKENS", None, False),
        (2, "COMPLETE", None, False),
        (2, "MAX_TOKENS", None, False),
        (2, "TOOL_CALL", None, False),
    ],
)
async def test_native_stream_terminal_failure_status(
    tracing, version, asynchronous, close_at_terminal, finish, error, failed
):
    cls = client_class(version, asynchronous)
    _, exporter, _ = tracing()
    transport = httpx.MockTransport(lambda r: event_response(version, finish, error))
    response = []
    if asynchronous:
        async with httpx.AsyncClient(transport=transport) as http:
            client = cls(api_key="synthetic-only", httpx_client=http)
            stream = client.chat_stream(**request_args(version))
            async for event in stream:
                response.append(event)
                if close_at_terminal and (
                    getattr(event, "type", None) == "message-end"
                    or getattr(event, "event_type", None) == "stream-end"
                ):
                    break
            await stream.aclose()
            await stream.aclose()
    else:
        with httpx.Client(transport=transport) as http:
            client = cls(api_key="synthetic-only", httpx_client=http)
            stream = client.chat_stream(**request_args(version))
            for event in stream:
                response.append(event)
                if close_at_terminal and (
                    getattr(event, "type", None) == "message-end"
                    or getattr(event, "event_type", None) == "stream-end"
                ):
                    break
            stream.close()
            stream.close()
    assert len(response) == (2 if version == 1 else 3)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get("cohere.finish_reason") == finish
    expected = (
        trace.StatusCode.ERROR
        if failed
        else (trace.StatusCode.UNSET if close_at_terminal else trace.StatusCode.OK)
    )
    assert spans[0].status.status_code == expected
    if failed:
        assert spans[0].attributes["error.type"] == (finish or "_OTHER")
    else:
        assert "error.type" not in spans[0].attributes


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("configuration", ["explicit", "environment", "derived"])
@pytest.mark.parametrize("hidden", [False, True])
async def test_native_stream_error_details_are_private(
    tracing, monkeypatch, asynchronous, configuration, hidden
):
    client_class(2, asynchronous)
    if configuration == "derived":
        monkeypatch.delenv("FI_HIDE_ERROR_DETAILS", raising=False)
        options = {"hide_input_text": hidden}
    else:
        monkeypatch.setenv(
            "FI_HIDE_ERROR_DETAILS",
            str(not hidden if configuration == "explicit" else hidden).lower(),
        )
        options = {"hide_error_details": hidden} if configuration == "explicit" else {}
    _, exporter, _ = tracing(**options)
    response = await call_chat(
        2,
        asynchronous,
        True,
        httpx.MockTransport(lambda r: event_response(2, "ERROR", ERROR)),
    )
    assert response[-1].delta.error == ERROR
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    wire = encode_spans(spans).SerializeToString()
    assert (ERROR.encode() not in wire) == hidden
    assert OUTPUT.encode() in wire
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert spans[0].attributes["error.type"] == "ERROR"


def test_distribution_requires_compatible_core():
    """The installed distribution must exclude the reproduced broken core floor."""
    dependencies = [Requirement(value) for value in requires("traceAI-cohere")]
    core = next(dep for dep in dependencies if dep.name == "fi-instrumentation-otel")
    assert not core.specifier.contains("0.1.11")
    assert core.specifier.contains("1.2.2")
