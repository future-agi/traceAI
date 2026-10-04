"""Shared-harness contract tests (TH-8339 Receiver).

Real path: AG2 ``Agent`` + AG2 ``TelemetryMiddleware`` (attached by
``traceai_ag2.setup``) -> ``AG2SpanProcessor`` -> ``fi_instrumentation``
``BatchSpanProcessor`` -> real OTLP/HTTP protobuf exporter -> loopback
``harness.Receiver``. The model is AG2's scripted ``TestConfig``; nothing
leaves 127.0.0.1 and no vendor API is called.

The Receiver drops resource attributes and headers; those are asserted in
``test_resource_headers.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_PYTHON_DIR = Path(__file__).resolve().parents[3]
_PACKAGE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PYTHON_DIR / "tests"))

from harness import Receiver, run  # noqa: E402

from ._support import (  # noqa: E402
    MODEL,
    PROVIDER,
    TOOL_CALL_ID,
    ask,
    assert_no_content,
    expected_one_tool_kinds,
    otlp_attrs,
    weather_agent,
)

_STATUS_ERROR = {"STATUS_CODE_ERROR", 2}


@pytest.fixture()
def fi_env(monkeypatch):
    def _apply(origin: str) -> None:
        monkeypatch.setenv("FI_BASE_URL", origin)
        monkeypatch.setenv("FI_API_KEY", "placeholder-api-key")
        monkeypatch.setenv("FI_SECRET_KEY", "placeholder-secret-key")
        for name in ("FI_HIDE_INPUTS", "FI_HIDE_OUTPUTS"):
            monkeypatch.delenv(name, raising=False)

    return _apply


def test_one_tool_run_reaches_collector_path_with_normalized_gen_ai_spans(fi_env):
    from fi_instrumentation import register
    from fi_instrumentation.fi_types import ProjectType

    from traceai_ag2 import setup

    with Receiver() as receiver:
        fi_env(receiver.origin)
        trace_provider = register(
            project_type=ProjectType.OBSERVE,
            project_name="ag2-contract",
            verbose=False,
        )
        try:
            agent = weather_agent()
            setup(agent, tracer_provider=trace_provider)  # capture_content defaults off
            reply = ask(agent)
            assert reply.body
            assert trace_provider.force_flush(timeout_millis=10_000)
            spans = receiver.spans()
        finally:
            trace_provider.shutdown()

    by_name = {s["name"]: otlp_attrs(s) for s in spans}
    expected_kinds = expected_one_tool_kinds()
    assert sorted(by_name) == sorted(expected_kinds), sorted(by_name)

    # span kind, from gen_ai.operation.name
    assert {n: a.get("gen_ai.span.kind") for n, a in by_name.items()} == expected_kinds
    assert by_name["invoke_agent weather_bot"]["gen_ai.operation.name"] == "invoke_agent"
    assert by_name["invoke_agent weather_bot"]["gen_ai.agent.name"] == "weather_bot"
    assert by_name["execute_tool get_weather"]["gen_ai.tool.name"] == "get_weather"
    assert by_name["execute_tool get_weather"]["gen_ai.tool.call.id"] == TOOL_CALL_ID

    # model, provider, tokens (originals and dotted aliases)
    chat = by_name[f"chat {MODEL}"]
    assert chat["gen_ai.request.model"] == MODEL
    assert chat["gen_ai.response.model"] == MODEL
    assert chat["gen_ai.provider.name"] == PROVIDER
    assert chat["gen_ai.response.finish_reasons"] == ["stop"]
    expected_usage = {
        "gen_ai.usage.input_tokens": 11,
        "gen_ai.usage.output_tokens": 7,
        "gen_ai.usage.cache_creation_input_tokens": 2,
        "gen_ai.usage.cache_creation.input_tokens": 2,
        "gen_ai.usage.cache_read_input_tokens": 3,
        "gen_ai.usage.cache_read.input_tokens": 3,
        "gen_ai.usage.thinking_tokens": 5,
        "gen_ai.usage.reasoning.output_tokens": 5,
    }
    usage_spans = [chat]
    if "record_usage model_call" in by_name:  # ag2 >= 1.0.3
        usage_spans.append(by_name["record_usage model_call"])
    for source in usage_spans:
        assert {k: source.get(k) for k in expected_usage} == expected_usage

    # session: AG2 emits none; content: capture is off
    for name, attributes in by_name.items():
        assert "session.id" not in attributes, name
        assert_no_content(attributes, name)

    for span in spans:
        assert span.get("status", {}).get("code") not in _STATUS_ERROR, span["name"]


def test_short_script_flushes_spans_before_exit():
    """The offline example, run as a separate process via harness.run."""
    with Receiver() as receiver:
        env = dict(os.environ)
        env.update(
            {
                "FI_BASE_URL": receiver.origin,
                "FI_API_KEY": "placeholder-api-key",
                "FI_SECRET_KEY": "placeholder-secret-key",
                "FI_PROJECT_NAME": "ag2-contract-script",
                "PYTHONPATH": os.pathsep.join(
                    [str(_PACKAGE_DIR), str(_PYTHON_DIR), env.get("PYTHONPATH", "")]
                ),
            }
        )
        result = run(
            [sys.executable, str(_PACKAGE_DIR / "examples" / "offline_weather_agent.py")],
            env=env,
            stdin=None,
            timeout=120,
        )
        spans = receiver.spans()

    assert not result.timed_out, result.stderr.decode(errors="replace")
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert b"It is sunny in Paris." in result.stdout
    by_name = {s["name"]: otlp_attrs(s) for s in spans}
    assert by_name["invoke_agent weather_bot"]["gen_ai.span.kind"] == "AGENT"
    assert by_name["execute_tool get_weather"]["gen_ai.span.kind"] == "TOOL"
    assert by_name["chat fake-model"]["gen_ai.span.kind"] == "LLM"
    assert by_name["chat fake-model"]["gen_ai.usage.cache_read.input_tokens"] == 3
    for name, attributes in by_name.items():
        assert_no_content(attributes, name)
