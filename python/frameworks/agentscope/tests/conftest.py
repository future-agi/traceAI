import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

import pytest
from agentscope import message, model, tool
from fi_instrumentation import TraceConfig
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from traceai_agentscope import AgentScopeInstrumentor

V2 = version("agentscope").startswith("2.")
API_KEY = "local-test-provider-secret-12345"


def msg(text="question", role="user"):
    if V2:
        return message.Msg(name=role, role=role, content=[message.TextBlock(text=text)])
    return message.Msg(role, text, role)


def response(text="answer"):
    kwargs = {"content": [message.TextBlock(type="text", text=text)], "id": "fixed-id"}
    if V2:
        kwargs["is_last"] = True
    return model.ChatResponse(**kwargs)


def make_model(endpoint, stream=False):
    if V2:
        from agentscope.credential import OpenAICredential

        return model.OpenAIChatModel(
            credential=OpenAICredential(api_key=API_KEY, base_url=endpoint.url),
            model="local-test",
            stream=stream,
            max_retries=0,
            client_kwargs={"max_retries": 0},
        )
    return model.OpenAIChatModel(
        model_name="local-test",
        api_key=API_KEY,
        stream=stream,
        client_kwargs={"base_url": endpoint.url, "max_retries": 0},
    )


def model_input():
    return [msg()] if V2 else [{"role": "user", "content": "question"}]


def make_agent(endpoint, name="assistant", toolkit=None, stream=False):
    model_instance = make_model(endpoint, stream)
    if V2:
        from agentscope.agent import Agent, InjectionConfig

        return Agent(
            name=name,
            system_prompt="Be concise.",
            model=model_instance,
            toolkit=toolkit,
            injection_config=InjectionConfig(),
        )
    from agentscope.agent import ReActAgent
    from agentscope.formatter import OpenAIChatFormatter

    agent = ReActAgent(
        name,
        "Be concise.",
        model_instance,
        OpenAIChatFormatter(),
        toolkit=toolkit,
        max_iters=3,
    )
    agent.set_console_output_enabled(False)
    return agent


async def call_agent(agent, text="question"):
    return await agent.reply(msg(text)) if V2 else await agent(msg(text))


async def add(a: int, b: int):
    """Add two integers.

    Args:
        a: First integer.
        b: Second integer.
    """
    content = [message.TextBlock(type="text", text=str(a + b))]
    return tool.ToolChunk(content=content) if V2 else tool.ToolResponse(content=content)


def make_toolkit():
    if V2:
        from agentscope.permission import PermissionBehavior, PermissionDecision

        return tool.Toolkit(
            tools=[
                tool.FunctionTool(
                    add,
                    permission=PermissionDecision(
                        behavior=PermissionBehavior.ALLOW, message="test"
                    ),
                )
            ]
        )
    toolkit = tool.Toolkit()
    toolkit.register_tool_function(add)
    return toolkit


def call_tool(toolkit, name="add", values=None):
    values = {"a": 3, "b": 4} if values is None else values
    if V2:
        from agentscope.state import AgentState

        return toolkit.call_tool(
            message.ToolCallBlock(id="call-1", name=name, input=json.dumps(values)),
            AgentState(),
        )
    return toolkit.call_tool_function(
        {"type": "tool_use", "id": "call-1", "name": name, "input": values}
    )


@pytest.fixture(autouse=True)
def isolated_secret_registry(monkeypatch):
    # A previous test's observed credential must never make a missing observation
    # in the next test appear safe. This is core state, not integration metadata.
    from fi_instrumentation import _secrets

    monkeypatch.setattr(_secrets, "_registry", _secrets._SecretRegistry())


@pytest.fixture
def transport_tracing(tracing):
    """AgentScope plus real S5 transport observation, with wire-span dedup active."""
    from fi_instrumentation import FITracer
    from fi_instrumentation.auto import wire

    _, provider, _ = tracing
    statuses = wire.enable(
        FITracer(provider.get_tracer("agentscope-transport-test")),
        transports=["httpx", "httpx2"],
    )
    assert statuses["httpx"] == "instrumented"
    try:
        yield tracing
    finally:
        wire.disable()


@pytest.fixture
def tracing():
    instrumentor = AgentScopeInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor.instrument(tracer_provider=provider)
    assert instrumentor._enabled
    yield instrumentor, provider, exporter
    instrumentor.uninstrument()
    provider.shutdown()


@pytest.fixture
def retrace(tracing):
    instrumentor, provider, exporter = tracing

    def configure(**kwargs):
        instrumentor.uninstrument()
        exporter.clear()
        instrumentor.instrument(tracer_provider=provider, config=TraceConfig(**kwargs))
        return instrumentor, provider, exporter

    return configure


@pytest.fixture
def endpoint():
    class Endpoint:
        requests = None
        mode = "text"
        text = "hello local"
        status = 200
        echo_header = None

    state = Endpoint()
    state.requests = []
    state.headers = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.requests.append(body)
            state.headers.append(
                {key.lower(): value for key, value in self.headers.items()}
            )
            if state.status != 200:
                payload = json.dumps(
                    {"error": {"message": "local failure", "type": "test"}}
                ).encode()
                self.send_response(state.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            text = state.text
            if state.echo_header is not None:
                text = "echoed: " + self.headers[state.echo_header].split()[-1]
            message_body = {"role": "assistant", "content": text}
            finish_reason = "stop"
            if state.mode == "tool" and not any(
                m.get("role") == "tool" for m in body["messages"]
            ):
                message_body = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "add", "arguments": '{"a":3,"b":4}'},
                        }
                    ],
                }
                finish_reason = "tool_calls"
            base = {
                "id": "chatcmpl-local",
                "model": "local-test",
                "created": 1700000000,
            }
            usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
            if body.get("stream"):
                chunks = []
                for delta in (text[:6], text[6:]):
                    chunks.append(
                        {
                            **base,
                            "object": "chat.completion.chunk",
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": delta},
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                chunks += [
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                    {
                        **base,
                        "object": "chat.completion.chunk",
                        "choices": [],
                        "usage": usage,
                    },
                ]
                payload = (
                    "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
                    + "data: [DONE]\n\n"
                ).encode()
                content_type = "text/event-stream"
            else:
                payload = json.dumps(
                    {
                        **base,
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": message_body,
                                "finish_reason": finish_reason,
                            }
                        ],
                        "usage": usage,
                    }
                ).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
