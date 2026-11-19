"""Terminal completeness and qualified, observed streaming latency."""
import time

import pytest
from opentelemetry.trace import StatusCode
from traceai_hermes_agent._recorder import HermesSpanRecorder


@pytest.mark.parametrize("completed,failed,interrupted,outcome,status", [
    (False, False, False, "incomplete", StatusCode.UNSET),
    (None, False, False, "incomplete", StatusCode.UNSET),
    (True, False, False, "success", StatusCode.OK),
    (False, True, False, "error", StatusCode.ERROR),
    (False, False, True, "interrupted", StatusCode.ERROR),
])
@pytest.mark.parametrize("hide_errors", [False, True])
def test_terminal_outcome_and_partial_answer(provider, exporter, completed, failed, interrupted, outcome, status, hide_errors):
    recorder = HermesSpanRecorder(provider.get_tracer("test"), capture_content=True, hide_errors=hide_errors)
    base = dict(session_id="s", turn_id="t")
    recorder.on_pre_llm_call(**base, user_message="Try a tool")
    recorder.on_post_llm_call(**base, assistant_response="Partial answer")
    recorder.on_session_end(**base, completed=completed, failed=failed, interrupted=interrupted,
                            turn_exit_reason="max_iterations_reached(1/1)")
    root, = exporter.get_finished_spans()
    assert root.attributes["fi.agent.outcome"] == outcome
    assert root.status.status_code == status
    if outcome in {"success", "incomplete"}:
        assert root.attributes["output.value"] == "Partial answer"
    elif hide_errors:
        assert "output.value" not in root.attributes


def test_unfinished_shutdown_is_incomplete(provider, exporter):
    recorder = HermesSpanRecorder(provider.get_tracer("test"))
    recorder.on_pre_llm_call(session_id="s", turn_id="t")
    recorder.end_all()
    root, = exporter.get_finished_spans()
    assert root.attributes["fi.agent.outcome"] == "incomplete"
    assert root.status.status_code == StatusCode.UNSET


def attempt(provider, **extra):
    r = HermesSpanRecorder(provider.get_tracer("test"))
    base = dict(session_id="s", turn_id="t", api_request_id="r", api_call_count=1, model="m")
    start = time.time() - 1
    r.on_pre_api_request(**base, started_at=start, **extra)
    return r, base, start


def llm(exporter):
    return next(s for s in exporter.get_finished_spans() if s.name == "chat m")


def test_first_content_observer_is_canonical_ms_not_the_earlier_noncontent_chunk(provider, exporter, monkeypatch):
    r, base, start = attempt(provider)
    monkeypatch.setattr("traceai_hermes_agent._recorder.time.time", lambda: start + 0.6)
    r.on_stream_delta(session_id="s", turn_id="t", iteration=1, kind="text", delta="not retained")
    r.on_post_api_request(**base, first_chunk_at=start + 0.1, ended_at=start + 1)
    a = llm(exporter).attributes
    assert a["hermes.time_to_first_chunk_ms"] == 100
    assert a["gen_ai.server.time_to_first_token"] == 600
    assert a["hermes.first_chunk.source"] == "post_api_request.first_chunk_at"
    assert a["hermes.time_to_first_token.source"] == "on_stream_delta.text"
    assert a["hermes.time_to_first_token.timing"] == "callback-observed"
    assert "not retained" not in repr(a)


@pytest.mark.parametrize("event", [None, {"kind": "text", "delta": ""},
    {"kind": "reasoning", "delta": "private"}, {"kind": "text", "delta": "x", "iteration": 2}])
def test_no_qualified_content_observation_omits_canonical_latency(provider, exporter, event):
    r, base, start = attempt(provider)
    if event:
        r.on_stream_delta(**{**dict(session_id="s", turn_id="t", iteration=1), **event})
    r.on_post_api_request(**base, first_chunk_at=start + 0.1, ended_at=start + 1)
    a = llm(exporter).attributes
    assert a["hermes.time_to_first_chunk_ms"] == 100
    assert "gen_ai.server.time_to_first_token" not in a


@pytest.mark.parametrize("first", [None, True, float("nan"), float("inf"), -1, 1e20])
def test_missing_or_invalid_chunk_timing_is_not_a_measurement(provider, exporter, first):
    r, base, start = attempt(provider)
    r.on_post_api_request(**base, first_chunk_at=first, ended_at=start + 1)
    assert "hermes.time_to_first_chunk_ms" not in llm(exporter).attributes
    assert "gen_ai.server.time_to_first_token" not in llm(exporter).attributes


def test_retry_without_attempt_correlated_stream_event_omits_canonical(provider, exporter):
    r, base, start = attempt(provider, retry_count=1)
    r.on_stream_delta(session_id="s", turn_id="t", iteration=1, kind="text", delta="x")
    r.on_post_api_request(**base, first_chunk_at=time.time(), ended_at=time.time() + 1)
    assert "gen_ai.server.time_to_first_token" not in llm(exporter).attributes


def test_late_observer_cannot_attach_to_next_request(provider, exporter):
    r, base, start = attempt(provider)
    r.on_post_api_request(**base, first_chunk_at=start + 0.1, ended_at=start + 1)
    r.on_pre_api_request(**{**base, "api_request_id": "next", "api_call_count": 2}, started_at=time.time())
    r.on_stream_delta(session_id="s", turn_id="t", iteration=1, kind="text", delta="late")
    r.on_post_api_request(**{**base, "api_request_id": "next", "api_call_count": 2}, ended_at=time.time())
    assert all("gen_ai.server.time_to_first_token" not in s.attributes for s in exporter.get_finished_spans())
