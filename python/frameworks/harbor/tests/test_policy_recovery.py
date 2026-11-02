"""Regressions for host privacy policy and recoverable incomplete exports."""

import json
from dataclasses import replace

import pytest
from conftest import attrs, modify_result, spans
from traceai_harbor import Settings, import_job
from traceai_harbor._privacy import Privacy


@pytest.mark.parametrize("direction", ["INPUT", "OUTPUT"])
@pytest.mark.parametrize("flag_suffix", ["S", "_TEXT", "_MESSAGES"])
def test_hide_flags_block_wire_content_and_isolate_acks(
    job, receiver, trajectory, monkeypatch, direction, flag_suffix
):
    trajectory["steps"][1]["observation"] = trajectory["steps"][2].pop("observation")
    trajectory["steps"][0]["message"] = "SYNTHETIC-INPUT-CANARY"
    trajectory["steps"][2]["message"] = "SYNTHETIC-OUTPUT-CANARY"
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    settings = replace(receiver.settings, capture_content=True)
    assert import_job(job, settings=settings).sent == 1
    monkeypatch.setenv("FI_API_KEY", settings.api_key)
    monkeypatch.setenv("FI_SECRET_KEY", settings.secret_key)
    monkeypatch.setenv("FI_HARBOR_ENDPOINT", settings.endpoint)
    monkeypatch.setenv(f"FI_HIDE_{direction}{flag_suffix}", "TrUe")
    hidden = Settings.from_env(project_name=settings.project_name, capture_content=True)
    assert hidden.destination != settings.destination
    assert import_job(job, settings=hidden).sent == 1
    exported = receiver.requests[-1][2]
    assert f"SYNTHETIC-{direction}-CANARY".encode() not in exported
    other = "OUTPUT" if direction == "INPUT" else "INPUT"
    assert f"SYNTHETIC-{other}-CANARY".encode() in exported
    assert import_job(job, settings=hidden).skipped == 1


def test_token_credentials_redacted_in_nested_tool_arguments(job, receiver, trajectory):
    trajectory["steps"][1]["tool_calls"][0]["arguments"] = {
        "refresh_tokens": "SYNTHETIC-REFRESH-CREDENTIAL",
        "api_tokens": ["SYNTHETIC-API-CREDENTIAL"],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    settings = replace(receiver.settings, capture_content=True)
    assert (
        Privacy(settings).tree({"refresh_tokens": "SYNTHETIC-REFRESH-CREDENTIAL"})[
            "refresh_tokens"
        ]
        == "[redacted]"
    )
    assert import_job(job, settings=settings).sent == 1
    body = receiver.requests[-1][2]
    assert b"SYNTHETIC-REFRESH-CREDENTIAL" not in body
    assert b"SYNTHETIC-API-CREDENTIAL" not in body
    assert any("prompt_tokens" in str(attrs(span)) for span in spans(receiver))
    privacy = Privacy(settings)
    assert "SYNTHETIC-REFRESH-CREDENTIAL" not in privacy.text(
        'Tool args: {"refresh_tokens": "SYNTHETIC-REFRESH-CREDENTIAL"}'
    )
    assert privacy.tree({"prompt_tokens": 12}) == {"prompt_tokens": 12}
    assert privacy.tree({"prompt_tokens": "SYNTHETIC-REFRESH-CREDENTIAL"}) == {
        "prompt_tokens": "[redacted]"
    }


def test_incomplete_then_recovered_sends_only_new_spans(job, receiver, trajectory):
    path = job / "trial-1" / "agent" / "trajectory.json"
    path.unlink()
    first = import_job(job, settings=receiver.settings)
    assert first.sent == 1 and first.incomplete == 1
    existing = {s.span_id for s in spans(receiver)}
    assert import_job(job, settings=receiver.settings).skipped == 1
    path.write_text(json.dumps(trajectory))
    second = import_job(job, settings=receiver.settings)
    assert second.sent == 1 and second.conflicts == 0 and second.incomplete == 0
    new = [s.span_id for s in spans(receiver)[len(existing) :]]
    assert new and not existing.intersection(new)
    assert import_job(job, settings=receiver.settings).skipped == 1
    assert len(receiver.requests) == 2


def test_recovery_delivery_failure_leaves_provisional_ack(job, receiver, trajectory):
    path = job / "trial-1" / "agent" / "trajectory.json"
    path.unlink()
    assert import_job(job, settings=receiver.settings).incomplete == 1
    initial = {s.span_id for s in spans(receiver)}
    path.write_text(json.dumps(trajectory))
    receiver.responses = [(400, b"", {})]
    failed = import_job(job, settings=receiver.settings)
    assert failed.failed == 1 and failed.sent == 0
    retried = import_job(job, settings=receiver.settings)
    assert retried.sent == 1 and retried.conflicts == 0
    assert initial.isdisjoint({s.span_id for s in spans(receiver)[len(initial) + 1 :]})


def test_changed_trial_result_still_conflicts_after_incomplete(
    job, receiver, trajectory
):
    (job / "trial-1" / "agent" / "trajectory.json").unlink()
    assert import_job(job, settings=receiver.settings).incomplete == 1
    modify_result(job, verifier_result={"rewards": {"reward": 1}})
    (job / "trial-1" / "agent" / "trajectory.json").write_text(json.dumps(trajectory))
    result = import_job(job, settings=receiver.settings)
    assert result.conflicts == 1 and result.sent == 0
    assert len(receiver.requests) == 1
