"""The auto module with the real MCP and agent packages: registry, report rows and capture.

``traceai.init`` borrows a loopback F01 manager (or runs with attachments OFF) and
enables each installed package by name. Every test skips the packages that are not
installed; ``TRACEAI_EXPECT_MCP_AGENT_ADAPTERS=1`` (set by CI) turns a missing
package into a failure so a broken install cannot pass silently.
"""

import asyncio
import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

traceai = pytest.importorskip("traceai")

from fi_instrumentation import TraceConfig  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from _harness import data_uri, flush, harness, png, refs  # noqa: E402

provider = harness.provider  # the loopback OpenAI-compatible API (fixture)

# report row name -> (instrumentor package, SDK module, descriptor module, descriptor name)
PACKAGES = {
    "mcp": ("traceai_mcp", "mcp", "traceai_mcp", "mcp_attachment_capabilities"),
    "mcp_use": ("traceai_mcp_use", "mcp_use", "traceai_mcp_use._attachments", "mcp_use_attachment_capabilities"),
    "openai_agents": ("traceai_openai_agents", "agents", "traceai_openai_agents._attachments",
                      "openai_agents_attachment_capabilities"),
    "claude_agent_sdk": ("traceai_claude_agent_sdk", "claude_agent_sdk", "traceai_claude_agent_sdk._attachments",
                         "claude_agent_attachment_capabilities"),
    "smolagents": ("traceai_smolagents", "smolagents", "traceai_smolagents._attachments",
                   "smolagents_attachment_capabilities"),
    "firecrawl": ("traceai_firecrawl", "firecrawl", "traceai_firecrawl._attachments",
                  "firecrawl_attachment_capabilities"),
}
INSTRUMENTORS = {"openai_agents": "OpenAIAgentsInstrumentor", "claude_agent_sdk": "ClaudeAgentInstrumentor",
                 "smolagents": "SmolagentsInstrumentor", "firecrawl": "FirecrawlInstrumentor"}
SERVER = Path(__file__).resolve().parent / "_mcp_media_server.py"
EXPECT_ALL = os.environ.get("TRACEAI_EXPECT_MCP_AGENT_ADAPTERS") == "1"


def require(name):
    package, sdk = PACKAGES[name][:2]
    for module in (package, sdk):
        if importlib.util.find_spec(module) is None:
            if EXPECT_ALL:
                pytest.fail("%s is not installed" % module)
            pytest.skip("%s is not installed" % module)


@pytest.fixture
def auto(monkeypatch):
    """Fresh auto state; uninstruments whatever a test enabled."""
    for attr, value in (("_tracer_provider", None), ("_init_options", None), ("_instrumented", set()),
                        ("_adapter_status", {}), ("_attachment_manager_borrowed", False)):
        monkeypatch.setattr(traceai, attr, value)
    monkeypatch.setenv("FI_API_KEY", "SYNTHETIC_KEY")
    monkeypatch.setenv("FI_SECRET_KEY", "SYNTHETIC_SECRET")
    started = []

    def init(libraries=(), **options):
        tracer_provider = traceai.init(project_name="synthetic", project_type="observe",
                                       auto_instrument=list(libraries), verbose=False,
                                       set_global_tracer_provider=False, **options)
        exporter = InMemorySpanExporter()
        tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
        started.append((tracer_provider, list(libraries)))
        return tracer_provider, exporter

    yield init
    for tracer_provider, libraries in started:
        for name in libraries:
            module = PACKAGES[name][0]
            if module in traceai._ENTRY_POINTS:  # mcp-use has no instrumentor
                instrumentor = getattr(importlib.import_module(module), traceai._ENTRY_POINTS[module])()
                if instrumentor.is_instrumented_by_opentelemetry:
                    instrumentor.uninstrument()
        tracer_provider.shutdown()


@pytest.mark.parametrize("name", sorted(PACKAGES))
def test_report_row_is_the_installed_descriptor(name):
    require(name)
    _, _, path, attr = PACKAGES[name]
    descriptor = getattr(importlib.import_module(path), attr)
    rows = {row["name"]: row for row in traceai.report()["integrations"]}
    assert rows[name]["media"] == "provider_adapter"
    assert rows[name]["media_operations"] == list(descriptor["operations"])
    assert rows[name]["package"] == PACKAGES[name][0]


@pytest.mark.parametrize("name", sorted(INSTRUMENTORS))
def test_enabling_by_name_instruments_with_attachments_on_and_off(name, auto, f01):
    require(name)
    # OFF (no manager): the instrumentor accepts auto's OFF keyword arguments.
    auto([name])
    assert traceai.attachment_status()["adapters"][name] == "registered"
    instrumentor = getattr(importlib.import_module(PACKAGES[name][0]), INSTRUMENTORS[name])()
    assert instrumentor.is_instrumented_by_opentelemetry
    instrumentor.uninstrument()
    traceai._tracer_provider = None
    traceai._init_options = None
    # ON (a borrowed, enabled manager): the same, with attachments=.
    manager = f01.manager()
    try:
        auto([name], attachment_manager=manager)
        assert traceai.attachment_status()["adapters"][name] == "registered"
        assert traceai.get_attachment_manager() is manager
    finally:
        manager.shutdown(5)


def _mcp_session(calls):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await calls(traceai.wrap_mcp_client(session))

    return asyncio.run(go())


def test_wrap_mcp_client_captures_on_the_shared_manager(auto, f01):
    require("mcp")
    sys.path.insert(0, str(SERVER.parent))
    import _mcp_media_server as server

    manager = f01.manager()
    try:
        _, exporter = auto(["mcp"], attachment_manager=manager)

        async def calls(session):
            return await session.call_tool("screenshot", {})

        _mcp_session(calls)
        flush(manager)
        assert [(u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads] == [
            ("image/png", "output", "tool_result")]
        assert f01.payloads == [server.png(1)]
        assert sum(len(refs(s)) for s in exporter.get_finished_spans() if s.name == "mcp.tools/call") == 1
    finally:
        manager.shutdown(5)


def test_wrap_mcp_client_with_attachments_off_uploads_nothing(auto, f01):
    require("mcp")
    _, exporter = auto(["mcp"])

    async def calls(session):
        return await session.call_tool("screenshot", {})

    result = _mcp_session(calls)
    assert result.content  # the SDK result is unchanged
    assert f01.uploads == [] and f01.payloads == []
    assert all(not refs(s) for s in exporter.get_finished_spans())


def _mcp_use_run(callback, tool):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frameworks" / "mcp-use" / "tests"))
    import _mcp_use_support as support
    from mcp_use import MCPAgent, MCPClient

    async def go():
        client = MCPClient.from_dict({"mcpServers": {"media": {"command": sys.executable, "args": [str(SERVER)]}}})
        try:
            agent = MCPAgent(llm=support.ChatFake(script=[support.tool_call(tool, {}, "call_media_1"),
                                                          support.answer()]),
                             client=client, callbacks=[callback])
            return await agent.run(support.PROMPT)
        finally:
            await client.close_all_sessions()

    return asyncio.run(go())


def test_mcp_use_callback_captures_on_the_shared_manager(auto, f01):
    require("mcp_use")
    manager = f01.manager()
    try:
        auto(["mcp_use"], attachment_manager=manager)
        _mcp_use_run(traceai.mcp_use_callback(), "screenshot")
        flush(manager)
        assert [(u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads] == [
            ("image/png", "output", "tool_result")]
    finally:
        manager.shutdown(5)


def test_mcp_use_callback_carries_auto_hide_flags(auto, f01):
    require("mcp_use")
    manager = f01.manager()
    try:
        auto(["mcp_use"], attachment_manager=manager, trace_config=TraceConfig(hide_outputs=True))
        _mcp_use_run(traceai.mcp_use_callback(), "screenshot")
        flush(manager)
        assert f01.uploads == []
    finally:
        manager.shutdown(5)


def test_smolagents_enabled_by_name_captures_a_model_input_image(auto, f01, provider):
    require("smolagents")
    pil = pytest.importorskip("PIL.Image")
    pytest.importorskip("openai")
    import smolagents

    manager = f01.manager()
    try:
        auto(["smolagents"], attachment_manager=manager)
        image = pil.new("RGB", (8, 6), (7, 10, 20))
        content = [{"type": "text", "text": "look"}, {"type": "image", "image": image}]
        model = smolagents.OpenAIServerModel(model_id="m", api_base=provider.url + "/v1", api_key="SYNTHETIC_KEY")
        if hasattr(model, "generate"):
            from smolagents.models import ChatMessage, MessageRole

            model.generate([ChatMessage(role=MessageRole.USER, content=content)])
        else:
            model([{"role": "user", "content": content}])
        flush(manager)
        assert [(u["mime_type"], u["direction"], u["purpose"]) for u in f01.uploads] == [
            ("image/png", "input", "model_input")]
    finally:
        manager.shutdown(5)


def test_firecrawl_enabled_by_name_captures_an_inline_screenshot(auto, f01):
    require("firecrawl")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "frameworks" / "firecrawl" / "tests"))
    from _firecrawl_fake import FakeFirecrawl
    from firecrawl import Firecrawl

    shot = png(seed=71)
    fake = FakeFirecrawl()
    manager = f01.manager()
    try:
        _, exporter = auto(["firecrawl"], attachment_manager=manager)
        fake.routes[("POST", "/v2/scrape")] = (200, {"success": True, "data": {
            "markdown": "body", "screenshot": data_uri("image/png", shot),
            "metadata": {"sourceURL": "https://example.com"}}})
        document = Firecrawl(api_key="fc-SYNTHETIC-PLACEHOLDER", api_url=fake.origin, max_retries=1).scrape(
            "https://example.com", formats=["screenshot"])
        flush(manager)
        assert document.screenshot == data_uri("image/png", shot)
        assert f01.payloads == [shot]
    finally:
        manager.shutdown(5)
        fake.close()
