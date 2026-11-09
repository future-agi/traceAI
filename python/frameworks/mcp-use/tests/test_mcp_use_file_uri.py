"""File-URI privacy regressions: synthetic paths, real agent IO and OTLP."""

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
from traceai_mcp_use._text import scrub_local

HOME = "/data/synthetic-tenant-user"


@pytest.mark.parametrize("uri", [
    "file://" + HOME + "/models/local.gguf",
    "file:" + HOME + "/models/local.gguf",
    "FILE://" + HOME + "/models/local.gguf",
    "file://localhost" + HOME + "/models/local.gguf",
    "file://LOCALHOST" + HOME + "/models/local.gguf",
    "file://" + HOME,
    "file://" + HOME + "/",
    "file://" + HOME + "/models/local.gguf?mode=read#weights",
    "file:///data/%73ynthetic-tenant-user/models/local.gguf",
    "file:///data%2Fsynthetic-tenant-user%2fmodels/local.gguf",
    "file:///%64ata/synthetic-tenant-user/models/local.gguf",
    ("file://" + HOME + "/models/local.gguf").replace("/", r"\/"),
    "file:///home/synthetic-other/private.txt",
    "file:///Users/synthetic-other/private.txt",
    "file:///tmp/synthetic-model.gguf",
    "file:///C:/Users/synthetic-other/private.txt",
    "file:///C%3A%5CUsers%5Csynthetic-other%5Cprivate.txt",
])
def test_file_uri_sensitive_paths_are_scrubbed(monkeypatch, uri):
    monkeypatch.setenv("HOME", HOME)
    assert scrub_local(uri) == "[path]"
    assert scrub_local("ECHO-RESULT:" + uri) == "ECHO-RESULT:[path]"
    # Keep surrounding text and JSON intact, rather than discarding content.
    assert scrub_local("model='" + uri + "', done") == "model='[path]', done"
    assert json.loads(scrub_local('{"model":"' + uri + '"}')) == {"model": "[path]"}


@pytest.mark.parametrize("home", ["/custom.mount/synthetic user", "/data/synthetic+user", "/data/synthetic-\u00e9"])
def test_percent_encoded_home_file_uris(monkeypatch, home):
    monkeypatch.setenv("HOME", home)
    uri = "file://" + quote(home + "/model with spaces.gguf")
    assert scrub_local(uri) == "[path]"


@pytest.mark.parametrize("text", [
    "https://example.com" + HOME + "/models/local.gguf",
    "http://example.com/home/public/model.gguf",
    "https://example.com/data/%73ynthetic-tenant-user/model.gguf",
    "file:///data/public/model.gguf",
    "file:///data/synthetic-tenant-user-other/model.gguf",
    "file:///data/synthetic-tenant-user2/model.gguf",
    "file:///data/synthetic-tenant-user.bak/model.gguf",
    "file:///data/synthetic-tenant-user%2Dother/model.gguf",
    "file:///api/v1/items",
    "file://files.example/data/public/model.gguf",
    "file:relative/model.gguf",
    "profile:///data/synthetic-tenant-user/model.gguf",
    "https://example.com/file:///data/synthetic-tenant-user/model.gguf",
    "ordinary model identifier",
])
def test_non_sensitive_uri_content_is_preserved(monkeypatch, text):
    monkeypatch.setenv("HOME", HOME)
    assert scrub_local(text) == text


def test_file_uri_uses_current_home_and_root_is_not_a_catch_all(monkeypatch):
    uri = "file:///data/synthetic-second/model.gguf"
    monkeypatch.setenv("HOME", HOME)
    assert scrub_local(uri) == uri
    monkeypatch.setenv("HOME", "/data/synthetic-second/")
    assert scrub_local(uri) == "[path]"
    monkeypatch.setenv("HOME", "/")
    assert scrub_local("file:///api/v1/items") == "file:///api/v1/items"


@pytest.mark.parametrize("method", ["run", "stream", "stream_events"])
@pytest.mark.parametrize("hidden", [False, True])
def test_provider_file_uri_models_are_private(monkeypatch, method, hidden):
    monkeypatch.setenv("HOME", HOME)
    spans = provider_spans(method, hidden, model="file://" + HOME + "/models/local.gguf")
    assert HOME not in wire(spans)
    llm, = [s for s in spans if s.attributes.get("gen_ai.span.kind") == "LLM"]
    assert llm.name == "chat [path]"
    assert llm.attributes["gen_ai.request.model"] == "[path]"
    assert llm.attributes["gen_ai.response.model"] == "[path]"
    assert llm.attributes["gen_ai.usage.total_tokens"] == 75


@pytest.mark.parametrize("hidden", [False, True])
def test_file_uri_privacy_reaches_otlp_without_hiding_benign_content(monkeypatch, hidden):
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setenv("FI_HIDE_INPUTS", str(hidden).lower())
    monkeypatch.setenv("FI_HIDE_OUTPUTS", str(hidden).lower())
    uri = "file://localhost" + HOME + "/data/private.txt"
    control = "https://example.com/public/model.gguf"
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
                        script=[tool_call("echo", {"text": uri + " " + control}), answer("done")],
                        model_name="file://" + HOME + "/models/local.gguf",
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
    assert HOME not in json.dumps(spans)
    agent, = [s for s in spans if _attrs(s)["gen_ai.span.kind"] == "AGENT"]
    llms = [s for s in spans if _attrs(s)["gen_ai.span.kind"] == "LLM"]
    assert len(llms) == 2
    for llm in llms:
        assert llm["name"] == "chat [path]"
        assert _attrs(llm)["gen_ai.request.model"] == "[path]"
        assert int(_attrs(llm)["gen_ai.usage.total_tokens"]) == 18
    assert all(s["parentSpanId"] == agent["spanId"] for s in spans if s is not agent)
    tool, = [s for s in spans if _attrs(s)["gen_ai.span.kind"] == "TOOL"]
    if hidden:
        assert "gen_ai.tool.call.arguments" not in _attrs(tool)
        assert "gen_ai.tool.call.result" not in _attrs(tool)
        assert _attrs(tool)["output.value"] == "__REDACTED__"
    else:
        assert "ECHO-RESULT:[path] " + control in _attrs(tool)["gen_ai.tool.call.result"]
        assert control in _attrs(tool)["gen_ai.tool.call.arguments"]
    assert not handler._runs and not handler._roots
