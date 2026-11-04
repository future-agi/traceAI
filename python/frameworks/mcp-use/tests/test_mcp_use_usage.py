"""Cache cost inputs survive the real agent loop and OTLP HTTP export."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict

import pytest
from _mcp_use_support import ANSWER, PROMPT, ChatFake, add_script, drive, mcp_client
from fi_instrumentation import TraceConfig, register
from fi_instrumentation.fi_types import ProjectType
from mcp_use import MCPAgent

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

from harness import Receiver, _flatten_attributes  # noqa: E402
from traceai_mcp_use import FutureAGICallback  # noqa: E402


class CachedModel(ChatFake):
    cache_details: Dict[str, Any]

    def _next(self, messages):
        details = {"cache_read": 10, **self.cache_details}
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
@pytest.mark.parametrize("details,writes,short,long", [
    pytest.param({}, None, None, None, id="absent"),
    pytest.param({"cache_creation": 0}, 0, None, None, id="zero"),
    pytest.param({"cache_creation": 20}, 20, None, None, id="generic"),
    pytest.param({"cache_creation": 0, "ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 20}, 50, 30, 20, id="split"),
    pytest.param({"cache_creation": 50, "ephemeral_5m_input_tokens": 30, "ephemeral_1h_input_tokens": 20}, 50, 30, 20, id="no-double-count"),
    pytest.param({"ephemeral_5m_input_tokens": 30}, 30, 30, None, id="5m-only"),
    pytest.param({"ephemeral_1h_input_tokens": 20}, 20, None, 20, id="1h-only"),
    pytest.param({"cache_creation": 50, "ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}, 0, 0, 0, id="zero-tiers-win"),
])
def test_cache_creation_reaches_the_collector(monkeypatch, method, hide_inputs, hide_outputs, details, writes, short, long):
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
                    llm=CachedModel(script=add_script(), cache_details=details, streaming=method != "run"),
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
        for key, expected in (
            ("gen_ai.usage.cache_write_tokens", writes),
            ("mcp_use.llm.cache_creation.ephemeral_5m_input_tokens", short),
            ("mcp_use.llm.cache_creation.ephemeral_1h_input_tokens", long),
        ):
            if expected is None:
                assert key not in span
            else:
                assert int(span[key]) == expected
    assert llms[0]["input.value"] == ("__REDACTED__" if hide_inputs else PROMPT)
    assert llms[-1]["output.value"] == ("__REDACTED__" if hide_outputs else ANSWER)
