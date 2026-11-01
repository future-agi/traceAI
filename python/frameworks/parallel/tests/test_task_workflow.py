"""Exercise the documented application span with real sync/async Parallel SDKs."""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fi_instrumentation import FITracer, using_session
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from parallel import AsyncParallel, AuthenticationError, Parallel
from traceai_parallel import ParallelInstrumentor

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "task_workflow.py"
spec = importlib.util.spec_from_file_location("parallel_task_example", EXAMPLE)
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from harness import Receiver, _flatten_attributes  # noqa: E402

KEY = "synthetic-parallel-key"
QUESTION = "synthetic-private-task-question"
ANSWER = "synthetic-private-task-answer"


def run_body(status="completed"):
    return {"run_id": "run_1", "interaction_id": "interaction_1", "processor": "base",
            "status": status, "is_active": status == "queued"}


def result_body(status="completed"):
    return {"run": run_body(status), "output": {"type": "text", "content": ANSWER, "basis": []}}


@pytest.fixture
def traced():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = ParallelInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield FITracer(provider.get_tracer("task-example")), exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "create-error", "result-error"])
def test_workflow_waits_for_result_and_preserves_errors(traced, use_async, outcome):
    tracer, exporter = traced
    paths = []

    def handler(request):
        paths.append(request.url.path)
        # Submission must not end the workflow span. Waiting stays inside it.
        assert exporter.get_finished_spans() == ()
        assert request.headers["x-api-key"] == KEY
        if (outcome == "create-error" and len(paths) == 1) or (
            outcome == "result-error" and len(paths) == 2
        ):
            return httpx.Response(401, json={"error": {"message": KEY + QUESTION + ANSWER}})
        if len(paths) == 1:
            assert json.loads(request.content)["input"] == QUESTION
            return httpx.Response(200, json=run_body("queued"))
        assert request.url.params["timeout"] == "600"
        return httpx.Response(200, json=result_body(outcome))

    async def run_async():
        async with AsyncParallel(api_key=KEY, max_retries=0, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False,
        )) as client:
            return await example.research_async(client, tracer, QUESTION)

    def run_sync():
        with Parallel(api_key=KEY, max_retries=0, http_client=httpx.Client(
            transport=httpx.MockTransport(handler), trust_env=False,
        )) as client:
            return example.research(client, tracer, QUESTION)

    with using_session("task-session"), tracer.start_as_current_span("application") as parent:
        if outcome.endswith("error"):
            with pytest.raises(AuthenticationError) as caught:
                asyncio.run(run_async()) if use_async else run_sync()
            assert KEY in str(caught.value)  # Original SDK exception is unchanged.
        else:
            result = asyncio.run(run_async()) if use_async else run_sync()
            assert result.output.content == ANSWER

    spans = exporter.get_finished_spans()
    assert len(spans) == 2  # No submission or polling monkeypatch spans.
    span = spans[0]
    assert span.name == "parallel.task"
    assert span.parent.span_id == parent.get_span_context().span_id
    assert span.attributes["session.id"] == "task-session"
    assert span.attributes["gen_ai.span.kind"] == "CHAIN"
    assert span.attributes["gen_ai.provider.name"] == "parallel"
    assert span.status.status_code is (StatusCode.OK if outcome == "completed" else StatusCode.ERROR)
    if outcome != "create-error":
        assert span.attributes["parallel.run_id"] == "run_1"
    if not outcome.endswith("error"):
        assert span.attributes["parallel.task.status"] == outcome
    assert paths == (["/v1/tasks/runs"] if outcome == "create-error" else
                     ["/v1/tasks/runs", "/v1/tasks/runs/run_1/result"])
    assert span.events == ()
    wire = span.to_json()
    for secret in (KEY, QUESTION, ANSWER):
        assert secret not in wire


def test_cancelling_the_wait_ends_one_span_without_cancelling_remote_task(traced):
    tracer, exporter = traced
    paths = []

    async def run():
        waiting = asyncio.Event()

        async def handler(request):
            paths.append(request.url.path)
            if len(paths) == 1:
                return httpx.Response(200, json=run_body("queued"))
            waiting.set()
            await asyncio.Event().wait()

        async with AsyncParallel(api_key=KEY, max_retries=0, http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False,
        )) as client:
            task = asyncio.create_task(example.research_async(client, tracer, QUESTION))
            await asyncio.wait_for(waiting.wait(), timeout=3)
            assert exporter.get_finished_spans() == ()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["parallel.cancelled"] is True
    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].status.description == "Task wait cancelled"
    assert spans[0].events == ()
    assert paths == ["/v1/tasks/runs", "/v1/tasks/runs/run_1/result"]


@pytest.mark.parametrize("mode", ["sync", "async", "readme"])
def test_example_cli_with_loopback_sdk_and_real_exporter(mode):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class API(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.respond(run_body("queued"))

        def do_GET(self):
            self.respond(result_body())

        def respond(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        with Receiver() as receiver:
            env = {k: v for k, v in os.environ.items() if not k.startswith(("FI_", "PARALLEL_", "OTEL_"))}
            env.update(FI_BASE_URL=receiver.origin, FI_API_KEY="synthetic-fi-key",
                       FI_SECRET_KEY="synthetic-fi-secret", PARALLEL_API_KEY=KEY,
                       PARALLEL_BASE_URL=f"http://127.0.0.1:{server.server_port}")
            if mode == "readme":
                section = (EXAMPLE.parents[1] / "README.md").read_text().split("## Trace a Task workflow")[1]
                snippet = section.split("```python\n")[1].split("```")[0]
                command = [sys.executable, "-I", "-c", snippet]
            else:
                command = [sys.executable, "-I", str(EXAMPLE)] + (["--async"] if mode == "async" else [])
            result = subprocess.run(command, env=env, capture_output=True, timeout=30)
            assert result.returncode == 0, result.stderr.decode()
            spans = receiver.spans()
            assert len(spans) == 1
            assert spans[0]["name"] == "parallel.task"
            attrs = _flatten_attributes(spans[0]["attributes"])
            assert attrs["parallel.run_id"] == "run_1"
            assert attrs["parallel.task.status"] == "completed"
            assert attrs["gen_ai.span.kind"] == "CHAIN"
            assert all(r["path"] == "/tracer/v1/traces" for r in receiver.requests())
            assert KEY not in json.dumps(spans)
            assert ANSWER not in json.dumps(spans)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
