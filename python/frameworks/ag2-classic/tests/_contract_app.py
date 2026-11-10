"""Contract app: real AG2 Classic + real OTel SDK + real Future AGI OTLP/HTTP exporter.

Run as a subprocess by ``test_contract_harness.py``. The parent points
``FI_BASE_URL`` at a loopback harness ``Receiver``; ``register()`` then exports
OTLP/HTTP protobuf to ``{FI_BASE_URL}/tracer/v1/traces``. The model is a
loopback fake. Usage: ``python _contract_app.py off|on``.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def main(argv: list) -> int:
    capture = len(argv) > 1 and argv[1] == "on"

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from opentelemetry import trace as trace_api

    from _fake_openai import FakeOpenAI
    from _scenarios import TOOL_SECRET_CITY, broken_tool_chat, failing_llm_chat, group_chat, two_agent_tool_chat
    from traceai_ag2_classic import setup

    global_before = type(trace_api.get_tracer_provider()).__name__
    provider = register(
        project_type=ProjectType.OBSERVE,
        project_name="ag2-classic-contract",
        verbose=False,
    )
    tracing = setup(tracer_provider=provider, capture_content=capture)

    out = {}
    with FakeOpenAI(tool_arguments={"city": TOOL_SECRET_CITY}) as fake:
        chat = two_agent_tool_chat(tracing, fake)
        out["chat_id"] = str(chat.chat_id)
        broken_tool_chat(tracing, fake)
        group = group_chat(tracing, fake)
        out["group_chat_id"] = str(group.chat_id)
        # The model rejects this one (HTTP 400): an LLM span that ends in an exception.
        out["llm_error"] = failing_llm_chat(tracing, fake)
        out["llm_requests"] = len(fake.requests)  # successful model calls only
        out["llm_failed_requests"] = len(fake.failed_requests)

    flushed = provider.force_flush(timeout_millis=10000)
    out["flushed"] = bool(flushed)
    out["global_provider_before"] = global_before
    out["global_provider_after"] = type(trace_api.get_tracer_provider()).__name__
    out["processors"] = [type(p).__name__ for p in provider._active_span_processor._span_processors]
    provider.shutdown()
    print("CONTRACT_RESULT " + json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
