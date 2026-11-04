import copy
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import harbor_atif2otel
import pytest
from conftest import attrs, exception, modify_result, spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from traceai_harbor import Settings, import_job
from traceai_harbor._artifacts import ArtifactError, parse


def test_native_wire_privacy_and_accounting(job, receiver, monkeypatch):
    original = harbor_atif2otel.convert_trajectory
    calls = []

    def record(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(harbor_atif2otel, "convert_trajectory", record)
    summary = import_job(job, settings=receiver.settings)
    assert summary.sent == 1 and summary.ok and not summary.incomplete
    assert len(calls) == 1
    path, headers, body = receiver.requests[0]
    assert path == "/tracer/v1/traces"
    headers = {k.lower(): v for k, v in headers.items()}
    assert headers["fi_api_key"] == receiver.settings.api_key
    assert headers["fi_secret_key"] == receiver.settings.secret_key
    assert headers["content-type"] == "application/x-protobuf"
    rs = ExportTraceServiceRequest.FromString(body).resource_spans[0]
    assert attrs(rs.resource)["project_name"] == "synthetic-harbor"
    assert attrs(rs.resource)["project_type"] == "observe"
    tree = spans(receiver)
    ids = {s.span_id for s in tree}
    assert len(ids) == len(tree)
    assert len({s.trace_id for s in tree}) == 1
    assert all(len(s.span_id) == 8 and len(s.trace_id) == 16 for s in tree)
    assert all(not s.parent_span_id or s.parent_span_id in ids for s in tree)
    assert sum(not s.parent_span_id for s in tree) == 1
    assert {attrs(s)["fi.span.kind"] for s in tree} >= {"CHAIN", "AGENT", "LLM", "TOOL"}
    assert all(
        "input.value" not in attrs(s) and "output.value" not in attrs(s) for s in tree
    )
    assert sum(attrs(s).get("llm.token_count.prompt", 0) for s in tree) == 10
    assert (
        sum(attrs(s).get("llm.token_count.prompt_details.cache_read", 0) for s in tree)
        == 4
    )
    assert sum(attrs(s).get("llm.cost.total", 0) for s in tree) == 0.01
    root = attrs(tree[0])
    assert root["harbor.reward.reward"] == 0
    assert root["harbor.reward_valid"] is True
    assert root["harbor.summary.input_tokens"] == 10
    assert tree[0].status.code == 0
    assert all(attrs(s).get("session.id") == root["harbor.trial.id"] for s in tree)


def test_replay_move_destination_conflict(job, receiver, tmp_path):
    assert import_job(job, settings=receiver.settings).sent == 1
    assert import_job(job, settings=receiver.settings).skipped == 1
    moved = tmp_path / "moved"
    shutil.move(job, moved)
    assert import_job(moved, settings=receiver.settings).skipped == 1
    assert len(receiver.requests) == 1
    alternate = replace(receiver.settings, project_name="different")
    assert import_job(moved, settings=alternate).sent == 1
    modify_result(moved, verifier_result={"rewards": {"reward": 1}})
    assert import_job(moved, settings=receiver.settings).conflicts == 1
    assert len(receiver.requests) == 2
    state = "".join(p.read_text() for p in (moved / ".traceai-harbor").rglob("*.json"))
    assert receiver.settings.api_key not in state and "reward" not in state


def test_concurrent_imports(job, receiver):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: import_job(job, settings=receiver.settings), range(4))
        )
    assert sum(r.sent for r in results) == 1
    assert sum(r.skipped for r in results) == 3
    assert len(receiver.requests) == 1


def test_equal_basenames_different_jobs(job, receiver, tmp_path):
    other = tmp_path / "other" / "job"
    shutil.copytree(job, other)
    data = json.loads((other / "result.json").read_text())
    data["id"] = str(uuid4())
    (other / "result.json").write_text(json.dumps(data))
    assert import_job(job, settings=receiver.settings).sent == 1
    assert import_job(other, settings=receiver.settings).sent == 1
    requests = [ExportTraceServiceRequest.FromString(r[2]) for r in receiver.requests]
    assert (
        requests[0].resource_spans[0].scope_spans[0].spans[0].trace_id
        != requests[1].resource_spans[0].scope_spans[0].spans[0].trace_id
    )


@pytest.mark.parametrize(
    "kind,valid,cause",
    [
        ("AgentTimeoutError", True, "agent_exhausted_budget"),
        ("CancelledError", False, "cancelled"),
        ("SandboxBuildFailedError", False, "sandbox_failed"),
        ("VerifierTimeoutError", False, "verifier_failed"),
    ],
)
def test_failure_semantics(job, receiver, kind, valid, cause):
    modify_result(job, exception_info=exception(kind))
    result = import_job(job, settings=receiver.settings)
    assert result.ok
    root = spans(receiver)[0]
    assert attrs(root)["harbor.reward_valid"] is valid
    assert attrs(root)["harbor.failure_cause"] == cause
    assert root.status.code == 2
    assert b"PRIVATE" not in receiver.requests[0][2]


def test_no_trajectory_or_verifier(job, receiver):
    (job / "trial-1" / "agent" / "trajectory.json").unlink()
    modify_result(job, verifier_result=None)
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.incomplete == 1
    assert attrs(spans(receiver)[0])["harbor.reward_valid"] is False
    assert "harbor.reward.reward" not in attrs(spans(receiver)[0])


@pytest.mark.parametrize(
    "mutation", ["invalid", "large", "depth", "remote", "symlink", "timestamp"]
)
def test_untrusted_trajectories(job, receiver, mutation, tmp_path, trajectory):
    file = job / "trial-1" / "agent" / "trajectory.json"
    if mutation == "invalid":
        file.write_text("{")
    elif mutation == "large":
        file.write_bytes(b" " * (8 * 1024 * 1024 + 1))
    elif mutation == "depth":
        file.write_text("[" * 1000 + "0" + "]" * 1000)
    elif mutation == "remote":
        trajectory["continued_trajectory_ref"] = "https://invalid.example/private.json"
        file.write_text(json.dumps(trajectory))
    elif mutation == "symlink":
        outside = tmp_path / "private.json"
        outside.write_text('"NEVER_READ_THIS"')
        file.unlink()
        file.symlink_to(outside)
    else:
        trajectory["steps"][0]["timestamp"] = "not-a-time"
        file.write_text(json.dumps(trajectory))
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.incomplete == 1
    assert b"NEVER_READ_THIS" not in receiver.requests[0][2]


def test_trial_symlink_and_traversal_rejected(job, receiver, tmp_path):
    target = tmp_path / "outside"
    shutil.move(job / "trial-1", target)
    (job / "trial-1").symlink_to(target, target_is_directory=True)
    assert not import_job(job, settings=receiver.settings).ok
    assert receiver.requests == []
    (job / "trial-1").unlink()
    shutil.move(target, job / "trial-1")
    modify_result(job, step_results=[{"step_name": "../../outside"}])
    assert not import_job(job, settings=receiver.settings).ok
    assert receiver.requests == []


@pytest.mark.parametrize("code", [400, 401, 403, 301, 302, 307, 308])
def test_permanent_and_redirect_failures(job, receiver, code):
    receiver.responses = [
        (code, b"PRIVATE SERVER ERROR", {"Location": receiver.endpoint + "/forbidden"})
    ]
    assert import_job(job, settings=receiver.settings).failed == 1
    assert len(receiver.requests) == 1
    assert import_job(job, settings=receiver.settings).sent == 1


@pytest.mark.parametrize("code", [429, 502, 503, 504])
def test_transient_retry(job, receiver, code):
    receiver.responses = [(code, b"", {})] * 2
    assert import_job(job, settings=receiver.settings).sent == 1
    assert len(receiver.requests) == 3


@pytest.mark.parametrize(
    "response",
    [
        b"not protobuf",
        ExportTraceServiceResponse(
            partial_success={"rejected_spans": 1, "error_message": "PRIVATE"}
        ).SerializeToString(),
        b"x" * 65537,
    ],
)
def test_invalid_or_partial_ack(job, receiver, response):
    receiver.responses = [(200, response, {})]
    assert import_job(job, settings=receiver.settings).failed == 1
    assert import_job(job, settings=receiver.settings).sent == 1


def test_privacy_canaries(job, receiver, trajectory):
    secret = receiver.settings.secret_key
    trajectory["metadata"] = {"anything": "PRIVATE-METADATA"}
    trajectory["steps"][0]["message"] = f"prompt {secret}"
    trajectory["steps"][1]["message"] = f"response {secret}"
    trajectory["steps"][1]["reasoning_content"] = "PRIVATE-REASONING"
    trajectory["steps"][1]["tool_calls"][0]["arguments"] = {
        "password": "PRIVATE-PASSWORD",
        "nested": {"api_key": "PRIVATE-KEY"},
        "safe": "visible",
    }
    trajectory["steps"][2]["observation"]["results"][0]["content"] = f"result {secret}"
    trajectory["tool_definitions"] = [{"secret": "PRIVATE-SCHEMA"}]
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    assert import_job(job, settings=receiver.settings).ok
    assert (
        b"PRIVATE" not in receiver.requests[-1][2]
        and secret.encode() not in receiver.requests[-1][2]
    )
    assert import_job(job, settings=replace(receiver.settings, capture_content=True)).ok
    assert (
        b"PRIVATE" not in receiver.requests[-1][2]
        and secret.encode() not in receiver.requests[-1][2]
    )
    assert b"visible" in receiver.requests[-1][2]


def test_multistep(job, receiver, trajectory):
    timing = {
        "started_at": "2026-01-01T00:00:02Z",
        "finished_at": "2026-01-01T00:00:05Z",
    }
    steps = [
        {
            "step_name": name,
            "agent_execution": timing,
            "verifier_result": {"rewards": {"reward": i}},
            "agent_result": {"n_input_tokens": 10},
        }
        for i, name in enumerate(["first", "second"])
    ]
    modify_result(job, step_results=steps, agent_result=None, agent_execution=None)
    for step in steps:
        directory = job / "trial-1" / "steps" / step["step_name"] / "agent"
        directory.mkdir(parents=True)
        (directory / "trajectory.json").write_text(json.dumps(trajectory))
    assert import_job(job, settings=receiver.settings).sent == 1
    tree = spans(receiver)
    assert sum(attrs(s).get("llm.token_count.prompt", 0) for s in tree) == 20
    assert attrs(tree[0])["harbor.summary.input_tokens"] == 20
    assert {attrs(s).get("harbor.step.name") for s in tree} >= {"first", "second"}


def test_multi_turn_subagent_aggregate_copied(job, receiver, trajectory):
    sub = copy.deepcopy(trajectory)
    sub["trajectory_id"] = "sub-1"
    trajectory["subagent_trajectories"] = [sub]
    trajectory["steps"][2]["observation"]["results"][0]["subagent_trajectory_ref"] = [
        {"trajectory_id": "sub-1"},
        {"trajectory_id": "external"},
    ]
    trajectory["steps"][1]["llm_call_count"] = 2
    trajectory["steps"].extend(
        [
            {
                "step_id": 4,
                "source": "user",
                "message": "again",
                "timestamp": "2026-01-01T00:00:05Z",
            },
            {
                "step_id": 5,
                "source": "agent",
                "message": "copied",
                "is_copied_context": True,
                "timestamp": "2026-01-01T00:00:06Z",
                "metrics": {"prompt_tokens": 100000},
            },
            {
                "step_id": 6,
                "source": "agent",
                "message": "done",
                "timestamp": "2026-01-01T00:00:07Z",
            },
        ]
    )
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    result = import_job(job, settings=receiver.settings)
    assert result.sent == 1 and result.incomplete == 1
    tree = spans(receiver)
    assert sum(attrs(s).get("llm.token_count.prompt", 0) for s in tree) == 20
    assert any(attrs(s).get("metadata.aggregated_llm_calls") == 2 for s in tree)
    ids = {s.span_id for s in tree}
    assert all(not s.parent_span_id or s.parent_span_id in ids for s in tree)


def test_missing_timing_labeled(job, receiver, trajectory):
    for step in trajectory["steps"]:
        step.pop("timestamp")
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    assert import_job(job, settings=receiver.settings).ok
    assert all(
        attrs(s).get("harbor.timing.fallback") == "trial_bounds"
        for s in spans(receiver)
        if s.name.startswith("ATIF")
    )


def test_bad_config():
    for endpoint in [
        "http://example.com",
        "https://user:password@example.com",
        "https://example.com/?secret=yes",
        "https://example.com/#secret",
    ]:
        with pytest.raises(ValueError):
            Settings(endpoint=endpoint, api_key="synthetic", secret_key="synthetic")
    with pytest.raises(ValueError):
        Settings()


def test_bounded_parser():
    for data in [
        b'{"a":NaN}',
        b"[" * 1000 + b"0" + b"]" * 1000,
        json.dumps([0] * 50001).encode(),
    ]:
        with pytest.raises(ArtifactError):
            parse(data)
