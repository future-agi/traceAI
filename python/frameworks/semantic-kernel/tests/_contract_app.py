"""Contract app: real Semantic Kernel + real OTel SDK + real Future AGI OTLP/HTTP exporter.

Run as a subprocess by ``test_contract_harness.py`` so Semantic Kernel's
process-wide diagnostics switches start from their defaults every time. The
parent points ``FI_BASE_URL`` at a loopback harness ``Receiver``;
``register()`` then exports OTLP/HTTP protobuf to
``{FI_BASE_URL}/tracer/v1/traces``. The model is a loopback fake
OpenAI-compatible server; placeholder keys only, no vendor call.

Usage: ``python _contract_app.py off|on|deadcollector``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _snapshot_processor():
    """Test-only: record each span's attributes before the mapping processor runs (AC-02)."""
    from opentelemetry.sdk.trace import SpanProcessor

    class SnapshotProcessor(SpanProcessor):
        def __init__(self) -> None:
            self.raw = {}

        def on_end(self, span):
            self.raw[format(span.context.span_id, "016x")] = dict(span.attributes or {})

    return SnapshotProcessor()


async def journeys(fake, out):
    from openai import AsyncOpenAI
    from semantic_kernel import Kernel
    from semantic_kernel.agents import ChatCompletionAgent
    from semantic_kernel.connectors.ai.function_choice_behavior import FunctionChoiceBehavior
    from semantic_kernel.connectors.ai.open_ai import OpenAIChatCompletion, OpenAIChatPromptExecutionSettings
    from semantic_kernel.contents import ChatHistory
    from semantic_kernel.functions import KernelArguments

    from fi_instrumentation import using_session
    from _scenarios import SECRET_PROMPT, SESSION, broken_plugin, weather_plugin

    client = AsyncOpenAI(api_key="placeholder-openai-key", base_url=fake.base_url, max_retries=0)
    chat = OpenAIChatCompletion(ai_model_id="gpt-4o-mini", async_client=client)
    auto = OpenAIChatPromptExecutionSettings(function_choice_behavior=FunctionChoiceBehavior.Auto())

    # 1. Plain chat completion through the connector.
    history = ChatHistory()
    history.add_user_message(SECRET_PROMPT)
    reply = await chat.get_chat_message_contents(history, OpenAIChatPromptExecutionSettings())
    out["chat_reply"] = str(reply[0].content)

    # 2. Kernel function that makes the model call one tool (auto function invocation).
    kernel = Kernel()
    kernel.add_service(chat)
    kernel.add_plugin(weather_plugin(), "Weather")
    result = await kernel.invoke_prompt(
        SECRET_PROMPT, function_name="ask_weather", plugin_name="Prompts", arguments=KernelArguments(settings=auto)
    )
    out["prompt_reply"] = str(result)

    # 3. ChatCompletionAgent invocation, inside an app-set session.
    agent = ChatCompletionAgent(
        kernel=kernel, name="Assistant", instructions="Answer briefly.", arguments=KernelArguments(settings=auto)
    )
    with using_session(SESSION):
        response = await agent.get_response(SECRET_PROMPT)
    out["agent_reply"] = str(response.message.content)
    out["agent_thread_id"] = str(response.thread.id)

    # 4. Streaming chat completion.
    stream_chat = OpenAIChatCompletion(ai_model_id="gpt-4o-mini-stream", async_client=client)
    chunks = []
    async for chunk in stream_chat.get_streaming_chat_message_contents(history, OpenAIChatPromptExecutionSettings()):
        chunks.extend(str(c.content or "") for c in chunk)
    out["stream_reply"] = "".join(chunks)

    # 5. Errors: model rejects (non-streaming and streaming); a tool raises.
    broken = OpenAIChatCompletion(ai_model_id="broken-model", async_client=client)
    errors = []
    try:
        await broken.get_chat_message_contents(history, OpenAIChatPromptExecutionSettings())
    except Exception as error:  # noqa: BLE001 - the error is the point
        errors.append(type(error).__name__)
    try:
        async for _ in broken.get_streaming_chat_message_contents(history, OpenAIChatPromptExecutionSettings()):
            pass
    except Exception as error:  # noqa: BLE001
        errors.append(type(error).__name__)
    out["model_errors"] = errors

    broken_kernel = Kernel()
    broken_kernel.add_service(chat)
    broken_kernel.add_plugin(broken_plugin(), "Broken")
    result = await broken_kernel.invoke_prompt(
        SECRET_PROMPT, function_name="ask_broken", plugin_name="Prompts", arguments=KernelArguments(settings=auto)
    )
    out["broken_tool_reply"] = str(result)


def main(argv):
    mode = argv[1] if len(argv) > 1 else "off"

    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType
    from opentelemetry import trace as trace_api

    from _fake_openai import FakeOpenAI
    from _scenarios import PROJECT_NAME, TOOL_SECRET_CITY
    from traceai_semantic_kernel import SemanticKernelInstrumentor

    assert not any(key.startswith("SEMANTICKERNEL_") for key in os.environ), "env flags must not be needed"

    out = {"mode": mode}
    out["global_provider_before"] = type(trace_api.get_tracer_provider()).__name__
    provider = register(project_type=ProjectType.OBSERVE, project_name=PROJECT_NAME, verbose=False)

    sensitive = mode == "on"
    SemanticKernelInstrumentor().instrument(tracer_provider=provider, sensitive=sensitive)
    SemanticKernelInstrumentor().instrument(tracer_provider=provider, sensitive=sensitive)  # AC-01: idempotent
    out["processors"] = [type(p).__name__ for p in provider._active_span_processor._span_processors]

    snapshot = _snapshot_processor()
    active = provider._active_span_processor
    with active._lock:
        active._span_processors = (snapshot,) + tuple(active._span_processors)

    with FakeOpenAI(tool_arguments={"city": TOOL_SECRET_CITY}) as fake:
        asyncio.run(journeys(fake, out))
        out["llm_requests"] = len(fake.requests)

    out["flushed"] = bool(provider.force_flush(timeout_millis=10000))
    out["global_provider_after"] = type(trace_api.get_tracer_provider()).__name__
    out["raw"] = snapshot.raw
    provider.shutdown()
    print("CONTRACT_RESULT " + json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
