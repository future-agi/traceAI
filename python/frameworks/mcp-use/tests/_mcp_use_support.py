"""Fakes and helpers shared by the traceAI-mcp-use tests.

The LLM is ``ChatFake``, a LangChain chat model that replays a script. The
MCP server is ``_mcp_server.py``, started by mcp-use over stdio. Nothing
here opens a network connection; every key is a placeholder.

Import this module before ``mcp_use``: it turns off mcp-use's own usage
telemetry, which otherwise posts to PostHog and Scarf.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# mcp-use 1.7.1 sends anonymized usage events unless this is "false"
# (mcp_use/telemetry/telemetry.py:139). Telemetry is a singleton built on
# first use (telemetry.py:122), so this runs before any MCPAgent/MCPClient.
os.environ["MCP_USE_ANONYMIZED_TELEMETRY"] = "false"
# Importing mcp_use sets up Langfuse and Laminar when their keys are present
# (mcp_use/agents/observability/__init__.py:6). The tests set no keys and
# turn both off as well.
os.environ["MCP_USE_LANGFUSE"] = "false"
os.environ["MCP_USE_LAMINAR"] = "false"

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
REPO_PYTHON = PACKAGE.parents[1]
SERVER = HERE / "_mcp_server.py"

MODEL = "fake-model-1"
PROMPT = "PROMPT-MARKER please add two and three"
ANSWER = "ANSWER-MARKER the sum is five"
ARG = "ARG-MARKER"
# Placeholders only. LLM_KEY has the sk- shape; ENV_SECRET is only known
# to the callback through the FAKE_PROVIDER_API_KEY environment variable.
LLM_KEY = "sk-placeholder-llm-key-0000000000000000"
ENV_SECRET_NAME = "FAKE_PROVIDER_API_KEY"
ENV_SECRET = "placeholder-env-secret-value-1234"
EXPLICIT_SECRET = "placeholder-mcp-header-token-42"
CONTENT_MARKERS = (
    "PROMPT-MARKER",
    "ANSWER-MARKER",
    "ARG-MARKER",
    "SUM-RESULT",
    "ECHO-RESULT",
    "tool failed because",
)

AGENT = "mcp_use.agent"
LLM_SPAN = "chat " + MODEL


class ChatFake(BaseChatModel):
    """A chat model that replays ``script``.

    Each step is an ``AIMessage`` to return, an exception to raise, or a
    callable taking the input messages and returning an ``AIMessage``.
    Returned messages carry usage and response metadata like a provider's.
    ``streaming=True`` streams word by word through ``on_llm_new_token``.
    """

    script: List[Any]
    model_name: str = MODEL
    temperature: float = 0.2
    max_tokens: int = 64
    # Held like a provider key. LangChain puts repr(model), and so this
    # value, into the serialized payload of on_chat_model_start.
    api_key: str = LLM_KEY
    streaming: bool = False
    index: int = 0
    delay: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "ChatFake":
        return self

    def _next(self, messages: Sequence[BaseMessage]) -> AIMessage:
        if self.index >= len(self.script):
            step: Any = AIMessage(content="script exhausted")
        else:
            step = self.script[self.index]
        self.index += 1
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = step(messages)
        return step.model_copy(
            update={
                "usage_metadata": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
                "response_metadata": {
                    "model_name": MODEL,
                    "finish_reason": "tool_calls" if step.tool_calls else "stop",
                },
            }
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._generate(messages, stop=stop, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        message = self._next(messages)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": _json(call["args"]),
                            "id": call["id"],
                            "index": index,
                        }
                        for index, call in enumerate(message.tool_calls)
                    ],
                    usage_metadata=message.usage_metadata,
                    response_metadata=message.response_metadata,
                )
            )
            return
        words = str(message.content).split(" ")
        for position, word in enumerate(words):
            text = word if position == len(words) - 1 else word + " "
            last = position == len(words) - 1
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content=text,
                    usage_metadata=message.usage_metadata if last else None,
                    response_metadata=message.response_metadata if last else {},
                )
            )
            if run_manager:
                await run_manager.on_llm_new_token(text, chunk=chunk)
            yield chunk


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def tool_call(name: str, args: Dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def answer(text: str = ANSWER) -> AIMessage:
    return AIMessage(content=text)


def new_provider() -> Tuple[InMemorySpanExporter, TracerProvider]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


class _LoopbackServer:
    """``_mcp_server.py`` over streamable HTTP on 127.0.0.1, in a thread.

    Started once per test process: spawning the stdio server costs several
    seconds of imports per test. The contract and example tests still use
    stdio.
    """

    _lock = threading.Lock()
    _url: Optional[str] = None

    @classmethod
    def url(cls) -> str:
        with cls._lock:
            if cls._url is None:
                cls._url = cls._start()
            return cls._url

    @staticmethod
    def _start() -> str:
        import socket

        import uvicorn

        sys.path.insert(0, str(HERE))
        from _mcp_server import mcp as fake_server

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(fake_server.streamable_http_app(), log_level="warning")
        )
        thread = threading.Thread(
            target=lambda: asyncio.run(server.serve(sockets=[sock])), daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 120
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the loopback MCP server did not start")
            time.sleep(0.02)
        return "http://127.0.0.1:{0}/mcp".format(port)


def mcp_client(transport: str = "http") -> Any:
    """An MCPClient for the fake server: loopback HTTP, or a stdio subprocess."""
    from mcp_use import MCPClient

    if transport == "stdio":
        server: Dict[str, Any] = {"command": sys.executable, "args": [str(SERVER)]}
    else:
        server = {"url": _LoopbackServer.url()}
    return MCPClient.from_dict({"mcpServers": {"fake": server}})


def add_script() -> List[Any]:
    """The J1 journey: one tool call to add, then the final answer."""
    return [tool_call("add", {"a": 2, "b": 3}), answer()]


async def drive(
    agent: Any, method: str, query: str = PROMPT, **kwargs: Any
) -> Any:
    """Run the agent through one of its three public entry points."""
    if method == "run":
        return await agent.run(query, **kwargs)
    if method == "stream":
        result = None
        async for item in agent.stream(query, **kwargs):
            result = item
        return result
    if method == "stream_events":
        events = []
        async for event in agent.stream_events(query, **kwargs):
            events.append(event)
        return events
    raise ValueError(method)


def run_agent(
    script: List[Any],
    callbacks: Optional[List[Any]],
    method: str = "run",
    query: str = PROMPT,
    streaming: bool = False,
    agent_kwargs: Optional[Dict[str, Any]] = None,
    around: Optional[Callable[[], Any]] = None,
    transport: str = "http",
) -> Any:
    """Build an MCPAgent on the fake LLM and fake server and run it once.

    ``around`` returns a context manager entered around the run (for
    using_session, a parent span, suppress_tracing...).
    """
    from mcp_use import MCPAgent

    async def go() -> Any:
        client = mcp_client(transport)
        try:
            agent = MCPAgent(
                llm=ChatFake(script=script, streaming=streaming),
                client=client,
                callbacks=callbacks,
                **(agent_kwargs or {}),
            )
            if around is None:
                return await drive(agent, method, query)
            with around():
                return await drive(agent, method, query)
        finally:
            await client.close_all_sessions()

    return asyncio.run(go())


class ToolStarted(BaseCallbackHandler):
    """A second LangChain handler that signals when a tool call starts."""

    run_inline = True

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def on_tool_start(self, *args: Any, **kwargs: Any) -> None:
        self.started.set()


def spans_named(spans: Iterable[ReadableSpan], name: str) -> List[ReadableSpan]:
    return [span for span in spans if span.name == name]


def only(spans: Iterable[ReadableSpan], name: str) -> ReadableSpan:
    found = spans_named(spans, name)
    assert len(found) == 1, (name, [span.name for span in spans])
    return found[0]


def parent_id(span: ReadableSpan) -> Optional[int]:
    return span.parent.span_id if span.parent is not None else None


def attrs(span: ReadableSpan) -> Dict[str, Any]:
    return dict(span.attributes or {})


def wire(spans: Iterable[ReadableSpan]) -> str:
    """Every name, attribute, event and status text of the spans, as one string."""
    parts: List[str] = []
    for span in spans:
        parts.append(span.name)
        parts.append(repr(dict(span.attributes or {})))
        parts.append(str(span.status.description))
        for event in span.events:
            parts.append(event.name)
            parts.append(repr(dict(event.attributes or {})))
    return "\n".join(parts)
