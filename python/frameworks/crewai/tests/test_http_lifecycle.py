"""Real Agent/Crew contracts against a loopback provider; no provider keys/network.

The current native streaming/tool APIs are intentionally tested through Crew,
not a manually constructed ToolUsage or a mocked Crew.kickoff.
"""

import asyncio
from contextlib import nullcontext
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import threading

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from fi_instrumentation import TraceConfig
from traceai_crewai import CrewAIInstrumentor

pytestmark = pytest.mark.skipif(
    tuple(int(n) for n in version("crewai").split(".")[:2]) < (1, 15),
    reason="Native streaming and tool-call contract for CrewAI 1.15+",
)


@pytest.fixture
def http_crew():
    from crewai import Agent, Crew, LLM, Task

    requests = []
    release = threading.Event()
    state = {"block": False, "fail": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if state["fail"]:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            base = {
                "id": "synthetic-completion",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o-mini",
            }
            usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                first = {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "Final Answer: useful ",
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                last = {
                    **base,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "result"},
                            "finish_reason": "stop",
                        }
                    ],
                }
                try:
                    self.wfile.write(("data: " + json.dumps(first) + "\n\n").encode())
                    self.wfile.flush()
                    if state["block"]:
                        assert release.wait(15), (
                            "test did not release loopback response"
                        )
                    for chunk in (last, {**base, "choices": [], "usage": usage}):
                        self.wfile.write(
                            ("data: " + json.dumps(chunk) + "\n\n").encode()
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass  # Expected when native async execution is cancelled.
            else:
                if body.get("tools") and not any(
                    m["role"] == "tool" for m in body["messages"]
                ):
                    name = body["tools"][0]["function"]["name"]
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_synthetic",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": '{"query":"positive-query"}',
                                },
                            }
                        ],
                    }
                    finish = "tool_calls"
                else:
                    message = {
                        "role": "assistant",
                        "content": "Final Answer: useful result",
                    }
                    finish = "stop"
                payload = json.dumps(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "message": message,
                                "finish_reason": finish,
                            }
                        ],
                        "usage": usage,
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    provider = TracerProvider(shutdown_on_exit=False)
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    instrumentor = CrewAIInstrumentor()

    def build(stream=False, tools=None):
        llm = LLM(
            model="openai/gpt-4o-mini",
            api_key="synthetic-only",
            max_retries=0,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        )
        agent = Agent(
            role="Worker",
            goal="Use tools to answer",
            backstory="Synthetic",
            llm=llm,
            tools=tools or [],
            allow_delegation=False,
            max_retry_limit=0,
        )
        task = Task(
            description="Look up positive-query",
            expected_output="A sentence",
            agent=agent,
        )
        return Crew(agents=[agent], tasks=[task], stream=stream)

    yield build, instrumentor, provider, memory, requests, state, release
    release.set()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    provider.shutdown()
    server.shutdown()
    server.server_close()
    worker.join()


def crew_spans(memory):
    return [
        s
        for s in memory.get_finished_spans()
        if s.attributes.get("gen_ai.span.kind") == "CHAIN"
    ]


def assert_completed(memory, parent):
    spans = crew_spans(memory)
    assert len(spans) == 1
    span = spans[0]
    assert span.status.status_code is StatusCode.OK
    assert "useful result" in span.attributes["output.value"]
    assert span.attributes["gen_ai.usage.input_tokens"] > 0
    assert span.attributes["gen_ai.usage.output_tokens"] == 5
    assert span.attributes["gen_ai.usage.total_tokens"] > 5
    assert span.parent.span_id == parent.get_span_context().span_id
    tasks = [
        s
        for s in memory.get_finished_spans()
        if s.attributes.get("gen_ai.span.kind") == "AGENT"
    ]
    assert len(tasks) == 1
    assert tasks[0].parent.span_id == span.context.span_id
    assert span.start_time <= tasks[0].start_time <= tasks[0].end_time <= span.end_time


def test_streaming_kickoff_preserves_success(http_crew):
    build, inst, provider, memory, requests, _, _ = http_crew
    baseline = build(stream=True).kickoff()
    list(baseline)
    assert baseline.result.raw == "useful result"
    assert len(requests) == 1
    inst.instrument(tracer_provider=provider)
    stream = build(stream=True).kickoff()
    assert type(stream) is type(baseline)  # Preserve the native stream object/API.
    assert not memory.get_finished_spans() and len(requests) == 1
    with provider.get_tracer(__name__).start_as_current_span("consumer") as parent:
        list(stream)
        assert stream.result.raw == baseline.result.raw
        assert_completed(memory, parent)
    assert len(requests) == 2


@pytest.mark.parametrize("method", ["kickoff_async", "akickoff"])
@pytest.mark.asyncio
async def test_async_streaming_preserves_success(http_crew, method):
    build, inst, provider, memory, requests, _, _ = http_crew
    baseline = await getattr(build(stream=True), method)()
    _ = [chunk async for chunk in baseline]
    assert baseline.result.raw == "useful result" and len(requests) == 1
    inst.instrument(tracer_provider=provider)
    stream = await getattr(build(stream=True), method)()
    assert type(stream) is type(baseline)
    assert not memory.get_finished_spans() and len(requests) == 1
    with provider.get_tracer(__name__).start_as_current_span("consumer") as parent:
        _ = [chunk async for chunk in stream]
        assert stream.result.raw == baseline.result.raw
        assert_completed(memory, parent)
    assert len(requests) == 2


def test_sync_close_keeps_native_execution_lifecycle(http_crew):
    build, inst, provider, memory, requests, state, release = http_crew
    inst.instrument(tracer_provider=provider)
    # Close before consumption must not start a request or leave an open span.
    build(stream=True).kickoff().close()
    assert not requests and not memory.get_finished_spans()
    state["block"] = True
    with provider.get_tracer(__name__).start_as_current_span("consumer") as parent:
        stream = build(stream=True).kickoff()
        next(iter(stream))
        assert not crew_spans(memory)  # No early success on first chunk.
        release.set()
        stream.close()  # Native sync close joins its executing worker.
        assert_completed(memory, parent)


@pytest.mark.parametrize("method", ["kickoff_async", "akickoff"])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.asyncio
async def test_async_close_and_cancel_keep_native_lifecycle(http_crew, method, cancel):
    build, inst, provider, memory, requests, state, release = http_crew
    inst.instrument(tracer_provider=provider)
    stream = await getattr(build(stream=True), method)()
    await stream.aclose()
    assert not requests and not memory.get_finished_spans()
    state["block"] = True
    stream = await getattr(build(stream=True), method)()
    iterator = stream.__aiter__()
    await asyncio.wait_for(anext(iterator), 10)
    assert not crew_spans(memory)
    if cancel:
        pending = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)  # Enter the generator's native cancellation path.
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        await stream.aclose()
    release.set()
    # kickoff_async uses to_thread: cancellation cannot terminate its worker.
    # It must finish the span when execution really ends, not pretend to abort.
    for _ in range(200):
        if crew_spans(memory):
            break
        await asyncio.sleep(0.01)
    spans = crew_spans(memory)
    assert len(spans) == 1
    expected = StatusCode.ERROR if method == "akickoff" else StatusCode.OK
    assert spans[0].status.status_code is expected
    assert not spans[0].events


@pytest.mark.parametrize("method", ["kickoff", "kickoff_async", "akickoff"])
@pytest.mark.asyncio
async def test_stream_errors_end_exactly_one_span(http_crew, method):
    build, inst, provider, memory, _, state, _ = http_crew
    state["fail"] = True
    for instrumented in (False, True):
        if instrumented:
            inst.instrument(tracer_provider=provider)
        crew = build(stream=True)
        stream = (
            crew.kickoff() if method == "kickoff" else await getattr(crew, method)()
        )
        assert not crew_spans(memory)
        with pytest.raises(Exception) as error:
            if method == "kickoff":
                list(stream)
            else:
                _ = [chunk async for chunk in stream]
        if not instrumented:
            baseline_type = type(error.value)
        else:
            assert type(error.value) is baseline_type
    spans = crew_spans(memory)
    assert len(spans) == 1 and spans[0].status.status_code is StatusCode.ERROR
    assert "output.value" not in spans[0].attributes and not spans[0].events


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("method", ["kickoff", "akickoff"])
@pytest.mark.parametrize("tool_kind", ["sync", "async", "base"])
@pytest.mark.asyncio
async def test_real_native_tool_execution_is_traced(http_crew, fail, method, tool_kind):
    from crewai.tools import BaseTool, tool

    build, inst, provider, memory, requests, _, _ = http_crew
    calls = []

    def lookup(query: str) -> str:
        """Look up a synthetic value locally."""
        calls.append(query)
        if fail:
            raise ValueError("synthetic-private-exception-detail")
        return "useful tool result"

    async def async_lookup(query: str) -> str:
        """Look up a synthetic value asynchronously."""
        return lookup(query)

    class Lookup(BaseTool):
        name: str = "local_lookup"
        description: str = "Look up a synthetic value locally."

        def _run(self, query: str) -> str:
            return lookup(query)

        async def _arun(self, query: str) -> str:
            return lookup(query)

    local_lookup = (
        Lookup()
        if tool_kind == "base"
        else tool("local_lookup")(async_lookup if tool_kind == "async" else lookup)
    )

    async def run():
        crew = build(tools=[local_lookup])
        return (
            await asyncio.to_thread(crew.kickoff)
            if method == "kickoff"
            else await crew.akickoff()
        )

    baseline = await run()
    assert "useful result" in baseline.raw and calls == ["positive-query"]
    inst.instrument(tracer_provider=provider)
    result = await run()
    assert result.raw == baseline.raw and calls == ["positive-query", "positive-query"]
    assert len(requests) == 4  # Both actual two-turn provider interactions occurred.
    spans = memory.get_finished_spans()
    tools = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "TOOL"]
    assert len(tools) == 1
    observed = tools[0]
    assert observed.attributes["gen_ai.tool.name"] == "local_lookup"
    assert "positive-query" in observed.attributes["input.value"]
    task = next(s for s in spans if s.attributes.get("gen_ai.span.kind") == "AGENT")
    assert observed.parent.span_id == task.context.span_id
    assert task.parent.span_id == crew_spans(memory)[0].context.span_id
    if fail:
        assert observed.status.status_code is StatusCode.ERROR
        assert "output.value" not in observed.attributes
        assert not observed.events and observed.status.description is None
    else:
        assert observed.status.status_code is StatusCode.OK
        assert "useful tool result" in observed.attributes["output.value"]


@pytest.mark.parametrize("async_tool", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.asyncio
async def test_native_tool_privacy_suppression_and_uninstrument(
    http_crew, async_tool, hidden, structured
):
    from crewai.tools import tool as make_tool
    from crewai.tools.base_tool import BaseTool, Tool
    from crewai.tools.structured_tool import CrewStructuredTool
    from opentelemetry.instrumentation.utils import suppress_instrumentation

    _, inst, provider, memory, _, _, _ = http_crew
    secret = "synthetic-private-tool-key"
    seen = []

    def lookup(query: str, api_key: str) -> dict:
        """Return synthetic application data."""
        seen.append((query, api_key))
        return {"answer": query, "api_key": api_key}

    async def alookup(query: str, api_key: str) -> dict:
        """Return synthetic application data asynchronously."""
        return lookup(query, api_key)

    function = alookup if async_tool else lookup
    tool = (
        CrewStructuredTool.from_function(function)
        if structured
        else make_tool(function)
    )
    methods = [
        (cls, name)
        for cls, names in (
            (CrewStructuredTool, ("invoke", "ainvoke")),
            (BaseTool, ("run", "arun")),
            (Tool, ("run", "arun")),
        )
        for name in names
    ]
    originals = [getattr(cls, name) for cls, name in methods]
    inst.instrument(
        tracer_provider=provider,
        config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
    )
    for suppressed in (False, True):
        with suppress_instrumentation() if suppressed else nullcontext():
            arguments = {"query": "useful-query", "api_key": secret}
            if structured:
                result = (
                    await tool.ainvoke(arguments)
                    if async_tool
                    else tool.invoke(arguments)
                )
            else:
                result = (
                    await tool.arun(**arguments)
                    if async_tool
                    else tool.run(**arguments)
                )
            assert result == {"answer": "useful-query", "api_key": secret}
            assert arguments["api_key"] == secret
    spans = memory.get_finished_spans()
    assert len(spans) == 1
    serialized = json.dumps(dict(spans[0].attributes))
    assert secret not in serialized
    assert ("useful-query" in serialized) is not hidden
    inst.uninstrument()
    assert [getattr(cls, name) for cls, name in methods] == originals
    assert seen == [("useful-query", secret), ("useful-query", secret)]
