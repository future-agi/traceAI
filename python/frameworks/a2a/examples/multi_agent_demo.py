"""
Multi-Agent Distributed Trace Demo
===================================

Demonstrates how traceai-a2a stitches two separate agent processes into
a single end-to-end trace in FutureAGI / any OTel backend.

Architecture:
    [Orchestrator Agent] ──A2A──→ [Specialist Agent]
          │                               │
          │  (A2A_CLIENT span)            │  (A2A_SERVER span — child of CLIENT)
          │                               │  (LLM span — child of SERVER)
          └── same trace_id ─────────────┘

The key insight: without traceai-a2a, these would be TWO separate traces
with no link between them. With traceai-a2a, you get ONE trace spanning
both agents.

Usage:
    python multi_agent_demo.py

Requirements:
    - FI_API_KEY and FI_SECRET_KEY set in environment (for FutureAGI backend)
    - OR any OTel-compatible backend configured

This demo works WITHOUT the real a2a-sdk by using mocks — perfect for
seeing the trace structure locally.
"""

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("a2a_demo")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Set up traceAI (works with or without real FI credentials)
# ─────────────────────────────────────────────────────────────────────────────

def setup_tracing(project_name: str = "multi_agent_demo"):
    """Configure the traceAI tracer provider."""
    try:
        from fi_instrumentation import register
        from fi_instrumentation.fi_types import ProjectType

        api_key = os.environ.get("FI_API_KEY", "demo-key")
        secret_key = os.environ.get("FI_SECRET_KEY", "demo-secret")
        os.environ.setdefault("FI_API_KEY", api_key)
        os.environ.setdefault("FI_SECRET_KEY", secret_key)
        os.environ.setdefault(
            "FI_BASE_URL", os.environ.get("FI_BASE_URL", "https://api.futureagi.com")
        )

        trace_provider = register(
            project_type=ProjectType.OBSERVE,
            project_name=project_name,
            verbose=True,
        )
        logger.info("✅ traceAI registered — project: %s", project_name)
        return trace_provider

    except Exception as exc:
        logger.warning("traceAI registration failed (%s) — using local console exporter", exc)
        return _fallback_provider()


def _fallback_provider():
    """Create a simple console-exporting tracer provider for local demo."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    provider._demo_exporter = exporter  # stash for later inspection
    return provider


# ─────────────────────────────────────────────────────────────────────────────
# 2. Mock A2A Client (simulates the real a2a-sdk A2AClient)
# ─────────────────────────────────────────────────────────────────────────────

class MockA2ATask:
    """Simulates an A2A SDK Task object returned by send_task()."""
    def __init__(self, task_id: str, state: str = "completed"):
        self.id = task_id
        class _Status:
            def __init__(self, state):
                self.state = state
        self.status = _Status(state)


class MockA2AArtifactEvent:
    """Simulates an SSE artifact event from send_task_streaming()."""
    def __init__(self, text: str):
        class _TextPart:
            type = "text"
            text_content = text
        class _Artifact:
            parts = [_TextPart()]
        self.artifact = _Artifact()


class MockA2AStatusEvent:
    """Simulates an SSE status event."""
    def __init__(self, state: str):
        class _Status:
            def __init__(self, s):
                self.state = s
        self.status = _Status(state)
        self.artifact = None


class MockA2AClient:
    """
    A lightweight mock of a2a.client.A2AClient.
    Does not make real HTTP calls — simulates the SDK API surface.

    In production, you would use a real A2AClient pointing at your agent's URL.
    """

    def __init__(self, url: str, **kwargs):
        self.url = url

    async def send_task(self, payload: Dict[str, Any], **kwargs) -> MockA2ATask:
        """Simulate a non-streaming A2A task call."""
        task_id = str(uuid.uuid4())
        logger.info(
            "  [MockA2AClient] send_task → agent=%s task_id=%s", self.url, task_id
        )

        # Simulate network latency
        await asyncio.sleep(0.1)

        # In reality, this is where the HTTP call goes out with traceparent header
        # The remote agent would receive it and create a child span
        received_headers = kwargs.get("headers", {})
        if "traceparent" in received_headers:
            logger.info(
                "  [MockA2AClient] ✅ traceparent propagated: %s",
                received_headers["traceparent"]
            )
        else:
            logger.warning("  [MockA2AClient] ⚠️  traceparent NOT in headers")

        return MockA2ATask(task_id=task_id, state="completed")

    async def send_task_streaming(self, payload: Dict[str, Any], **kwargs):
        """Simulate a streaming A2A task call that yields SSE events."""
        task_id = str(uuid.uuid4())
        logger.info(
            "  [MockA2AClient] send_task_streaming → agent=%s task_id=%s",
            self.url, task_id,
        )

        received_headers = kwargs.get("headers", {})
        if "traceparent" in received_headers:
            logger.info(
                "  [MockA2AClient] ✅ traceparent propagated: %s",
                received_headers["traceparent"]
            )

        await asyncio.sleep(0.05)
        yield MockA2AArtifactEvent("Here is a streaming chunk...")
        await asyncio.sleep(0.05)
        yield MockA2AArtifactEvent("And another chunk with the final answer.")
        await asyncio.sleep(0.05)
        yield MockA2AStatusEvent("completed")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Instrument and run the demo
# ─────────────────────────────────────────────────────────────────────────────

async def orchestrator_agent(task: str, trace_provider):
    """
    The orchestrator agent. It receives a user task, then delegates to
    a specialist ML agent via A2A — all within the same distributed trace.
    """
    from opentelemetry import trace

    tracer = trace_provider.get_tracer("orchestrator_agent")

    with tracer.start_as_current_span("Orchestrator.handle_user_task") as span:
        span.set_attribute("user.task", task)
        logger.info("🎯 Orchestrator received task: %s", task)

        # ── Non-streaming call ──────────────────────────────────────
        logger.info("\n── Non-streaming A2A call ──────────────────────────────")
        client = MockA2AClient(url="http://specialist-agent:8080")

        result = await client.send_task(
            payload={
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": task}],
                }
            }
        )
        logger.info("  Orchestrator got result: task_id=%s state=%s", result.id, result.status.state)

        # ── Streaming call ──────────────────────────────────────────
        logger.info("\n── Streaming A2A call ──────────────────────────────────")
        client2 = MockA2AClient(url="http://summarizer-agent:8080")

        chunks = []
        async for event in client2.send_task_streaming(
            payload={
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": f"Summarize: {task}"}],
                }
            }
        ):
            artifact = getattr(event, "artifact", None)
            if artifact:
                for part in artifact.parts:
                    chunks.append(getattr(part, "text_content", ""))

        final_answer = " ".join(chunks)
        logger.info("  Streaming result: %r", final_answer[:80])
        span.set_attribute("output.value", final_answer)

    return final_answer


async def main():
    print("\n" + "═" * 60)
    print("  traceai-a2a: Multi-Agent Distributed Trace Demo")
    print("═" * 60 + "\n")

    # Set up tracing
    trace_provider = setup_tracing("multi_agent_demo")

    # Instrument A2A — this is the one-liner that enables distributed tracing
    from traceai_a2a import A2AInstrumentor

    # We patch MockA2AClient into the a2a module namespace for demo purposes
    import sys
    import types
    mock_a2a_module = types.ModuleType("a2a")
    mock_client_module = types.ModuleType("a2a.client")
    mock_client_module.A2AClient = MockA2AClient
    mock_a2a_module.client = mock_client_module
    mock_a2a_module.A2AClient = MockA2AClient
    sys.modules["a2a"] = mock_a2a_module
    sys.modules["a2a.client"] = mock_client_module

    instrumentor = A2AInstrumentor()
    instrumentor.instrument(tracer_provider=trace_provider)
    logger.info("✅ A2AInstrumentor active — W3C context propagation enabled\n")

    # Run the orchestrator
    result = await orchestrator_agent(
        task="Analyze the latest quarterly earnings and provide investment recommendations",
        trace_provider=trace_provider,
    )

    # Show exported spans if using fallback provider
    if hasattr(trace_provider, "_demo_exporter"):
        spans = trace_provider._demo_exporter.get_finished_spans()
        print("\n" + "─" * 60)
        print(f"  📊 Captured {len(spans)} spans locally:")
        print("─" * 60)
        for s in spans:
            attrs = dict(s.attributes or {})
            print(f"\n  [{s.name}]")
            for k, v in sorted(attrs.items()):
                if k.startswith("gen_ai.a2a") or k in ("output.value", "user.task"):
                    print(f"    {k} = {v!r}")

    # Uninstrument cleanly
    instrumentor.uninstrument()
    logger.info("\n✅ A2AInstrumentor deactivated — all patches removed")

    print("\n" + "═" * 60)
    print("  Demo complete! Check your FutureAGI dashboard for the trace.")
    print("  Both agent spans should appear under the SAME trace ID.")
    print("═" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
