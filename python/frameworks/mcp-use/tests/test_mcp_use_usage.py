"""Cache cost inputs survive the real agent loop and OTLP HTTP export."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import pytest
from _mcp_use_support import ANSWER, PROMPT, ChatFake, add_script, drive, mcp_client
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from mcp_use import MCPAgent

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402
from traceai_mcp_use import FutureAGICallback  # noqa: E402


class CachedModel(ChatFake):
    cache_creation: Optional[int] = None

    def _next(self, messages):
        details = {"cache_read": 10}
        if self.cache_creation is not None:
            details["cache_creation"] = self.cache_creation
        return super()._next(messages).model_copy(
            update={
                "usage_metadata": {
                    "input_tokens": 100,
                    "output_tokens": 7,
                    "total_tokens": 107,
                    "input_token_details": details,
                }
            }
        )


@pytest.mark.parametrize("method", ["run", "stream", "stream_events"])
@pytest.mark.parametrize("hide_inputs,hide_outputs", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("cache_creation", [None, 0, 20], ids=["absent", "zero", "written"])
def test_cache_creation_reaches_the_collector(monkeypatch, method, hide_inputs, hide_outputs, cache_creation):
    with Receiver() as receiver:
        monkeypatch.setenv("FI_BASE_URL", receiver.origin)
        monkeypatch.setenv("FI_API_KEY", "placeholder-fi-api-key")
        monkeypatch.setenv("FI_SECRET_KEY", "placeholder-fi-secret-key")
        provider = register(project_type=ProjectType.OBSERVE, project_name="mcp-use-usage", verbose=False)
        handler = FutureAGICallback(
            tracer_provider=provider,
            config=TraceConfig(hide_inputs=hide_inputs, hide_outputs=hide_outputs),
        )

        async def run():
            client = mcp_client()
            try:
                agent = MCPAgent(
                    llm=CachedModel(script=add_script(), cache_creation=cache_creation, streaming=method != "run"),
                    client=client,
                    callbacks=[handler],
                )
                await drive(agent, method)
            finally:
                await client.close_all_sessions()

        try:
            asyncio.run(run())
            assert not handler._runs
            assert provider.force_flush(timeout_millis=10_000)
        finally:
            provider.shutdown()
        spans = [_flatten_attributes(span.get("attributes", [])) for span in receiver.spans()]

    assert len(spans) == 4  # agent, LLM, tool, LLM
    llms = [span for span in spans if span.get("gen_ai.span.kind") == "LLM"]
    assert len(llms) == 2
    for span in llms:
        assert int(span["gen_ai.usage.input_tokens"]) == 100
        assert int(span["gen_ai.usage.output_tokens"]) == 7
        # Cache details are subsets of input usage, not extra total tokens.
        assert int(span["gen_ai.usage.total_tokens"]) == 107
        assert int(span["gen_ai.usage.cache_read_tokens"]) == 10
        if cache_creation is None:
            assert "gen_ai.usage.cache_write_tokens" not in span
        else:
            assert int(span["gen_ai.usage.cache_write_tokens"]) == cache_creation
    assert llms[0]["input.value"] == ("__REDACTED__" if hide_inputs else PROMPT)
    assert llms[-1]["output.value"] == ("__REDACTED__" if hide_outputs else ANSWER)
