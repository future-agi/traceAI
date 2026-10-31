"""Released OpenAI router and connection lookup with synthetic loopback transport."""

import __future__

import ast
import asyncio
import http.client
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from zipfile import ZipFile

import pytest
from opentelemetry.trace import StatusCode
from pydantic import SecretStr
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse


@pytest.fixture
def model_server():
    state = SimpleNamespace(result=None, sse=None, requests=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state.requests.append(
                (
                    self.path,
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            payload = (
                json.dumps(state.result).encode() if state.sse is None else state.sse
            )
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json" if state.sse is None else "text/event-stream",
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    yield state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture
def native_router(host, model_server):
    s, ns = host
    config = {}
    storage = {
        "openai.enable": True,
        "openai.api_base_urls": ["http://127.0.0.1:1/unused", model_server.url],
        "openai.api_keys": ["unused-synthetic-key", "synthetic-key"],
        "openai.api_configs": {"1": config},
    }

    async def get_many(*keys):
        return {key: storage[key] for key in keys}

    native = {
        "router": SimpleNamespace(post=lambda *a, **kw: lambda f: f),
        "Request": object,
        "Depends": lambda x: None,
        "get_verified_user": lambda: None,
        "Config": SimpleNamespace(get=AsyncMock(return_value=True), get_many=get_many),
        "Models": ns["Models"],
        "BYPASS_MODEL_ACCESS_CONTROL": False,
        "check_model_access": AsyncMock(),
        "strip_provider_model_prefix": lambda model, prefix: model,
        "is_openai_new_model": lambda model: False,
        "get_headers_and_cookies": AsyncMock(return_value=({}, None)),
        "AIOHTTP_CLIENT_SESSION_SSL": False,
        "get_client_timeout": lambda **kw: 5,
        "JSONCodec": SimpleNamespace(
            dumps=json.dumps, loads=json.loads, JSONDecodeError=json.JSONDecodeError
        ),
        "JSONResponse": JSONResponse,
        "PlainTextResponse": PlainTextResponse,
        "StreamingResponse": StreamingResponse,
        "cleanup_response": AsyncMock(),
        "log": logging.getLogger("host_contract.native_openai"),
        "HTTPException": RuntimeError,
        "ERROR_MESSAGES": SimpleNamespace(
            SERVER_CONNECTION_ERROR="synthetic connection error"
        ),
    }

    def exchange(method, url, data, headers):
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        try:
            connection.request(method, parsed.path, data, headers)
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    async def http_request(method, url, data, headers, **kwargs):
        status, headers, payload = await asyncio.to_thread(
            exchange, method, url, data, headers
        )

        async def chunks():
            for offset in range(0, len(payload), 7):
                yield payload[offset : offset + 7], False

        response = SimpleNamespace(
            headers=headers,
            status=status,
            json=AsyncMock(side_effect=lambda **kw: json.loads(payload)),
            text=AsyncMock(return_value=payload.decode()),
            content=SimpleNamespace(iter_chunks=chunks),
            closed=False,
        )
        response.close = lambda: setattr(response, "closed", True)
        return response

    session = SimpleNamespace(request=AsyncMock(side_effect=http_request))
    native["get_session"] = AsyncMock(return_value=session)
    with ZipFile(os.environ["OPENWEBUI_WHEEL"]) as wheel:
        filename = "open_webui/routers/openai.py"
        tree = ast.parse(wheel.read(filename).decode(), filename)
        names = {
            "get_openai_runtime_config",
            "get_openai_connection",
            "convert_to_responses_payload",
            "convert_responses_result",
            "generate_chat_completion",
        }
        nodes = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name in names
        ]
        assert len(nodes) == len(names)
        # Keep native routing, conversion and config precedence unchanged.
        exec(  # noqa: S102 - released host contract, isolated synthetic dependencies
            compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), native
        )
    s.request.app.state.OPENAI_MODELS = {"synthetic": {"id": "synthetic", "urlIdx": 1}}
    ns["generate_openai_chat_completion"] = native["generate_chat_completion"]
    s.modules["open_webui.routers.openai"].get_openai_connection = native[
        "get_openai_connection"
    ]
    return SimpleNamespace(
        s=s, ns=ns, config=config, storage=storage, server=model_server, session=session
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("config_key", ["index", "url"])
async def test_native_responses_connection_is_rejected_before_dispatch(
    native_router, stream, config_key
):
    n = native_router
    n.config["api_type"] = "responses"
    if config_key == "url":
        n.storage["openai.api_configs"] = {n.server.url: n.config}
    # Even a streaming request can receive a non-streaming Responses result.
    # Reject the connection, not only stream=False, before lossy conversion.
    n.server.result = {
        "object": "response",
        "status": "failed",
        "error": {"message": "private-provider-error"},
    }
    result = await n.ns["generate_chat_completion"](
        n.s.request, {**n.s.body, "stream": stream}, n.s.user
    )
    if stream:
        (chunk,) = [chunk async for chunk in result.body_iterator]
        assert '"error"' in chunk and "Responses API" in chunk
        assert "finish_reason" not in chunk
    else:
        assert "Responses API" in str(result["error"])
        assert "choices" not in result
    n.session.request.assert_not_awaited()
    assert not n.server.requests
    assert not n.s.exporter.get_finished_spans()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tracing", [False, True])
@pytest.mark.parametrize(
    "provider_result",
    [
        None,
        [],
        "private-invalid-body",
        True,
        42,
        {},
        {"detail": "private-invalid-body"},
        {"object": "list", "data": []},
        {"choices": None},
        {"choices": "private-invalid-body"},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{}]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": "private-invalid-body"}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": 42}}]},
        {"choices": [{"message": {"content": "valid"}}, {}]},
    ],
)
async def test_native_invalid_reply_is_an_error(
    native_router, provider_result, stream, tracing
):
    n = native_router
    n.server.result = provider_result
    if not tracing:
        n.s.pipe.valves.api_key = SecretStr("")
    result = await n.ns["generate_chat_completion"](
        n.s.request, {**n.s.body, "stream": stream}, n.s.user
    )
    if stream:
        (chunk,) = [chunk async for chunk in result.body_iterator]
        result = json.loads(chunk.removeprefix("data: "))
    assert result == {"error": "model returned an invalid non-streaming response"}
    n.session.request.assert_awaited_once()
    assert len(n.server.requests) == 1
    assert n.server.requests[0][0] == "/v1/chat/completions"
    spans = n.s.exporter.get_finished_spans()
    if tracing:
        (span,) = spans
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes["openwebui.outcome"] == "error"
        assert span.status.description == "model request failed"
        assert "private-invalid-body" not in repr(span.attributes) + repr(span.events)
    else:
        assert not spans


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "message,finish_reason",
    [
        ({"content": ""}, "stop"),
        ({"content": "private-output"}, "length"),
        ({"content": None}, "content_filter"),
        ({"refusal": "private-refusal"}, "stop"),
        (
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "example", "arguments": "{}"},
                    }
                ]
            },
            "tool_calls",
        ),
        ({"function_call": {"name": "example", "arguments": "{}"}}, "function_call"),
        ({"audio": {"id": "audio_1", "data": "synthetic"}}, "stop"),
    ],
)
async def test_native_minimal_completions_are_preserved(
    native_router, stream, message, finish_reason
):
    n = native_router
    # IDs, model, role, index, usage and timestamps are not universally supplied.
    n.server.result = {
        "choices": [{"message": message, "finish_reason": finish_reason}]
    }
    result = await n.ns["generate_chat_completion"](
        n.s.request, {**n.s.body, "stream": stream}, n.s.user
    )
    if stream:
        (chunk,) = [chunk async for chunk in result.body_iterator]
        result = json.loads(chunk.removeprefix("data: "))
    assert result == n.server.result
    assert len(n.server.requests) == 1
    (span,) = n.s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == "completed"
    assert span.status.status_code != StatusCode.ERROR
    assert "private-" not in repr(span.attributes) + repr(span.events)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("tracing", [False, True])
@pytest.mark.parametrize(
    "error",
    ["private-provider-error", {"message": "private-provider-error", "code": "busy"}],
)
async def test_native_error_envelopes_are_preserved(
    native_router, stream, tracing, error
):
    n = native_router
    n.server.result = {"error": error}
    if not tracing:
        n.s.pipe.valves.api_key = SecretStr("")
    result = await n.ns["generate_chat_completion"](
        n.s.request, {**n.s.body, "stream": stream}, n.s.user
    )
    if stream:
        (chunk,) = [chunk async for chunk in result.body_iterator]
        result = json.loads(chunk.removeprefix("data: "))
    assert result == n.server.result
    assert len(n.server.requests) == 1
    spans = n.s.exporter.get_finished_spans()
    if tracing:
        (span,) = spans
        assert span.attributes["openwebui.outcome"] == "error"
        assert span.status.description == "model request failed"
        assert "private-provider-error" not in repr(span.attributes) + repr(span.events)
    else:
        assert not spans


@pytest.fixture
def native_stream(native_router):
    n = native_router
    native = n.ns["generate_openai_chat_completion"].__globals__
    stream_ns = {
        "CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE": None,
        "log": logging.getLogger("host_contract.native_stream"),
    }
    with ZipFile(os.environ["OPENWEBUI_WHEEL"]) as wheel:
        for filename, names in (
            ("open_webui/utils/misc.py", {"stream_chunks_handler"}),
            (
                "open_webui/utils/session_pool.py",
                {"stream_wrapper", "cleanup_response"},
            ),
        ):
            tree = ast.parse(wheel.read(filename).decode(), filename)
            nodes = [
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in names
            ]
            assert len(nodes) == len(names)
            exec(  # noqa: S102 - unchanged released SSE assembly and native wrapper
                compile(
                    ast.Module(body=nodes, type_ignores=[]),
                    filename,
                    "exec",
                    flags=__future__.annotations.compiler_flag,
                ),
                stream_ns,
            )
        filename = "open_webui/routers/openai.py"
        tree = ast.parse(wheel.read(filename).decode(), filename)
        nodes = [
            node
            for node in tree.body
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_clean_proxy_headers"
            )
            or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "_STRIP_PROXY_HEADERS"
                    for target in node.targets
                )
            )
        ]
        exec(  # noqa: S102 - unchanged released proxy header handling
            compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), native
        )
    native["stream_wrapper"] = stream_ns["stream_wrapper"]
    return n


@pytest.mark.parametrize(
    "count,frames,done,expected",
    [
        (1, [[(0, True)]], False, "completed"),
        (1, [[(0, False)]], False, "incomplete"),
        (2, [[(0, True), (1, False)]], False, "incomplete"),
        (2, [[(0, False), (1, True)]], False, "incomplete"),
        (2, [[(0, True)]], False, "incomplete"),
        (2, [[(1, True)]], False, "incomplete"),
        (2, [[(0, True)], [(1, False)]], False, "incomplete"),
        (2, [[(0, True)], [(0, True)]], False, "incomplete"),
        (2, [[(0, True), (1, True)]], False, "completed"),
        (2, [[(1, True)], [(0, True)]], False, "completed"),
        (2, [[(0, False), (1, False)]], True, "completed"),
    ],
)
async def test_native_stream_completes_all_requested_choices(
    native_stream, count, frames, done, expected
):
    n = native_stream
    n.server.sse = (
        "".join(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": index,
                            "delta": {"content": "private-output"},
                            "finish_reason": "stop" if finished else None,
                        }
                        for index, finished in choices
                    ]
                }
            )
            + "\n\n"
            for choices in frames
        )
        + ("data: [DONE]\n\n" if done else "")
    ).encode()
    response = await n.ns["generate_chat_completion"](
        n.s.request, {**n.s.body, "stream": True, "n": count}, n.s.user
    )
    assert b"".join([chunk async for chunk in response.body_iterator]) == n.server.sse
    assert len(n.server.requests) == 1
    assert n.server.requests[0][0] == "/v1/chat/completions"
    assert n.server.requests[0][1]["n"] == count
    (span,) = n.s.exporter.get_finished_spans()
    assert span.attributes["openwebui.outcome"] == expected
    assert (span.status.status_code == StatusCode.ERROR) == (expected == "incomplete")
    assert "private-" not in repr(span.attributes) + repr(span.events)


async def test_native_chat_completion_and_config_refresh(native_router):
    n = native_router
    n.server.result = n.s.delegate.return_value
    # Index settings take precedence over legacy URL settings in the host.
    n.storage["openai.api_configs"][n.server.url] = {"api_type": "responses"}
    result = await n.ns["generate_chat_completion"](n.s.request, n.s.body, n.s.user)
    assert result == n.server.result
    (span,) = n.s.exporter.get_finished_spans()
    assert span.attributes["llm.token_count.total"] == 10
    assert span.attributes["openwebui.outcome"] == "completed"
    assert len(n.server.requests) == 1
    # Do not cache a successful check after an administrator changes API type.
    n.config["api_type"] = "responses"
    result = await n.ns["generate_chat_completion"](n.s.request, n.s.body, n.s.user)
    assert "Responses API" in str(result["error"])
    assert len(n.server.requests) == 1
    assert len(n.s.exporter.get_finished_spans()) == 1
