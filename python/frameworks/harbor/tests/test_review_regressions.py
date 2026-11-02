"""Independent reviewer regression probes against the installed distribution."""

import json
import shutil
import pytest
import harbor_atif2otel
from harbor.models.trajectories import Trajectory
from dataclasses import replace

from conftest import attrs, spans
from traceai_harbor import import_job


def valid_trajectory(trajectory):
    trajectory["steps"][1]["observation"] = trajectory["steps"][2].pop("observation")
    return trajectory


@pytest.mark.parametrize("version", ["ATIF-v1.2", "ATIF-v1.5", "ATIF-v1.6"])
def test_review_native_supported_harbor_schema(job, receiver, trajectory, version):
    trajectory = valid_trajectory(trajectory)
    trajectory["schema_version"] = version
    # Round-trip through Harbor's own schema serializer, then independently prove
    # the pinned upstream converter can handle it without a copied converter.
    native = Trajectory.model_validate(trajectory).to_json_dict()
    assert harbor_atif2otel.validate_trajectory(native) == []
    upstream = harbor_atif2otel.convert_trajectory(native)
    assert any(
        attrs(s).get("openinference.span.kind") == "LLM"
        for scope in upstream.scope_spans
        for s in scope.spans
    )
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(native))
    summary = import_job(job, settings=receiver.settings)
    assert summary.sent == 1
    assert any(attrs(s).get("fi.span.kind") == "LLM" for s in spans(receiver)), summary


def test_review_current_harbor_default_trajectory(job, receiver, trajectory):
    trajectory = valid_trajectory(trajectory)
    trajectory.pop("schema_version")
    native = Trajectory.model_validate(trajectory).to_json_dict()
    assert native["schema_version"] == "ATIF-v1.8"
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(native))
    summary = import_job(job, settings=receiver.settings)
    assert any(attrs(s).get("fi.span.kind") == "LLM" for s in spans(receiver)), summary


def test_review_accounting_total_tokens(job, receiver, trajectory):
    native = Trajectory.model_validate(valid_trajectory(trajectory)).to_json_dict()
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(native))
    assert import_job(job, settings=receiver.settings).sent == 1
    llm = [s for s in spans(receiver) if attrs(s).get("fi.span.kind") == "LLM"]
    assert len(llm) == 1
    values = attrs(llm[0])
    assert values["llm.token_count.prompt"] == 10
    assert values["llm.token_count.completion"] == 3
    assert values["llm.token_count.total"] == 13


def test_review_missing_completed_trial_directory(job, receiver):
    data = json.loads((job / "result.json").read_text())
    data["stats"] = {"n_completed_trials": 1}
    (job / "result.json").write_text(json.dumps(data))
    shutil.rmtree(job / "trial-1")
    summary = import_job(job, settings=receiver.settings)
    assert not summary.ok, summary


def test_review_observation_json_strings_redacted(job, receiver, trajectory):
    # Many real ATIF observations hold formatted output strings, not structured JSON.
    trajectory = valid_trajectory(trajectory)
    trajectory["steps"][1]["observation"]["results"][0]["content"] = (
        'Result: {"password": "PRIVATE-PASSWORD"}'
    )
    native = Trajectory.model_validate(trajectory).to_json_dict()
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(native))
    assert (
        import_job(job, settings=replace(receiver.settings, capture_content=True)).sent
        == 1
    )
    assert b"PRIVATE-PASSWORD" not in receiver.requests[0][2]


def test_review_tool_arguments_json_strings_redacted(job, receiver, trajectory):
    trajectory = valid_trajectory(trajectory)
    trajectory["steps"][1]["tool_calls"][0]["arguments"] = {
        "text": 'Command arguments: {"api_key": "PRIVATE-KEY"}'
    }
    native = Trajectory.model_validate(trajectory).to_json_dict()
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(native))
    assert (
        import_job(job, settings=replace(receiver.settings, capture_content=True)).sent
        == 1
    )
    assert b"PRIVATE-KEY" not in receiver.requests[0][2]


def test_review_real_harbor_hermes_serializer(job, receiver):
    from types import SimpleNamespace
    from harbor.agents.installed.hermes import Hermes

    agent = SimpleNamespace(version=lambda: "synthetic", model_name="synthetic-model")
    session = json.dumps(
        {
            "messages": [
                {"role": "user", "content": "hello"},
                {
                    "role": "assistant",
                    "content": "hello world",
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3},
                },
            ]
        }
    )
    native = Hermes._convert_hermes_session_to_atif(
        agent, session, "synthetic-session"
    ).to_json_dict()
    assert native["schema_version"] == "ATIF-v1.2"
    assert not harbor_atif2otel.validate_trajectory(native)
    assert any(
        attrs(s).get("openinference.span.kind") == "LLM"
        for scope in harbor_atif2otel.convert_trajectory(native).scope_spans
        for s in scope.spans
    )
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(native))
    summary = import_job(job, settings=receiver.settings)
    assert any(attrs(s).get("fi.span.kind") == "LLM" for s in spans(receiver)), summary


def test_current_schema_label_preserved(job, receiver, trajectory):
    trajectory = valid_trajectory(trajectory)
    trajectory["schema_version"] = "ATIF-v1.8"
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    assert import_job(job, settings=receiver.settings).sent == 1
    assert any(
        attrs(s).get("atif.schema_version") == "ATIF-v1.8" for s in spans(receiver)
    )


def test_audio_requires_converter_support(job, receiver, trajectory):
    trajectory = valid_trajectory(trajectory)
    trajectory["schema_version"] = "ATIF-v1.8"
    trajectory["steps"][0]["message"] = [
        {"type": "audio", "source": {"media_type": "audio/wav", "path": "sound.wav"}}
    ]
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    summary = import_job(job, settings=receiver.settings)
    assert summary.incomplete == 1
    assert not any(attrs(s).get("fi.span.kind") == "LLM" for s in spans(receiver))


def test_cancelled_unstarted_trial_does_not_count_as_missing(job, receiver):
    data = json.loads((job / "result.json").read_text())
    data["n_total_trials"] = 2
    data["stats"] = {"n_completed_trials": 1, "n_cancelled_trials": 1}
    (job / "result.json").write_text(json.dumps(data))
    summary = import_job(job, settings=receiver.settings)
    assert summary.ok and summary.sent == 1


def test_mixed_schema_embedded_subagent(job, receiver, trajectory):
    import copy

    trajectory = valid_trajectory(trajectory)
    subagent = copy.deepcopy(trajectory)
    subagent["trajectory_id"] = "child"
    subagent["schema_version"] = "ATIF-v1.8"
    trajectory["subagent_trajectories"] = [subagent]
    trajectory["steps"][1]["observation"]["results"][0]["subagent_trajectory_ref"] = [
        {"trajectory_id": "child"}
    ]
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    assert import_job(job, settings=receiver.settings).sent == 1
    assert sum(attrs(s).get("fi.span.kind") == "LLM" for s in spans(receiver)) == 2
