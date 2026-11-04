"""Independent installed-package qualification: real Agno/OpenAI over loopback HTTP."""

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.workflow import Workflow
from agno.workflow.step import Step
from agno.workflow.types import StepInput, StepOutput
from openai import OpenAI, AsyncOpenAI
from openinference.instrumentation import TraceConfig
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from traceai_agno import configure_agno_tracing

INPUT = "SYNTHETIC-PRIVATE-INPUT-1104"
OUTPUT = "SYNTHETIC-PRIVATE-OUTPUT-1104"
ERROR = "SYNTHETIC-PRIVATE-ERROR-1104"


@pytest.fixture
def server():
    state = {
        "requests": [],
        "started": threading.Event(),
        "release": threading.Event(),
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(body)
            if body["model"] == "delay":
                state["started"].set()
                state["release"].wait(15)
            if body["model"] == "error":
                payload = json.dumps(
                    {
                        "error": {
                            "message": ERROR,
                            "type": "invalid_request_error",
                            "code": "invalid_request",
                        }
                    }
                ).encode()
                code, mime = 400, "application/json"
            elif body.get("stream"):
                common = {
                    "id": "chatcmpl-q",
                    "created": 0,
                    "model": body["model"],
                    "object": "chat.completion.chunk",
                }
                chunks = [
                    {
                        **common,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": OUTPUT},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        **common,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    },
                    {
                        **common,
                        "choices": [],
                        "usage": state["usage"],
                    },
                ]
                payload = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                    + "data: [DONE]\n\n"
                ).encode()
                code, mime = 200, "text/event-stream"
            else:
                payload = json.dumps(
                    {
                        "id": "chatcmpl-q",
                        "created": 0,
                        "model": body["model"],
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": OUTPUT},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": state["usage"],
                    }
                ).encode()
                code, mime = 200, "application/json"
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    th = threading.Thread(target=httpd.serve_forever, daemon=True)
    th.start()
    state["url"] = f"http://127.0.0.1:{httpd.server_port}/v1"
    yield state
    state["release"].set()
    httpd.shutdown()
    th.join()
    httpd.server_close()


@contextmanager
def capture(config=None, endpoint=None):
    exporter = InMemorySpanExporter()
    if endpoint is None:
        provider = TracerProvider()
        instrumentation = configure_agno_tracing(
            tracer_provider=provider, config=config
        )
    else:
        instrumentation = configure_agno_tracing(otlp_endpoint=endpoint, config=config)
        provider = instrumentation.tracer_provider
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    try:
        yield exporter
    finally:
        provider.force_flush()
        instrumentation.uninstrument()
        if endpoint is None:
            provider.shutdown()


@pytest.fixture
async def model(server):
    sync = OpenAI(api_key="synthetic-key-only", base_url=server["url"], max_retries=0)
    async_client = AsyncOpenAI(
        api_key="synthetic-key-only", base_url=server["url"], max_retries=0
    )
    yield lambda name="gpt-4o-mini": OpenAIChat(
        id=name, client=sync, async_client=async_client
    )
    sync.close()
    await async_client.close()


def span_data(exporter):
    return [
        {
            "name": s.name,
            "kind": s.attributes.get("openinference.span.kind"),
            "status": s.status.status_code.name,
            "description": s.status.description,
            "attributes": dict(s.attributes),
            "events": [dict(e.attributes) for e in s.events],
        }
        for s in exporter.get_finished_spans()
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("hide", [False, True])
async def test_loopback_smoke(model, server, asynchronous, streaming, hide):
    # None tests the actual documented default (capture content).
    config = TraceConfig(hide_inputs=True, hide_outputs=True) if hide else None
    with capture(config) as exporter:
        agent = Agent(model=model(), telemetry=False)
        before = trace.get_current_span().get_span_context()
        if asynchronous:
            if streaming:
                chunks = []
                async for c in agent.arun(INPUT, stream=True):
                    assert trace.get_current_span().get_span_context() == before
                    chunks.append(c.content or "")
                output = "".join(chunks)
            else:
                output = (await agent.arun(INPUT)).content
        elif streaming:
            chunks = []
            for c in agent.run(INPUT, stream=True):
                assert trace.get_current_span().get_span_context() == before
                chunks.append(c.content or "")
            output = "".join(chunks)
        else:
            output = agent.run(INPUT).content
        assert output == OUTPUT
        spans = exporter.get_finished_spans()
        assert len(spans) == 2, span_data(exporter)
        root = next(s for s in spans if s.parent is None)
        llm = next(
            s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"
        )
        assert root.attributes["openinference.span.kind"] == "AGENT"
        assert llm.parent.span_id == root.context.span_id
        assert llm.context.trace_id == root.context.trace_id
        assert llm.attributes["llm.model_name"] == "gpt-4o-mini"
        assert llm.attributes["llm.provider"] == "OpenAI"
        assert llm.attributes["llm.token_count.prompt"] == 12
        assert llm.attributes["llm.token_count.completion"] == 3
        assert all(s.status.status_code == StatusCode.OK for s in spans), span_data(
            exporter
        )
        assert all(s.end_time is not None for s in spans)
        data = json.dumps(span_data(exporter))
        assert (INPUT in data) == (not hide)
        assert (OUTPUT in data) == (not hide)
        assert "synthetic-key-only" not in data
        assert len(server["requests"]) == 1
        assert INPUT in json.dumps(
            server["requests"]
        )  # privacy does not mutate request


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_loopback_errors(model, asynchronous):
    with capture() as exporter:
        agent = Agent(model=model("error"), telemetry=False)
        try:
            if asynchronous:
                await agent.arun(INPUT)
            else:
                agent.run(INPUT)
        except Exception as error:
            assert ERROR in str(error)
        spans = exporter.get_finished_spans()
        assert len(spans) == 2, span_data(exporter)
        assert all(s.status.status_code == StatusCode.ERROR for s in spans), span_data(
            exporter
        )


@pytest.mark.asyncio
async def test_cancelled_nonstream_model_ends_without_success(model, server):
    with capture() as exporter:
        task = asyncio.create_task(
            Agent(model=model("delay"), telemetry=False).arun(INPUT)
        )
        try:
            assert await asyncio.to_thread(server["started"].wait, 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            server["release"].set()
        spans = exporter.get_finished_spans()
        assert len(spans) == 2, span_data(exporter)
        llm = next(
            s for s in spans if s.attributes.get("openinference.span.kind") == "LLM"
        )
        # Explicit user cancellation need not be an error, but must end and never be OK.
        assert llm.end_time is not None
        assert llm.status.status_code != StatusCode.OK, span_data(exporter)


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_created", [False, True])
async def test_fi_content_privacy_env_is_honored(
    model, monkeypatch, receiver, endpoint_created
):
    monkeypatch.setenv("FI_HIDE_INPUTS", "true")
    monkeypatch.setenv("FI_HIDE_OUTPUTS", "true")
    with capture(endpoint=receiver[0] if endpoint_created else None) as exporter:
        assert (
            await Agent(model=model(), telemetry=False).arun(INPUT)
        ).content == OUTPUT
        spans = exporter.get_finished_spans()
        assert len(spans) == 2, span_data(exporter)
        assert all(span.status.status_code == StatusCode.OK for span in spans)
        data = json.dumps(span_data(exporter))
        assert INPUT not in data and OUTPUT not in data, span_data(exporter)
    if endpoint_created:
        assert receiver[1]
        assert all(
            INPUT.encode() not in body and OUTPUT.encode() not in body
            for _, _, body in receiver[1]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint_created", [False, True])
@pytest.mark.parametrize("explicit_error_flag", [True, False])
async def test_fi_error_privacy_env_is_honored(
    model, monkeypatch, receiver, endpoint_created, explicit_error_flag
):
    if explicit_error_flag:
        monkeypatch.setenv("FI_HIDE_ERROR_DETAILS", "true")
    else:
        monkeypatch.delenv("FI_HIDE_ERROR_DETAILS", raising=False)
    with capture(
        TraceConfig(hide_inputs=True, hide_outputs=True),
        endpoint=receiver[0] if endpoint_created else None,
    ) as exporter:
        try:
            await Agent(model=model("error"), telemetry=False).arun(INPUT)
        except Exception:
            pass
        spans = exporter.get_finished_spans()
        assert len(spans) == 2, span_data(exporter)
        assert ERROR not in json.dumps(span_data(exporter)), span_data(exporter)
        assert all(s.status.status_code == StatusCode.ERROR for s in spans)
        assert any(
            event.attributes.get("exception.type")
            for span in spans
            for event in span.events
        )
    if endpoint_created:
        assert receiver[1]
        assert all(ERROR.encode() not in body for _, _, body in receiver[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_completed_workflow_stream_close_is_success(asynchronous):
    def step(step_input: StepInput) -> StepOutput:
        return StepOutput(content="done")

    with capture() as exporter:
        workflow = Workflow(
            name="terminal-workflow",
            steps=[Step(name="step", executor=step)],
            telemetry=False,
        )
        found = False
        if asynchronous:
            iterator = workflow.arun("hello", stream=True)
            try:
                async for event in iterator:
                    if str(getattr(event, "event", "")) == "WorkflowCompleted":
                        found = True
                        break
            finally:
                await iterator.aclose()
        else:
            iterator = workflow.run("hello", stream=True)
            try:
                for event in iterator:
                    if str(getattr(event, "event", "")) == "WorkflowCompleted":
                        found = True
                        break
            finally:
                iterator.close()
        assert found, "test must reach genuine terminal completion"
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.parent is None)
        assert root.status.status_code == StatusCode.OK, span_data(exporter)
        assert root.attributes["output.value"] == "done"
