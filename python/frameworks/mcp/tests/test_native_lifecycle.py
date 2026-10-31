"""Exercise install, removal and pending import hooks in fresh interpreters."""
import subprocess
import sys
import textwrap


def run(script):
    subprocess.run([sys.executable, "-c", textwrap.dedent(script)], check=True)


def test_real_transport_restored_and_reinstrumented():
    run('''
        import mcp.client.streamable_http as module
        from traceai_mcp import MCPInstrumentor
        name = "streamable_http_client" if hasattr(module, "streamable_http_client") else "streamablehttp_client"
        original = getattr(module, name)
        i = MCPInstrumentor()
        for _ in range(2):
            i.instrument(skip_dep_check=True)
            assert getattr(module, name) is not original
            i.uninstrument()
            assert getattr(module, name) is original
    ''')


def test_pending_hook_does_not_wrap_after_uninstrument():
    run('''
        from traceai_mcp import MCPInstrumentor
        i = MCPInstrumentor()
        i.instrument(skip_dep_check=True)
        i.uninstrument()
        import mcp.client.streamable_http as module
        name = "streamable_http_client" if hasattr(module, "streamable_http_client") else "streamablehttp_client"
        from wrapt import ObjectProxy
        assert not isinstance(getattr(module, name), ObjectProxy)
    ''')


def test_pending_hooks_reinstall_once():
    run('''
        import asyncio
        from traceai_mcp import MCPInstrumentor, InstrumentedStreamReader
        i = MCPInstrumentor()
        i.instrument(skip_dep_check=True)
        i.uninstrument()
        i.instrument(skip_dep_check=True)
        import mcp.client.streamable_http as module
        name = "streamable_http_client" if hasattr(module, "streamable_http_client") else "streamablehttp_client"
        async def check():
            async with getattr(module, name)("http://localhost:9/mcp") as streams:
                assert isinstance(streams[0], InstrumentedStreamReader)
                assert not isinstance(streams[0].__wrapped__, InstrumentedStreamReader)
        asyncio.run(check())
        i.uninstrument()
    ''')


def test_both_transport_aliases_are_wrapped_and_restored():
    from types import ModuleType
    from unittest.mock import patch
    from traceai_mcp import MCPInstrumentor

    module = ModuleType("dual_alias_fixture")
    def transport():
        pass
    module.streamable_http_client = transport
    module.streamablehttp_client = transport
    hooks = {}
    instrumentor = MCPInstrumentor()
    with patch.object(instrumentor, "_pending_hooks", set(), create=True), patch("traceai_mcp.register_post_import_hook", side_effect=lambda fn, name: hooks.setdefault(name, fn)):
        instrumentor._instrument()
        hooks["mcp.client.streamable_http"](module)
        assert module.streamable_http_client is not transport
        assert module.streamablehttp_client is not transport
        instrumentor._uninstrument()
        assert module.streamable_http_client is transport
        assert module.streamablehttp_client is transport
