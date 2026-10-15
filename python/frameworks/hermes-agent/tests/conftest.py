"""Shared fixtures: a recorder on an in-memory exporter, and Hermes-shaped hook payloads.

Payload field names follow the Hermes Agent hook reference
(https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks, read 2026-10-14)
and the call sites in hermes-agent v2026.9.24 (agent/turn_context.py,
agent/turn_api_request.py, agent/turn_response_intake.py, model_tools.py,
agent/turn_finalizer.py).
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from traceai_hermes_agent import register

SECRET = "FI-HERMES-TEST-MARKER-7f3a"
# A local path with a user name, as it appears in real exception text.
LOCAL_PATH = "/Users/alice/projects/acme/.env"


class FakeContext:
    """The part of Hermes' PluginContext that register() uses."""

    def __init__(self) -> None:
        self.hooks: Dict[str, List[Callable[..., Any]]] = {}

    def register_hook(self, name: str, callback: Callable[..., Any]) -> None:
        self.hooks.setdefault(name, []).append(callback)

    def fire(self, name: str, **kwargs: Any) -> List[Any]:
        # Hermes adds this envelope marker to every plugin-hook call.
        kwargs.setdefault("telemetry_schema_version", "hermes.observer.v1")
        return [cb(**kwargs) for cb in self.hooks.get(name, [])]


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def provider(exporter: InMemorySpanExporter) -> TracerProvider:
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    return tp


@pytest.fixture
def make_hermes(provider: TracerProvider, monkeypatch: pytest.MonkeyPatch):
    def _make(capture: bool = False) -> FakeContext:
        if capture:
            monkeypatch.setenv("FI_HERMES_CAPTURE_CONTENT", "true")
        else:
            monkeypatch.delenv("FI_HERMES_CAPTURE_CONTENT", raising=False)
        ctx = FakeContext()
        register(ctx, tracer_provider=provider)
        return ctx

    return _make


def run_tool_turn(ctx: FakeContext, *, session_id: str = "20261014_120000_abc123",
                  turn_id: str = "turn-1", tool_status: str = "ok",
                  fail_api: bool = False, turn_failed: bool = False) -> None:
    """One Hermes turn: model asks for read_file, tool runs, model answers."""
    now = time.time()
    base = dict(session_id=session_id, task_id="task-1", turn_id=turn_id, platform="cli")
    ctx.fire("pre_llm_call", **base, user_message="read the readme " + SECRET,
             conversation_history=[{"role": "user", "content": SECRET}], is_first_turn=True,
             model="gpt-6-luna", parent_session_id="", sender_id="")
    ctx.fire("pre_api_request", **base, api_request_id="req-1", user_message=SECRET,
             conversation_history=[], model="gpt-6-luna", provider="custom",
             base_url="http://user:pw@127.0.0.1:8317/v1", api_mode="chat_completions",
             api_call_count=1, retry_count=0,
             request_messages=[{"role": "user", "content": SECRET}],
             system_prompt="system " + SECRET, message_count=2, tool_count=12,
             approx_input_tokens=900, request_char_count=3600, max_tokens=None,
             started_at=now, middleware_trace=[],
             request={"messages": [{"role": "user", "content": SECRET}]})
    if fail_api:
        # agent/api_request_hooks.py sends error={"type", "message"}.
        ctx.fire("api_request_error", **base, api_request_id="req-1", model="gpt-6-luna",
                 provider="custom", base_url="http://127.0.0.1:8317/v1",
                 api_mode="chat_completions", api_call_count=1, api_duration=0.2,
                 started_at=now, ended_at=now + 0.2, status_code=429, retry_count=0,
                 max_retries=3, retryable=True, reason="rate_limit",
                 error={"type": "RateLimitError",
                        "message": "429 for prompt " + SECRET + " reading " + LOCAL_PATH},
                 request={})
    else:
        ctx.fire("post_api_request", **base, api_request_id="req-1", model="gpt-6-luna",
                 provider="custom", base_url="http://127.0.0.1:8317/v1",
                 api_mode="chat_completions", api_call_count=1, api_duration=0.5,
                 started_at=now, ended_at=now + 0.5, first_chunk_at=now + 0.1,
                 finish_reason="tool_calls", message_count=2, response_model="gpt-6-luna-2026",
                 response={"content": SECRET},
                 usage={"input_tokens": 800, "output_tokens": 20, "cache_read_tokens": 100,
                        "cache_write_tokens": 0, "reasoning_tokens": 5, "request_count": 1,
                        "prompt_tokens": 900, "total_tokens": 920},
                 assistant_message={"content": "", "tool_calls": [
                     {"function": {"name": "read_file", "arguments": '{"path": "' + SECRET + '"}'}}]},
                 assistant_content_chars=0, assistant_tool_call_count=1)
        error = {} if tool_status == "ok" else {"error_type": "FileNotFoundError",
                                                 "error_message": "missing " + SECRET + " " + LOCAL_PATH}
        ctx.fire("post_tool_call", **base, tool_name="read_file", args={"path": SECRET},
                 result="# Title " + SECRET, tool_call_id="call-1", api_request_id="req-1",
                 duration_ms=12, status=tool_status, middleware_trace=[],
                 **{"error_type": None, "error_message": None, **error})
    ctx.fire("post_llm_call", **base, user_message=SECRET, assistant_response="Title " + SECRET,
             conversation_history=[], model="gpt-6-luna")
    # agent/turn_loop_errors.py builds failed-turn reasons from str(e).
    failed_reason = ("repeated_outer_errors(Error during API call #3: [Errno 2] No such file "
                     "or directory: '" + LOCAL_PATH + "' " + SECRET + ")")
    ctx.fire("on_session_end", **base, completed=not turn_failed, failed=turn_failed,
             interrupted=False,
             turn_exit_reason=failed_reason if turn_failed else "text_response(finish_reason=stop)",
             model="gpt-6-luna")


def by_name(spans) -> Dict[str, Any]:
    return {s.name: s for s in spans}
