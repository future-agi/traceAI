"""Maintained OpenAI adapter composition over real loopback HTTP on floor/latest.

No provider-method mocks: the floor uses LangChain's OpenAI client, current
CrewAI uses its native OpenAI client. Only LLM spans are billable call records;
Crew's legacy usage attributes remain a (potentially estimated) roll-up.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

import pytest
from fi_instrumentation import TraceConfig
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from traceai_crewai import CrewAIInstrumentor

FLOOR = version("crewai") == "0.41.1"


@pytest.fixture
def provider_composition():
    from crewai import Agent, Crew, Task

    # Optional integration dependency, mandatory in the compatibility matrix.
    # Framework-only tests still run in the general package job without it.
    from importlib.util import find_spec

    if find_spec("traceai_openai") is not None:
        from traceai_openai import OpenAIInstrumentor

        llm_instrumentor = OpenAIInstrumentor()
    else:
        llm_instrumentor = None

    calls = []
    state = {
        "fail": False,
        "block": False,
        "entered": threading.Event(),
        "release": threading.Event(),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            if state["block"]:
                state["entered"].set()
                assert state["release"].wait(10), "Test did not release provider request"
            if state["fail"]:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            model = body["model"]
            prompt, completion = (11, 5) if model == "gpt-4o-mini" else (17, 7)
            base = {
                "id": "synthetic-composition",
                "object": "chat.completion",
                "created": 1,
                "model": model,
            }
            message = {"role": "assistant", "content": "Final Answer: positive-output"}
            finish = "stop"
            if body.get("tools") and not any(
                m["role"] == "tool" for m in body["messages"]
            ):
                message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "synthetic-tool-call",
                            "type": "function",
                            "function": {
                                "name": body["tools"][0]["function"]["name"],
                                "arguments": '{"query":"positive-input"}',
                            },
                        }
                    ],
                }
                finish = "tool_calls"
            usage = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            }
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for chunk in (
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"index": 0, "delta": message, "finish_reason": None}
                        ],
                    },
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    },
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [],
                        "usage": usage,
                    },
                ):
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            payload = json.dumps(
                {
                    **base,
                    "choices": [
                        {"index": 0, "message": message, "finish_reason": finish}
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    provider = TracerProvider(shutdown_on_exit=False)
    memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    crew_instrumentor = CrewAIInstrumentor()

    def build(models=("gpt-4o-mini",), stream=False, parallel=False):
        agents, tasks = [], []
        for model in models:
            settings = dict(
                model=model,
                api_key="synthetic-provider-key",
                base_url=f"http://127.0.0.1:{server.server_port}/v1",
                max_retries=0,
            )
            if FLOOR:
                from langchain_openai import ChatOpenAI

                llm = ChatOpenAI(**settings, streaming=stream, stream_usage=True)
            else:
                from crewai import LLM

                llm = LLM(**settings, stream=stream)
            agent = Agent(
                role="Worker",
                goal="Answer",
                backstory="Synthetic",
                llm=llm,
                allow_delegation=False,
                max_retry_limit=0,
            )
            agents.append(agent)
            tasks.append(
                Task(
                    description="Return positive-input",
                    expected_output="sentence",
                    agent=agent,
                    async_execution=parallel and not tasks,
                )
            )
        return Crew(agents=agents, tasks=tasks)

    yield build, crew_instrumentor, llm_instrumentor, provider, memory, calls, state
    for inst in (crew_instrumentor, llm_instrumentor):
        if inst is not None and inst.is_instrumented_by_opentelemetry:
            inst.uninstrument()
    provider.shutdown()
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("parallel", [False, True])
@pytest.mark.parametrize("api", ["kickoff", "kickoff_async"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_composed_calls_have_model_usage_parentage_and_privacy(
    provider_composition, api, stream, hidden, parallel
):
    build, crew_inst, llm_inst, provider, memory, calls, _ = provider_composition
    if llm_inst is None:
        pytest.skip("Install traceAI-openai to test provider composition")
    if stream and version("crewai") == "0.152.0":
        pytest.skip(
            "CrewAI 0.152.0's LiteLLM transport does not expose streamed SDK usage; "
            "use non-streaming or the native OpenAI route in CrewAI 1.15+"
        )
    config = TraceConfig(
        hide_inputs=hidden, hide_outputs=hidden, hide_error_details=True
    )
    crew_inst.instrument(tracer_provider=provider, config=config)
    llm_inst.instrument(tracer_provider=provider, config=config)
    with provider.get_tracer("caller").start_as_current_span("caller") as parent:
        crew = build(("gpt-4o-mini", "gpt-4o"), stream=stream, parallel=parallel)
        result = getattr(crew, api)()
        if api.endswith("async"):
            result = asyncio.run(result)
        assert result.raw == "positive-output"
        spans = memory.get_finished_spans()
        llms = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
        assert len(llms) == len(calls) == 2
        assert {s.attributes["gen_ai.request.model"] for s in llms} == {
            "gpt-4o-mini",
            "gpt-4o",
        }
        tasks = {s.context.span_id: s for s in spans if s.name.startswith("Task.")}
        crews = [s for s in spans if s.name.startswith("Crew.")]
        assert len(crews) == 1
        assert crews[0].parent.span_id == parent.get_span_context().span_id
        for span in llms:
            attrs = span.attributes
            assert attrs["gen_ai.provider.name"] == "openai"
            assert span.parent.span_id in tasks
            task = tasks[span.parent.span_id]
            assert task.parent.span_id == crews[0].context.span_id
            assert task.start_time <= span.start_time <= span.end_time <= task.end_time
            assert span.status.status_code is StatusCode.OK
            prompt, completion = (
                (11, 5) if attrs["gen_ai.request.model"] == "gpt-4o-mini" else (17, 7)
            )
            assert attrs["gen_ai.usage.input_tokens"] == prompt
            assert attrs["gen_ai.usage.output_tokens"] == completion
            assert attrs["gen_ai.usage.total_tokens"] == prompt + completion
            serialized = json.dumps(dict(attrs))
            assert "synthetic-provider-key" not in serialized
            assert ("positive-input" in serialized) is not hidden
            assert ("positive-output" in serialized) is not hidden
        # Do not add Crew's roll-up to the authoritative per-request usage.
        assert sum(s.attributes["gen_ai.usage.total_tokens"] for s in llms) == 40
        if hidden:
            assert "positive-input" not in json.dumps(
                [dict(s.attributes) for s in spans]
            )
            assert "positive-output" not in json.dumps(
                [dict(s.attributes) for s in spans]
            )


def test_composition_suppression_restoration_and_failure(provider_composition):
    build, crew_inst, llm_inst, provider, memory, calls, state = provider_composition
    if llm_inst is None:
        pytest.skip("Install traceAI-openai to test provider composition")
    config = TraceConfig(hide_inputs=True, hide_outputs=True, hide_error_details=True)
    for inst in (crew_inst, llm_inst):
        inst.instrument(tracer_provider=provider, config=config)
    with suppress_instrumentation():
        assert build().kickoff().raw == "positive-output"
    assert not memory.get_finished_spans()
    state["fail"] = True
    with pytest.raises(Exception):
        build().kickoff()
    spans = memory.get_finished_spans()
    llms = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert llms
    assert all(s.status.status_code is StatusCode.ERROR for s in spans)
    for span in llms:
        assert span.attributes["gen_ai.provider.name"] == "openai"
        assert span.attributes["gen_ai.request.model"] == "gpt-4o-mini"
        assert span.end_time is not None
    assert "synthetic-provider-key" not in json.dumps(
        [dict(s.attributes) for s in spans]
    )
    memory.clear()
    state["fail"] = False
    for inst in (crew_inst, llm_inst):
        inst.uninstrument()
    assert build().kickoff().raw == "positive-output"
    assert not memory.get_finished_spans()
