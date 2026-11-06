"""Real SDK/loopback regressions for tool privacy, text endpoints and finish reasons."""

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version

import litellm
import pytest
from fi_instrumentation import TraceConfig
from fi_instrumentation.fi_types import SpanAttributes
from opentelemetry import context
from opentelemetry.context import _SUPPRESS_INSTRUMENTATION_KEY
from opentelemetry.trace import StatusCode
from traceai_litellm import LiteLLMInstrumentor

from test_http_regressions import capture, server  # noqa: F401

FINISH = SpanAttributes.GEN_AI_RESPONSE_FINISH_REASONS


@pytest.mark.skipif(
    not hasattr(litellm, "responses"), reason="Responses absent at floor"
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_mcp_credentials_never_exported(server, capture, asynchronous, streaming):
    http, base = server
    exporter, _ = capture
    tool = {
        "type": "mcp",
        "server_label": "synthetic-server",
        "server_url": "https://synthetic.invalid/mcp?token=synthetic-url-token",
        "authorization": "synthetic-mcp-authorization",
        "headers": {
            "Authorization": "synthetic-header-auth",
            "X-Api-Key": "synthetic-header-key",
        },
        "require_approval": "never",
    }
    before = copy.deepcopy(tool)
    params = dict(
        model="openai/gpt-4o-mini",
        input="hello",
        tools=[tool],
        api_key="synthetic-provider-key",
        api_base=base,
        stream=streaming,
        num_retries=0,
    )
    result = (
        await litellm.aresponses(**params)
        if asynchronous
        else litellm.responses(**params)
    )
    if streaming:
        chunks = [chunk async for chunk in result] if asynchronous else list(result)
        assert chunks[-1].type == "response.completed"
    else:
        assert result.output[0].content[0].text == "synthetic-output"
    assert tool == before
    sent = http.requests[-1][1]["tools"][0]
    assert sent["authorization"] == before["authorization"]
    assert sent["server_url"] == before["server_url"]
    assert sent["headers"] == before["headers"]
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    serialized = spans[0].to_json()
    for credential in (
        before["authorization"],
        *before["headers"].values(),
        "synthetic-url-token",
    ):
        assert credential not in serialized
    tools = json.loads(spans[0].attributes["gen_ai.request.parameters"])["tools"]
    assert tools[0]["type"] == "mcp"
    assert tools[0]["server_label"] == "synthetic-server"


class CompletionHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, body))
        if self.server.fail:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "error": {
                            "message": "synthetic-failure",
                            "type": "invalid_request_error",
                        }
                    }
                ).encode()
            )
            return
        text = not self.path.endswith("/chat/completions")
        base = {
            "id": "cmpl_synthetic",
            "object": "text_completion" if text else "chat.completion",
            "created": 1,
            "model": "gpt-3.5-turbo-instruct" if text else "gpt-4o-mini",
        }
        choices = self.server.choice_reasons
        self.send_response(200)
        if body.get("stream"):
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if not text:
                base["object"] = "chat.completion.chunk"
            chunks = []
            for part in ("synthetic-", "output"):
                chunks.append(
                    {
                        **base,
                        "choices": [
                            {
                                "index": index,
                                "finish_reason": None,
                                **(
                                    {"text": part}
                                    if text
                                    else {
                                        "delta": {"role": "assistant", "content": part}
                                    }
                                ),
                            }
                            for index, _ in choices
                        ],
                    }
                )
            chunks.append(
                {
                    **base,
                    "choices": [
                        {
                            "index": index,
                            "finish_reason": reason,
                            **({"text": ""} if text else {"delta": {}}),
                        }
                        for index, reason in choices
                    ],
                }
            )
            chunks.append(
                {
                    **base,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                    },
                }
            )
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        **base,
                        "choices": [
                            {
                                "index": index,
                                "finish_reason": reason,
                                **(
                                    {"text": "synthetic-output", "logprobs": None}
                                    if text
                                    else {
                                        "message": {
                                            "role": "assistant",
                                            "content": "synthetic-output",
                                        }
                                    }
                                ),
                            }
                            for index, reason in choices
                        ],
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "total_tokens": 5,
                        },
                    }
                ).encode()
            )


@pytest.fixture
def completion_server():
    http = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
    http.requests = []
    http.choice_reasons = [(0, "stop")]
    http.fail = False
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield http, "http://127.0.0.1:%d/v1" % http.server_port
    http.shutdown()
    http.server_close()
    thread.join()


async def complete(
    base, asynchronous=False, streaming=False, text=True, consume=True, **kwargs
):
    params = dict(
        api_base=base,
        api_key="synthetic-provider-key",
        num_retries=0,
        stream=streaming,
        **kwargs,
    )
    if streaming:
        params.setdefault("stream_options", {"include_usage": True})
    if text:
        params.update(
            model="text-completion-openai/gpt-3.5-turbo-instruct",
            prompt="synthetic-prompt",
        )
        result = (
            await litellm.atext_completion(**params)
            if asynchronous
            else litellm.text_completion(**params)
        )
    else:
        params.update(
            model="openai/gpt-4o-mini", messages=[{"role": "user", "content": "hello"}]
        )
        result = (
            await litellm.acompletion(**params)
            if asynchronous
            else litellm.completion(**params)
        )
    if streaming and consume:
        return [chunk async for chunk in result] if asynchronous else list(result)
    return result


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_text_completion_real_sdk(
    completion_server, capture, asynchronous, streaming
):
    http, base = completion_server
    exporter, _ = capture
    result = await complete(base, asynchronous, streaming)
    assert result
    assert http.requests[-1][0].endswith("/completions")
    # The floor SDK normalizes the provider prompt to a singleton batch.
    expected_prompt = (
        ["synthetic-prompt"] if version("litellm") == "1.43.0" else "synthetic-prompt"
    )
    assert http.requests[-1][1]["prompt"] == expected_prompt
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == ("atext_completion" if asynchronous else "text_completion")
    attrs = spans[0].attributes
    assert json.loads(attrs["input.value"])["messages"][0]["content"] == "synthetic-prompt"
    assert attrs["output.value"] == "synthetic-output"
    assert attrs["gen_ai.output.messages.0.message.content"] == "synthetic-output"
    assert attrs["gen_ai.output.messages.0.message.role"] == "assistant"
    totals = []
    for chunk in result if streaming else [result]:
        data = chunk if isinstance(chunk, dict) else chunk.model_dump()
        usage = data.get("usage")
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if usage and usage.get("total_tokens") is not None:
            totals.append(usage["total_tokens"])
    if not totals:
        # The floor's async text stream exposes no usage chunk at all.
        assert version("litellm") == "1.43.0" and streaming and asynchronous
        assert "gen_ai.usage.total_tokens" not in attrs
    else:
        assert attrs["gen_ai.usage.total_tokens"] == totals[-1]
        if not streaming or version("litellm") != "1.43.0":
            assert totals[-1] == 5
    assert "synthetic-provider-key" not in spans[0].to_json()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("text", [False, True])
@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", "tool_calls"])
@pytest.mark.asyncio
async def test_reported_finish_reasons(
    completion_server, capture, asynchronous, streaming, text, reason
):
    http, base = completion_server
    http.choice_reasons = [(0, reason)]
    exporter, _ = capture
    await complete(base, asynchronous, streaming, text)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes.get(FINISH) == (reason,)
    assert (
        spans[0].attributes.get("gen_ai.output.messages.0.message.finish_reason")
        == reason
    )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("text", [False, True])
@pytest.mark.asyncio
async def test_finish_reasons_keep_choice_identity(
    completion_server, capture, asynchronous, streaming, text
):
    http, base = completion_server
    http.choice_reasons = [(1, "length"), (0, "stop")]
    exporter, _ = capture
    result = await complete(base, asynchronous, streaming, text, n=2)
    # LiteLLM itself can renumber non-streamed chat choices and collapse terminal
    # stream choices. Uninstrumented HTTP controls reproduce this. Record exactly
    # what the SDK exposes, not an invented reconstruction of the wire response.
    # Sparse/interleaved identity is independently pinned with controlled chunks.
    reported = {}
    for chunk in result if streaming else [result]:
        data = chunk if isinstance(chunk, dict) else chunk.model_dump()
        for choice in data["choices"]:
            if choice.get("finish_reason") is not None:
                reported[choice["index"]] = choice["finish_reason"]
    assert reported
    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs.get(FINISH) == tuple(reported[index] for index in sorted(reported))
    for index, reason in reported.items():
        assert (
            attrs.get(f"gen_ai.output.messages.{index}.message.finish_reason") == reason
        )


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("text", [False, True])
@pytest.mark.asyncio
async def test_no_invented_reason_on_early_close(
    completion_server, capture, asynchronous, text
):
    _, base = completion_server
    exporter, _ = capture
    stream = await complete(base, asynchronous, True, text, consume=False)
    if asynchronous:
        await stream.__anext__()
        await stream.aclose()
    else:
        next(stream)
        stream.close()
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert FINISH not in spans[0].attributes


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_text_error_and_suppression(
    completion_server, capture, asynchronous, streaming
):
    http, base = completion_server
    exporter, _ = capture
    token = context.attach(context.set_value(_SUPPRESS_INSTRUMENTATION_KEY, True))
    try:
        assert await complete(base, asynchronous, streaming)
    finally:
        context.detach(token)
    assert not exporter.get_finished_spans()
    http.fail = True
    swallowed_by_floor = (
        version("litellm") == "1.43.0" and streaming and not asynchronous
    )
    if swallowed_by_floor:
        # 1.43.0 catches a sync text-stream HTTP error inside the SDK and yields
        # a None sentinel. Prove this with instrumentation fully removed; do not
        # change SDK behavior or fabricate an exception/finish reason in tracing.
        inst = LiteLLMInstrumentor()
        inst.uninstrument()
        control = await complete(base, asynchronous, streaming)
        assert control == [None]
        assert not exporter.get_finished_spans()
        inst.instrument(tracer_provider=capture[1], config=TraceConfig())
        assert await complete(base, asynchronous, streaming) == control
    else:
        with pytest.raises(Exception):
            await complete(base, asynchronous, streaming)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    expected_status = StatusCode.OK if swallowed_by_floor else StatusCode.ERROR
    assert spans[0].status.status_code is expected_status
    assert FINISH not in spans[0].attributes


@pytest.mark.parametrize(
    "flag",
    [
        "hide_inputs",
        "hide_input_text",
        "hide_input_messages",
        "hide_outputs",
        "hide_output_text",
        "hide_output_messages",
    ],
)
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.asyncio
async def test_text_privacy(completion_server, capture, flag, asynchronous, streaming):
    _, base = completion_server
    exporter, provider = capture
    inst = LiteLLMInstrumentor()
    inst.uninstrument()
    inst.instrument(tracer_provider=provider, config=TraceConfig(**{flag: True}))
    await complete(base, asynchronous, streaming)
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert ("synthetic-prompt" if "input" in flag else "synthetic-output") not in spans[
        0
    ].to_json()
