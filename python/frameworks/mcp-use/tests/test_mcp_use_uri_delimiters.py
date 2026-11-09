"""Literal URI sub-delimiters in HOME, through real MCP IO and OTLP."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import pytest
from _mcp_use_support import ChatFake, answer, mcp_client, tool_call, wire
from fi_instrumentation import TraceConfig
from mcp_use import MCPAgent
from test_mcp_use_contract import Receiver, _attrs, _register
from test_mcp_use_provider import provider_spans

from traceai_mcp_use import FutureAGICallback
from traceai_mcp_use._text import Secrets, clean, scrub_local

HOMES = ["/data/synthetic,tenant-user", "/data/synthetic;tenant-user", "/data/synthetic)tenant-user"]
PUBLIC = "https://example.com/public/model.gguf"


def _uri(home, encoded):
    path = home + "/models/local.gguf"
    return "file://" + (quote(path, safe="/") if encoded else path)


@pytest.mark.parametrize("home", HOMES + [
    "/data/synthetic,tenant;user)directory",
    "/home/synthetic,tenant-user",
    "/data/synthetic-tenant,",
])
@pytest.mark.parametrize("form", ["literal", "encoded", "mixed", "escaped", "localhost", "short"])
def test_complete_home_is_scrubbed_before_uri_text_delimiters(monkeypatch, home, form):
    monkeypatch.setenv("HOME", home)
    uri = _uri(home, form == "encoded")
    if form == "mixed":
        uri = uri.replace("synthetic", "%73ynthetic").replace("/data/", "/%64ata%2f")
    elif form == "escaped":
        uri = uri.replace("/", r"\/")
    elif form == "localhost":
        uri = uri.replace("file://", "FILE://localhost")
    elif form == "short":
        uri = uri.replace("file://", "file:")
    assert scrub_local(uri) == "[path]"
    assert clean("chat " + uri, Secrets(), 256) == "chat [path]"
    assert scrub_local("model='" + uri + "', done") == "model='[path]', done"
    assert json.loads(scrub_local('{"model":"' + uri + '"}')) == {"model": "[path]"}
    for separator in [",", ";", ")"]:
        # Do not solve the URI bug by swallowing unrelated adjacent content.
        assert scrub_local("(" + uri + separator + PUBLIC) == "([path]" + separator + PUBLIC
        assert scrub_local(uri + separator + uri) == "[path]" + separator + "[path]"


@pytest.mark.parametrize("home", HOMES)
@pytest.mark.parametrize("encoded", [False, True])
def test_bare_home_and_benign_uri_controls(monkeypatch, home, encoded):
    monkeypatch.setenv("HOME", home)
    root = "file://" + (quote(home, safe="/") if encoded else home)
    assert scrub_local(root) == "[path]"
    assert scrub_local("(" + root + "), done") == "([path]), done"
    for value in [
        PUBLIC,
        "https://example.com" + home + "/model.gguf",
        root + "-other/model.gguf",
        root + ".bak/model.gguf",
        "file:///data/public/model.gguf",
        "file:///api/v1/items",
        "https://example.com/" + root,
        "ordinary benign answer: content remains visible",
    ]:
        assert scrub_local(value) == value
    # Both orders must preserve the benign URI and still scrub the private one.
    public_file = "file:///data/public/model.gguf"
    for separator in [",", ";", ")"]:
        assert scrub_local(root + separator + public_file) == "[path]" + separator + public_file
        assert scrub_local(public_file + separator + root) == public_file + separator + "[path]"


@pytest.mark.parametrize("home", HOMES)
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
@pytest.mark.parametrize("method", ["run", "stream", "stream_events"])
def test_provider_subdelimiter_models_are_private(monkeypatch, home, encoded, hidden, method):
    monkeypatch.setenv("HOME", home)
    spans = provider_spans(method, hidden=hidden, model=_uri(home, encoded))
    assert home not in wire(spans)
    llm, = [span for span in spans if span.attributes.get("gen_ai.span.kind") == "LLM"]
    assert llm.name == "chat [path]"
    assert llm.attributes["gen_ai.request.model"] == "[path]"
    assert llm.attributes["gen_ai.response.model"] == "[path]"
    assert llm.attributes["gen_ai.usage.total_tokens"] == 75


@pytest.mark.parametrize("home", HOMES)
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("hidden", [False, True])
def test_subdelimiter_privacy_in_actual_otlp(monkeypatch, home, encoded, hidden):
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
    monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
    uri = _uri(home, encoded)
    with Receiver() as receiver:
        provider = _register(receiver, monkeypatch)
        handler = FutureAGICallback(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=hidden, hide_outputs=hidden),
        )

        async def run():
            client = mcp_client("stdio")
            try:
                agent = MCPAgent(
                    llm=ChatFake(
                        script=[tool_call("echo", {"text": uri + " " + PUBLIC}), answer("done")],
                        model_name=uri,
                    ),
                    client=client,
                    callbacks=[handler],
                )
                assert await agent.run("hello") == "done"
            finally:
                await client.close_all_sessions()

        try:
            asyncio.run(run())
            assert provider.force_flush(timeout_millis=10_000)
        finally:
            provider.shutdown()
        spans = receiver.spans()
        assert receiver.requests()

    assert len(spans) == 4
    # Exported protobuf-decoded spans, not an in-memory callback approximation.
    print(json.dumps({"home": home, "encoded": encoded, "hidden": hidden, "spans": spans}))
    assert home not in json.dumps(spans)
    agent, = [span for span in spans if _attrs(span)["gen_ai.span.kind"] == "AGENT"]
    llms = [span for span in spans if _attrs(span)["gen_ai.span.kind"] == "LLM"]
    assert len(llms) == 2
    assert all(span["parentSpanId"] == agent["spanId"] for span in spans if span is not agent)
    for llm in llms:
        assert llm["name"] == "chat [path]"
        assert _attrs(llm)["gen_ai.request.model"] == "[path]"
        assert int(_attrs(llm)["gen_ai.usage.total_tokens"]) == 18
    tool, = [span for span in spans if _attrs(span)["gen_ai.span.kind"] == "TOOL"]
    if hidden:
        assert "gen_ai.tool.call.arguments" not in _attrs(tool)
        assert "gen_ai.tool.call.result" not in _attrs(tool)
        assert _attrs(tool)["output.value"] == "__REDACTED__"
    else:
        assert "ECHO-RESULT:[path] " + PUBLIC in _attrs(tool)["gen_ai.tool.call.result"]
        assert PUBLIC in _attrs(tool)["gen_ai.tool.call.arguments"]
    assert not handler._runs and not handler._roots
